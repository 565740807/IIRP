"""Disk ordering is optional; evidence and finite control bounds are mandatory."""

import importlib.util
import json
from datetime import timedelta

import pytest
from iirp.config import ROOT, settings
from iirp.models import now
from iirp.storage import maintenance


@pytest.fixture
def backup(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("backup_io_policy", ROOT / "scripts/backup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(settings(), "runtime_dir", tmp_path)
    return module


def test_io_order_changes_no_entries_and_falls_back_when_extents_unavailable(backup, monkeypatch, tmp_path):
    entries = [{"sha256": str(n) * 64, "relative_path": f"objects/{str(n) * 2}/{str(n) * 64}", "byte_size": n} for n in (1, 2, 3)]
    original = json.dumps(entries, sort_keys=True)
    monkeypatch.setattr(backup, "physical_offset", lambda path: {"1": 300, "2": None, "3": 100}[path.name[0]])
    ordered = backup.ordered_objects(tmp_path, entries, "test")
    assert [entry["sha256"][0] for entry in ordered] == ["3", "1", "2"]
    assert json.dumps(entries, sort_keys=True) == original
    assert sorted(ordered, key=lambda x: x["sha256"]) == entries
    monkeypatch.setattr(backup, "physical_offset", lambda path: None)
    assert backup.ordered_objects(tmp_path, entries[::-1], "test") == entries


def test_unsupported_or_missing_file_layout_does_not_skip_evidence(backup, tmp_path):
    assert backup.physical_offset(tmp_path / "missing") is None
    entry = {"sha256": "a" * 64, "relative_path": "objects/aa/" + "a" * 64, "byte_size": 1}
    assert backup.ordered_objects(tmp_path, [entry], "test") == [entry]
    with pytest.raises(FileNotFoundError):
        backup.digest(backup.object_path(tmp_path, entry))


def test_real_progress_extends_backup_but_neither_stalls_nor_total_bound(backup, monkeypatch, tmp_path):
    operation_id = "a" * 32
    directory = tmp_path / "maintenance-operations"
    directory.mkdir()
    path = directory / (operation_id + ".json")
    stamp = now()
    clock = [7200.0]
    monkeypatch.setattr(maintenance.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(maintenance, "now", lambda: stamp)
    path.write_text(json.dumps({"id": operation_id, "progress": {"updated_at": stamp.isoformat(), "completed": 25000}}))
    assert maintenance.maintenance_timeout(0, operation_id) is False
    assert maintenance.maintenance_timeout(0) is True
    path.write_text(json.dumps({"id": operation_id, "progress": {"updated_at": (stamp - timedelta(seconds=maintenance.BACKUP_STALL_SECONDS + 1)).isoformat()}}))
    assert maintenance.maintenance_timeout(0, operation_id) is True
    path.write_text(json.dumps({"id": operation_id, "progress": {"updated_at": stamp.isoformat()}}))
    clock[0] = maintenance.BACKUP_MAX_SECONDS + 1
    assert maintenance.maintenance_timeout(0, operation_id) is True
    path.write_text("invalid")
    assert maintenance.maintenance_timeout(0, operation_id) is True


def test_progress_uses_real_object_count_and_control_fence(backup, monkeypatch, tmp_path):
    from contextlib import contextmanager

    checks = []

    @contextmanager
    def fence():
        checks.append(True)
        yield

    backup.MANAGED_JOB = {"id": "synthetic"}
    backup._OPERATION = {"id": "b" * 32}
    monkeypatch.setattr(backup, "publication_guard", fence)
    backup.progress("source_verified", 17, 100, force=True)
    record = json.loads((tmp_path / "maintenance-operations" / ("b" * 32 + ".json")).read_text())
    assert record["progress"]["completed"] == 17 and checks == [True]
