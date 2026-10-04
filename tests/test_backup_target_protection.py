"""Filesystem failure paths must not acquire ownership of existing restores."""

import json
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import backup


@pytest.fixture
def isolated_files(tmp_path, monkeypatch):
    config = SimpleNamespace(
        runtime_dir=tmp_path, database_url="postgresql://test:test@localhost/iirp_v1_test_files",
        min_free_bytes=0,
    )
    monkeypatch.setattr(backup, "settings", lambda: config)
    monkeypatch.setattr(backup, "OPERATION_ID", "a" * 32)
    monkeypatch.setattr(backup, "maintenance_lock", nullcontext)
    return tmp_path


@pytest.mark.parametrize(
    "kind,parent", [
        ("restore_directory", "restores"), ("backup_directory", "backups"),
        ("pool_staging_directory", "backups"),
    ]
)
def test_existing_target_survives_failed_operation(isolated_files, kind, parent):
    destination = isolated_files / parent / "existing"
    destination.mkdir(parents=True)
    evidence = destination / "valuable"
    evidence.write_bytes(b"earlier verified backup")
    with pytest.raises(FileExistsError), backup.operation():
        backup.owned_directory(kind, destination)
    assert evidence.read_bytes() == b"earlier verified backup"
    record = json.loads(
        next((isolated_files / "maintenance-operations").glob("*.json")).read_text()
    )
    assert record["resources"] == []


@pytest.mark.parametrize("kind,parent,name", [
    ("restore_directory", "restores", "iirp_v1_test_restore_aaaaaaaaaaaa"),
    ("pool_staging_directory", "backups", ".pool-staging-" + "a" * 32),
])
def test_concurrent_target_creator_is_not_cleaned(isolated_files, monkeypatch, kind, parent, name):
    destination = isolated_files / parent / name
    original = type(destination).mkdir

    def mkdir(path, *args, **kwargs):
        if path == destination:
            original(path, parents=True)
            (path / "other-owner").write_text("retained")
            raise FileExistsError("synthetic race")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(type(destination), "mkdir", mkdir)
    with pytest.raises(FileExistsError), backup.operation():
        backup.owned_directory(kind, destination)
    assert (destination / "other-owner").read_text() == "retained"


@pytest.mark.parametrize("kind,parent,name", [
    ("restore_directory", "restores", "iirp_v1_test_restore_aaaaaaaaaaaa"),
    ("pool_staging_directory", "backups", ".pool-staging-" + "a" * 32),
])
def test_interruption_before_creation_confirmation_preserves_unknown_owner(
    isolated_files, monkeypatch, kind, parent, name
):
    destination = isolated_files / parent / name
    original = type(destination).mkdir

    def mkdir(path, *args, **kwargs):
        if path == destination:
            original(path, parents=True)
            (path / "other-owner").write_text("retained")
            raise KeyboardInterrupt("Synthetic interruption before mkdir return")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(type(destination), "mkdir", mkdir)
    with pytest.raises(KeyboardInterrupt), backup.operation():
        backup.owned_directory(kind, destination)
    assert (destination / "other-owner").read_text() == "retained"
    record = json.loads(
        next((isolated_files / "maintenance-operations").glob("*.json")).read_text()
    )
    assert record["status"] == "CLEANUP_FAILED"
    assert record["resources"][0]["ownership"] == "pending"


@pytest.mark.parametrize("kind,parent,name", [
    ("restore_directory", "restores", "iirp_v1_test_restore_aaaaaaaaaaaa"),
    ("pool_staging_directory", "backups", ".pool-staging-" + "a" * 32),
])
def test_replaced_target_does_not_inherit_directory_ownership(isolated_files, kind, parent, name):
    destination = isolated_files / parent / name
    with pytest.raises(RuntimeError), backup.operation():
        backup.owned_directory(kind, destination)
        destination.rename(destination.with_name("moved-owned-directory"))
        destination.mkdir()
        (destination / "other-owner").write_text("retained")
        raise RuntimeError("Synthetic target replacement")
    assert (destination / "other-owner").read_text() == "retained"


@pytest.mark.parametrize(
    "kind,parent,name",
    [
        ("restore_directory", "restores", "iirp_v1_test_restore_aaaaaaaaaaaa"),
        ("backup_directory", "backups", "synthetic-aaaaaaaaaaaa"),
        ("pool_staging_directory", "backups", ".pool-staging-" + "a" * 32),
    ],
)
def test_failed_operation_cleans_only_confirmed_directory(isolated_files, kind, parent, name):
    destination = isolated_files / parent / name
    with pytest.raises(RuntimeError), backup.operation():
        backup.owned_directory(kind, destination)
        (destination / "partial").write_text("owned scratch")
        raise RuntimeError("Synthetic failure after ownership confirmation")
    assert not destination.exists()


@pytest.mark.parametrize("failure", ["missing-dump", "linked-dump", "unknown-format"])
def test_incomplete_backup_rejected_before_database(isolated_files, monkeypatch, failure):
    directory = isolated_files / "backups" / "input"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(
        json.dumps({"format_version": 99 if failure == "unknown-format" else 3})
    )
    if failure == "linked-dump":
        (isolated_files / "external").write_bytes(b"must not read")
        (directory / "database.dump").symlink_to(isolated_files / "external")
    elif failure == "unknown-format":
        (directory / "database.dump").write_bytes(b"unused")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("No PG connection before backup validation")

    monkeypatch.setattr(backup, "connection", forbidden)
    with pytest.raises(ValueError):
        backup.restore_verify(directory)
    assert not (isolated_files / "restores").exists()


@pytest.fixture
def synthetic_database(isolated_files, monkeypatch):
    """Only SQL boundary responses are faked; filesystem rollback is real."""
    databases = {}
    drops = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, parameters=None):
            statement = query if isinstance(query, str) else query.as_string()
            row = None
            if statement.startswith("CREATE DATABASE"):
                name = statement.split('"')[1]
                databases[name] = 101
            elif statement.startswith("DROP DATABASE"):
                name = statement.split('"')[1]
                drops.append(name)
                databases.pop(name, None)
            elif statement.startswith("SELECT oid FROM pg_database"):
                oid = databases.get(parameters[0])
                row = (oid,) if oid is not None else None
            elif statement.startswith("SELECT pg_export_snapshot"):
                row = ("synthetic-snapshot",)
            elif statement.startswith("SELECT version_num"):
                row = ("0019",)
            elif statement.startswith("SELECT count(*) FROM job"):
                row = (0,)
            elif statement.startswith("SELECT to_regclass"):
                row = (None,)
            elif statement.startswith("SELECT pg_database_size"):
                row = (1024,)
            return SimpleNamespace(fetchone=lambda: row, fetchall=lambda: [])

    def pg_command(tool, _database, *args):
        if tool == "pg_dump":
            Path(args[-1]).write_bytes(b"synthetic dump boundary")

    monkeypatch.setattr(backup, "connection", lambda *_args, **_kwargs: Connection())
    monkeypatch.setattr(backup, "pg_command", pg_command)
    monkeypatch.setattr(backup, "table_fingerprints", lambda _conn: {})
    monkeypatch.setattr(backup, "require_capacity", lambda *_args: {})
    monkeypatch.setattr(backup, "durable_objects", lambda *_args: None)
    return SimpleNamespace(databases=databases, drops=drops)


def test_existing_staging_directory_survives_failed_backup(
    isolated_files, synthetic_database
):
    staging = isolated_files / "backups" / (".pool-staging-" + "a" * 32)
    staging.mkdir(parents=True)
    (staging / "other-owner").write_text("retained")
    with pytest.raises(FileExistsError):
        backup.backup()
    assert (staging / "other-owner").read_text() == "retained"


@pytest.mark.parametrize("replacement", ["directory", "database"])
def test_successful_restore_discard_checks_ownership(
    isolated_files, synthetic_database, monkeypatch, replacement
):
    directory = isolated_files / "backups" / "input"
    directory.mkdir(parents=True)
    dump = directory / "database.dump"
    dump.write_bytes(b"synthetic dump boundary")
    (directory / "manifest.json").write_text(json.dumps({
        "format_version": 3, "objects": [], "database_sha256": backup.digest(dump),
        "migration": "0019", "job_count": 0, "policy": None,
    }))
    destination = isolated_files / "restores" / "iirp_v1_test_restore_aaaaaaaaaaaa"
    original = backup.atomic_json

    def replace_after_verified(path, data):
        original(path, data)
        if path.name != "restore-report.json":
            return
        if replacement == "directory":
            destination.rename(destination.with_name("moved-owned-directory"))
            destination.mkdir()
            (destination / "other-owner").write_text("retained")
        else:
            synthetic_database.databases[destination.name] = 202

    monkeypatch.setattr(backup, "atomic_json", replace_after_verified)
    with pytest.raises(ValueError, match="所有权"):
        backup.restore_verify(directory, discard=True)
    if replacement == "directory":
        assert (destination / "other-owner").read_text() == "retained"
    else:
        assert synthetic_database.databases[destination.name] == 202
        assert not synthetic_database.drops


@pytest.mark.parametrize("kind", ["restore_directory", "restore_database"])
def test_legacy_unconfirmed_target_is_preserved(
    isolated_files, synthetic_database, kind
):
    operation_id = "a" * 32
    name = "iirp_v1_test_restore_aaaaaaaaaaaa"
    destination = isolated_files / "restores" / name
    destination.mkdir(parents=True)
    (destination / "other-owner").write_text("retained")
    synthetic_database.databases[name] = 202
    backup._journal_write({
        "id": operation_id, "source_database": "iirp_v1_test_files", "status": "RUNNING",
        "resources": [{"kind": kind, "value": name if kind == "restore_database" else str(destination)}],
    })
    report = backup.cleanup_owned_operation(operation_id)
    assert report["failures"]
    assert (destination / "other-owner").read_text() == "retained"
    assert synthetic_database.databases[name] == 202
    assert not synthetic_database.drops


def test_legacy_partial_journal_remains_compatible(isolated_files):
    operation_id = "a" * 32
    pool = isolated_files / "backups" / "object-pool" / "objects" / "aa"
    pool.mkdir(parents=True)
    own = pool / ("synthetic.partial-" + operation_id)
    other = pool / ("synthetic.partial-" + "b" * 32)
    for path in (own, other):
        path.write_text("retained unless owned")
    backup._journal_write({
        "id": operation_id, "source_database": "iirp_v1_test_files", "status": "RUNNING",
        "resources": [{"kind": "pool_partial", "value": str(own)}],
    })
    report = backup.cleanup_owned_operation(operation_id)
    assert not report["failures"]
    assert not own.exists()
    assert other.read_text() == "retained unless owned"


@pytest.mark.parametrize("collision", ["empty-target", "linked-parent"])
def test_retention_refuses_existing_trash_namespace(isolated_files, monkeypatch, collision):
    root = isolated_files / "backups"
    keep, retired = root / "keep", root / "retired"
    for path in (keep, retired):
        path.mkdir(parents=True)
        (path / "manifest.json").write_text("retained source")
    trash_parent = root / (".trash-" + "a" * 32)
    if collision == "empty-target":
        (trash_parent / retired.name).mkdir(parents=True)
    else:
        external = isolated_files / "external"
        external.mkdir()
        trash_parent.symlink_to(external, target_is_directory=True)
    stamp = datetime.now(timezone.utc)
    monkeypatch.setattr(backup, "completed_backups", lambda: [(stamp, keep, {}), (stamp, retired, {})])
    monkeypatch.setattr(backup, "backup_integrity", lambda _entry: True)
    monkeypatch.setattr(backup, "verified_timestamp", lambda _entry: stamp)
    with pytest.raises(FileExistsError if collision == "empty-target" else ValueError):
        backup.prune_backups()
    assert (retired / "manifest.json").read_text() == "retained source"
    if collision == "empty-target":
        assert (trash_parent / retired.name).is_dir()
        assert not list((trash_parent / retired.name).iterdir())
    else:
        assert trash_parent.is_symlink() and not list(external.iterdir())
