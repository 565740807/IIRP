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

    def collect():
        calls.append(1)
        entered.set()
        assert gate.wait(2)
        return {"database": 100, "objects": 2, "bytes": 1000,
                "backup_estimated_temporary_bytes": temporary_bytes("backup", 100, 1000, 2),
                "restore_estimated_temporary_bytes": temporary_bytes("restore", 100, 1000, 2)}

    monkeypatch.setattr(inventory, "_collect", collect)
    first = inventory.storage_state()
    assert first["inventory_status"] == "refreshing"
    assert first["restore_capacity_sufficient"] is None
    assert entered.wait(2)
    for _ in range(10):
        assert inventory.storage_state()["restore_capacity_sufficient"] is None
    assert len(calls) == 1
    gate.set()
    inventory._THREAD.join(2)
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
    inventory._THREAD.join(2)

    def fail():
        raise RuntimeError("controlled failure")

    monkeypatch.setattr(inventory, "_collect", fail)
    record = json.loads(inventory._file().read_text())
    record["measured_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    inventory._write(record)
    inventory.storage_state()
    inventory._THREAD.join(2)
    failed = inventory.storage_state()
    assert failed["inventory_status"] == "failed"
    assert "controlled failure" in failed["inventory_error"]
    assert failed["restore_capacity_sufficient"] is None
    record = json.loads(inventory._file().read_text())
    record["attempted_at"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    inventory._write(record)
    monkeypatch.setattr(inventory, "_collect", collect)
    inventory.storage_state()
    inventory._THREAD.join(2)
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
