import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from iirp import storage_inventory as inventory
from iirp.capacity import capacity_sufficient, temporary_bytes


def test_capacity_formula_and_thresholds():
    required = temporary_bytes("restore", 100, 1000, 3)
    assert required == 2 * 100 + 1000 + 3 * 4096
    assert temporary_bytes("backup", 100, 1000, 3) == 100 + 1000 + 3 * 4096
    assert capacity_sufficient(10000 + required - 1, required, 10000) is False
    assert capacity_sufficient(10000 + required, required, 10000) is True
    assert capacity_sufficient(10000 + required + 1, required, 10000) is True
    assert capacity_sufficient(10000 + required + 1, None, 10000) is None
    assert capacity_sufficient(None, required, 10000) is None


def test_inventory_coalesces_and_recovers_from_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "settings", lambda: SimpleNamespace(
        runtime_dir=tmp_path, min_free_bytes=10000))
    monkeypatch.setattr(inventory.shutil, "disk_usage", lambda _: SimpleNamespace(free=30000))
    gate = threading.Event()
    entered = threading.Event()
    calls = []

    def collect(_cached=None):
        calls.append(1)
        entered.set()
        assert gate.wait(2)
        return {"database": 100, "objects": 2, "bytes": 1000,
                "backup_estimated_temporary_bytes": temporary_bytes("backup", 100, 1000, 2),
                "restore_estimated_temporary_bytes": temporary_bytes("restore", 100, 1000, 2)}, {}

    monkeypatch.setattr(inventory, "_collect", collect)
    first = inventory.storage_state()
    assert first["inventory_status"] == "refreshing"
    assert first["restore_capacity_sufficient"] is None
    assert entered.wait(2)
    for _ in range(10):
        assert inventory.storage_state()["restore_capacity_sufficient"] is None
    assert len(calls) == 1
    gate.set()
    inventory._THREADS["summary"].join(2)
    fresh = inventory.storage_state()
    assert fresh["inventory_status"] == "fresh"
    assert fresh["restore_capacity_sufficient"] is True
    assert len(calls) == 1

    # An expired snapshot can be read but cannot certify capacity.
    record = json.loads(inventory._file().read_text())
    record["measured_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    inventory._write(record)
    gate.clear()
    entered.clear()
    stale = inventory.storage_state()
    assert stale["inventory_status"] == "stale"
    assert stale["restore_capacity_sufficient"] is None
    assert entered.wait(2)
    gate.set()
    inventory._THREADS["summary"].join(2)

    def fail(_cached=None):
        raise RuntimeError("controlled failure")

    monkeypatch.setattr(inventory, "_collect", fail)
    record = json.loads(inventory._file().read_text())
    record["measured_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    inventory._write(record)
    inventory.storage_state()
    inventory._THREADS["summary"].join(2)
    failed = inventory.storage_state()
    assert failed["inventory_status"] == "failed"
    assert "controlled failure" in failed["inventory_error"]
    assert failed["restore_capacity_sufficient"] is None
    record = json.loads(inventory._file().read_text())
    record["attempted_at"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    inventory._write(record)
    monkeypatch.setattr(inventory, "_collect", collect)
    inventory.storage_state()
    inventory._THREADS["summary"].join(2)
    assert inventory.storage_state()["inventory_status"] == "fresh"


def test_inventory_disk_failure_is_unknown_not_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "settings", lambda: SimpleNamespace(
        runtime_dir=tmp_path, min_free_bytes=10000))
    inventory._write({"status": "fresh", "measured_at": datetime.now(timezone.utc).isoformat(),
                      "totals": {"restore_estimated_temporary_bytes": 1000}})
    def fail(_):
        raise OSError("controlled disk failure")
    monkeypatch.setattr(inventory.shutil, "disk_usage", fail)
    state = inventory.storage_state()
    assert state["free_bytes"] is None
    assert state["restore_capacity_sufficient"] is None
    assert state["disk_pressure"] is None
    assert "controlled disk failure" in state["inventory_error"]


def test_routine_reads_never_start_the_exact_tree_walk(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "settings", lambda: SimpleNamespace(
        runtime_dir=tmp_path, min_free_bytes=10000))
    monkeypatch.setattr(inventory.shutil, "disk_usage", lambda _: SimpleNamespace(free=30000))
    monkeypatch.setattr(inventory, "_collect", lambda _cached=None: ({"database": 1, "objects": 0, "bytes": 0}, {}))
    walks = []
    monkeypatch.setattr(inventory, "_exact", lambda: walks.append(1) or {"evidence": {"allocated_bytes": 5}})
    for _ in range(3):
        inventory.storage_state()
        inventory._THREADS["summary"].join(2)
    assert walks == []
    state = inventory.request_exact()
    inventory._THREADS["exact"].join(2)
    assert walks == [1]
    state = inventory.storage_state()
    assert state["exact_status"] == "fresh"
    assert state["paths"]["evidence"]["allocated_bytes"] == 5
    # A daily background check does not repeat within 24 hours.
    assert inventory.exact_tick() is None
    assert walks == [1]


def test_backup_sizes_come_from_cached_manifests(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "settings", lambda: SimpleNamespace(runtime_dir=tmp_path))
    for name, sizes in (("20260101T000000Z-a", [10, 20]), ("20260102T000000Z-b", [10, 20, 30])):
        directory = tmp_path / "backups" / name
        directory.mkdir(parents=True)
        (directory / "database.dump").write_bytes(b"x" * 7)
        (directory / "manifest.json").write_text(json.dumps(
            {"completed_at": name, "objects": [{"sha256": "0" * 64, "byte_size": size} for size in sizes],
             "restore_verified_at": None}, indent=2))
    (tmp_path / "backups" / "object-pool").mkdir()
    cache, listed, estimate = inventory._backups({})
    assert [item["object_bytes"] for item in listed] == [30, 60]
    assert [item["object_count"] for item in listed] == [2, 3]
    assert listed[0]["completed_at"] == "20260101T000000Z-a"
    assert estimate == 7 + 7 + 60
    # An unchanged manifest is not parsed again.
    (tmp_path / "backups" / "20260101T000000Z-a" / "manifest.json").chmod(0)
    try:
        again, _listed, _estimate = inventory._backups(cache)
    finally:
        (tmp_path / "backups" / "20260101T000000Z-a" / "manifest.json").chmod(0o600)
    assert again["20260101T000000Z-a"]["object_bytes"] == 30
