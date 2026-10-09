"""Small isolated restore smoke test; does not claim full F acceptance."""

import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from iirp.analysis.calendar import ET, sessions
from iirp.config import ROOT, settings
from iirp.db import engine, session
from iirp.jobs import schedule
from iirp.jobs.batches import defaults
from iirp.jobs.queue import claim, control, fenced
from iirp.models import (
    AnalysisRequest,
    AnalysisResult,
    Batch,
    BatchJob,
    CollectionStrategy,
    ExportManifest,
    FeedSession,
    Job,
    MaintenanceRun,
    Policy,
    RequestScope,
    Security,
    SourceObject,
    now,
)
from iirp.storage import maintenance
from iirp.storage.objects import save_object
from psycopg import sql
from sqlalchemy import event, func, select
from sqlalchemy.engine import make_url


@pytest.mark.slow
def test_backup_content_hashes_pool_and_safe_restore(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "maintenance_backup_smoke", ROOT / "scripts/backup.py"
    )
    backup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup)
    url = make_url(settings().database_url)
    name = "iirp_v1_test_maintenance_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname="postgres",
        autocommit=True,
    )
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    previous = os.environ.get("IIRP_DATABASE_URL")
    os.environ["IIRP_DATABASE_URL"] = url.set(database=name).render_as_string(hide_password=False)
    engine.cache_clear()
    settings.cache_clear()
    settings().runtime_dir = tmp_path
    settings().min_free_bytes = 0
    restored = None
    try:
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        source = save_object(b"synthetic immutable maintenance evidence", "text/plain")
        with session() as s, s.begin():
            s.add(SourceObject(**source))
            s.add(Policy(id=1, sec_enabled=True, version=2))
            s.add(CollectionStrategy(key="backup", enabled=True, version=2, options={}))
            s.add(
                Job(
                    kind="fixture_check",
                    title="paused fixture",
                    target={},
                    idempotency_key="p" * 64,
                    status="PAUSED",
                    requested_action="pause",
                )
            )
            s.add(
                Job(
                    kind="fixture_check",
                    title="cancel fixture",
                    target={},
                    idempotency_key="c" * 64,
                    status="CANCEL_REQUESTED",
                    requested_action="cancel",
                )
            )
        first = backup.backup()
        second = backup.backup()
        relative = source["relative_path"]
        assert (first / relative).stat().st_ino == (second / relative).stat().st_ino
        assert (first / relative).stat().st_ino != (tmp_path / relative).stat().st_ino
        assert backup.prune_backups()["removed"] == []
        report = backup.restore_verify(second)
        restored = report["database"]
        assert report["fact_hashes_verified"] > 20
        with backup.connection(restored) as conn:
            assert conn.execute("SELECT sec_enabled FROM collection_policy").fetchone() == (False,)
            assert conn.execute("SELECT enabled FROM collection_strategy").fetchone() == (False,)
            assert conn.execute(
                "SELECT status,requested_action FROM job ORDER BY title"
            ).fetchall() == [("CANCELLED", "cancel"), ("PAUSED", "pause")]
        with session() as s:
            assert s.get(Policy, 1).sec_enabled
            assert s.get(CollectionStrategy, "backup").enabled
    finally:
        if restored:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(restored)))
        engine().dispose()
        engine.cache_clear()
        if previous is None:
            os.environ.pop("IIRP_DATABASE_URL", None)
        else:
            os.environ["IIRP_DATABASE_URL"] = previous
        settings.cache_clear()
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


# The remaining tests intentionally use disposable databases and synthetic files.


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    url = make_url(settings().database_url)
    database = "iirp_v1_test_maintenance_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname="postgres",
        autocommit=True,
    )
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    engine().dispose()
    engine.cache_clear()
    monkeypatch.setenv(
        "IIRP_DATABASE_URL", url.set(database=database).render_as_string(hide_password=False)
    )
    monkeypatch.setenv("IIRP_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("IIRP_MIN_FREE_BYTES", "0")
    settings.cache_clear()
    # SEC scheduling needs a real contact; fetch_sec keeps its own check (CI has none).
    monkeypatch.setattr("iirp.jobs.auto_update.sec_configured", lambda: True)
    monkeypatch.setattr("iirp.jobs.schedule.sec_configured", lambda: True)
    spec = importlib.util.spec_from_file_location(
        "maintenance_" + uuid.uuid4().hex, ROOT / "scripts/backup.py"
    )
    backup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup)
    try:
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        with session() as s, s.begin():
            s.add(Policy(id=1, sec_enabled=False))
            defaults(s)
            # These tests opt in to one maintenance/source action at a time;
            # formal first-open defaults must not schedule unrelated work.
            for strategy in s.scalars(select(CollectionStrategy)):
                strategy.enabled = False
                strategy.next_run_at = None
                strategy.options = {**strategy.options, "user_controlled": True}
        yield SimpleNamespace(backup=backup, runtime=tmp_path, admin=admin, database=database)
    finally:
        # Only databases registered by this test invocation are candidates.
        for journal in (tmp_path / "maintenance-operations").glob("*.json"):
            record = json.loads(journal.read_text())
            for entry in record.get("resources", []):
                if entry["kind"] == "restore_database":
                    admin.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                            sql.Identifier(entry["value"])
                        )
                    )
        engine().dispose()
        engine.cache_clear()
        settings.cache_clear()
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
        admin.close()


def make_job(kind="maintenance_backup"):
    with session() as s, s.begin():
        job = Job(
            kind=kind, title="synthetic maintenance", target={}, idempotency_key=uuid.uuid4().hex
        )
        s.add(job)
    return claim({kind})


@pytest.mark.slow
def test_many_source_backup_journals_one_owned_namespace_and_preserves_pool(isolated, monkeypatch):
    backup = isolated.backup
    sources = [save_object(f"synthetic backup object {index}".encode()) for index in range(64)]
    with session() as s, s.begin():
        for source in sources:
            s.add(SourceObject(**source))
    writes = []
    original = backup._journal_write

    def journal(record):
        writes.append(json.loads(json.dumps(record)))
        original(record)

    monkeypatch.setattr(backup, "_journal_write", journal)
    monkeypatch.setattr(backup, "physical_offset", lambda path: -int(path.name, 16))
    first = backup.backup()
    objects = json.loads((first / "manifest.json").read_text())["objects"]
    assert len(objects) == 64
    assert [entry["sha256"] for entry in objects] == sorted(source["sha256"] for source in sources)
    # Progress records can advance; the resource journal never grows per object.
    # Both directory creations persist intent and confirmed inode ownership.
    # Metadata remains bounded by resource count, never by object count.
    assert len({json.dumps(record["resources"], sort_keys=True) for record in writes}) <= 5
    assert max(len(record["resources"]) for record in writes) == 2
    assert {entry["kind"] for entry in writes[-1]["resources"]} == {
        "backup_directory", "pool_staging_directory",
    }
    assert all(
        set(entry["ownership"]) == {"device", "inode"}
        for entry in writes[-1]["resources"]
    )
    assert not list((isolated.runtime / "backups").glob(".pool-staging-*"))
    # All published source copies are checked and reused, without relinking a
    # mutable runtime inode or adding per-object recovery journal resources.
    second = backup.backup()
    for source in sources:
        relative = source["relative_path"]
        assert backup.digest(first / relative) == source["sha256"]
        assert (first / relative).stat().st_ino == (second / relative).stat().st_ino
        assert (first / relative).stat().st_ino != (isolated.runtime / relative).stat().st_ino


def test_manual_verify_uses_shared_lock_and_managed_does_not_relock(isolated):
    backup = isolated.backup
    directory = backup.backup()
    with maintenance.maintenance_lock(), pytest.raises(ValueError, match="maintenance.busy"):
        backup.restore_verify(directory)
    result = backup.managed_backup()
    assert result["verified"] is True
    manifest = json.loads((Path(result["backup_path"]) / "manifest.json").read_text())
    assert manifest["restore_verification"]["verification_copy_discarded"] is True
    # Only the verification report remains where the copy was.
    assert [path.name for path in (isolated.runtime / "restores").iterdir()] == [
        manifest["restore_verification"]["database"] + "-report.json"]
    assert backup.completed_backups()


@pytest.mark.parametrize("action", ["renew", "pause", "cancel"])
def test_restore_database_creation_allows_renewal_and_control(isolated, monkeypatch, action):
    """A CREATE DATABASE checkpoint must not lock out the supervising worker.

    Exercise real independent DB connections at the slow SQL boundary; no sleep
    or larger timeout is needed to reproduce the former source-row contention.
    """
    backup = isolated.backup
    directory = backup.backup()
    job = make_job()
    backup.MANAGED_JOB = {
        "id": job.id,
        "lease_token": job.lease_token,
        "control_version": job.control_version,
    }
    original_connection = backup.connection
    created = []

    @contextmanager
    def checkpoint_connection(database=None, **kwargs):
        with original_connection(database, **kwargs) as conn:
            if database != "postgres":
                yield conn
                return

            class DuringCreate:
                def execute(self, query, *args, **options):
                    statement = query if isinstance(query, str) else query.as_string(conn)
                    if statement.startswith("CREATE DATABASE "):
                        # This is the parent's real 500ms-bounded renewal path.
                        assert fenced(job, acknowledge_control=False)
                        if action != "renew":
                            control(job.id, action)
                        created.append(statement)
                    return conn.execute(query, *args, **options)

            yield DuringCreate()

    monkeypatch.setattr(backup, "connection", checkpoint_connection)
    if action == "renew":
        # The real worker supervises the whole restore. A one-off renewal at
        # CREATE DATABASE expires during a slow pg_restore on a busy host.
        stopped = threading.Event()
        renewal_errors = []

        def renew():
            while not stopped.wait(0.1):
                try:
                    if not fenced(job, acknowledge_control=False):
                        renewal_errors.append("lease lost")
                        return
                except Exception as exc:
                    renewal_errors.append(str(exc))
                    return

        supervisor = threading.Thread(target=renew, daemon=True)
        supervisor.start()
        try:
            report = backup.restore_verify(directory, discard=True)
        finally:
            stopped.set()
            supervisor.join(timeout=2)
        assert not supervisor.is_alive()
        assert not renewal_errors
        assert report["fact_hashes_verified"] > 20
        assert report["verification_copy_discarded"]
        assert json.loads((directory / "manifest.json").read_text())["restore_verified_at"]
    else:
        with pytest.raises(InterruptedError, match="失效"):
            backup.restore_verify(directory, discard=True)
        assert json.loads((directory / "manifest.json").read_text())["restore_verified_at"] is None
        assert not fenced(job)
        with session() as s:
            assert s.get(Job, job.id).status == {"pause": "PAUSED", "cancel": "CANCELLED"}[action]
    assert len(created) == 1
    # No restored copy is left; a passing run keeps only its report file.
    assert all(path.is_file() and path.name.endswith("-report.json")
               for path in (isolated.runtime / "restores").iterdir())
    assert (directory / "database.dump").is_file()


def test_failed_restore_removes_only_own_registered_copy(isolated, monkeypatch):
    backup = isolated.backup
    directory = backup.backup()
    preserved = isolated.runtime / "restores" / "unrelated-user-copy"
    preserved.mkdir(parents=True)
    (preserved / "keep").write_text("keep")
    restore_names = []

    def broken(tool, database, *_args):
        assert tool == "pg_restore"
        restore_names.append(database)
        raise RuntimeError("injected restore interruption")

    monkeypatch.setattr(backup, "pg_command", broken)
    with pytest.raises(RuntimeError, match="injected"):
        backup.restore_verify(directory)
    assert preserved.is_dir() and (preserved / "keep").read_text() == "keep"
    assert list((isolated.runtime / "restores").iterdir()) == [preserved]
    assert (
        isolated.admin.execute(
            "SELECT datname FROM pg_database WHERE datname=ANY(%s)", (restore_names,)
        ).fetchall()
        == []
    )
    assert (directory / "manifest.json").exists()
    reports = [
        json.loads(p.read_text())
        for p in (isolated.runtime / "maintenance-operations").glob("*.json")
    ]
    assert any(r["status"] == "ROLLED_BACK" for r in reports)


@pytest.mark.parametrize("failure", ["replaced", "unconfirmed"])
def test_restore_rollback_preserves_database_without_matching_oid(isolated, monkeypatch, failure):
    backup = isolated.backup
    directory = backup.backup()
    names = []
    original_connection = backup.connection

    if failure == "replaced":
        def replace_before_restore(_tool, name, *_args):
            names.append(name)
            old_oid = isolated.admin.execute(
                "SELECT oid FROM pg_database WHERE datname=%s", (name,)
            ).fetchone()[0]
            isolated.admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            isolated.admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            new_oid = isolated.admin.execute(
                "SELECT oid FROM pg_database WHERE datname=%s", (name,)
            ).fetchone()[0]
            assert new_oid != old_oid
            raise RuntimeError("synthetic replacement before pg_restore")

        monkeypatch.setattr(backup, "pg_command", replace_before_restore)
    else:
        @contextmanager
        def interrupt_after_create(database=None, **kwargs):
            with original_connection(database, **kwargs) as conn:
                class InterruptedCreate:
                    def execute(self, query, *args, **options):
                        statement = query if isinstance(query, str) else query.as_string(conn)
                        result = conn.execute(query, *args, **options)
                        if statement.startswith("CREATE DATABASE "):
                            names.append(statement.split('"')[1])
                            raise KeyboardInterrupt("synthetic interruption before OID confirmation")
                        return result

                yield InterruptedCreate()

        monkeypatch.setattr(backup, "connection", interrupt_after_create)

    with pytest.raises(RuntimeError if failure == "replaced" else KeyboardInterrupt):
        backup.restore_verify(directory)
    assert len(names) == 1
    assert isolated.admin.execute(
        "SELECT datname FROM pg_database WHERE datname=%s", (names[0],)
    ).fetchone() == (names[0],)
    records = [json.loads(path.read_text()) for path in
               (isolated.runtime / "maintenance-operations").glob("*.json")]
    failed = next(record for record in records if record["status"] == "CLEANUP_FAILED")
    assert any(entry["kind"] == "restore_database" for entry in failed["cleanup"]["failures"])
    assert not (isolated.runtime / "restores" / names[0]).exists()


def test_backup_failure_reclaims_partial_but_preserves_previous_and_pool(isolated, monkeypatch):
    backup = isolated.backup
    good = backup.backup()
    monkeypatch.setattr(
        backup,
        "pg_command",
        lambda *_a: (_ for _ in ()).throw(OSError("injected dump interruption")),
    )
    with pytest.raises(OSError, match="injected"):
        backup.backup()
    directories = [p for p in (isolated.runtime / "backups").iterdir() if p.is_dir()]
    assert directories == [good]
    assert (good / "manifest.json").exists()


def test_capacity_checks_estimate_before_dump_or_restore(isolated, monkeypatch):
    backup = isolated.backup
    good = backup.backup()
    monkeypatch.setattr(
        backup.shutil, "disk_usage", lambda _p: SimpleNamespace(free=1, total=100, used=99)
    )
    with pytest.raises(OSError, match="预计需要"):
        backup.backup()
    with pytest.raises(OSError, match="预计需要"):
        backup.restore_verify(good)
    assert not (isolated.runtime / "restores").exists()
    assert [p[1] for p in backup.completed_backups()] == [good]


def synthetic_backup(root, stamp, *, verified=False, corrupt=False, name=None):
    directory = root / (name or stamp.strftime("%Y%m%dT%H%M%SZ"))
    directory.mkdir(parents=True)
    payload = b"synthetic dump, not a restore fixture"
    (directory / "database.dump").write_bytes(payload + (b"corrupted" if corrupt else b""))
    manifest = {
        "format_version": 3,
        "created_at": directory.name,
        "completed_at": stamp.isoformat(),
        "database_sha256": hashlib.sha256(payload).hexdigest(),
        "objects": [],
        "restore_verified_at": (stamp + timedelta(minutes=1)).isoformat() if verified else None,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory


def test_retention_keeps_only_the_two_newest_complete_backups(isolated):
    backup = isolated.backup
    root = isolated.runtime / "backups"
    stamp = now() - timedelta(hours=1)
    paths = [synthetic_backup(root, stamp - timedelta(days=i), verified=i == 4) for i in range(6)]
    unknown = root / "unfinished-user-directory"
    unknown.mkdir()
    report = backup.prune_backups()
    assert report["kept"] == sorted([paths[0].name, paths[1].name])
    assert sorted(report["removed"]) == sorted(path.name for path in paths[2:])
    assert paths[0].is_dir() and paths[1].is_dir() and unknown.is_dir()
    assert not any(path.exists() for path in paths[2:])


@pytest.mark.parametrize("mutation", ["expired", "token", "version", "pause", "cancel"])
def test_stale_child_cannot_publish_or_prune(isolated, mutation):
    backup = isolated.backup
    job = make_job()
    backup.MANAGED_JOB = {
        "id": job.id,
        "lease_token": job.lease_token,
        "control_version": job.control_version,
    }
    with session() as s, s.begin():
        current = s.get(Job, job.id)
        if mutation == "expired":
            current.lease_until = now() - timedelta(seconds=1)
        elif mutation == "token":
            current.lease_token = str(uuid.uuid4())
        elif mutation == "version":
            current.control_version += 1
        else:
            current.requested_action = mutation
    target = isolated.runtime / "would-publish.json"
    with pytest.raises(InterruptedError, match="失效"):
        backup.atomic_json(target, {"bad": "must never be published"})
    assert not target.exists()
    stamp = now() - timedelta(hours=1)
    paths = [
        synthetic_backup(isolated.runtime / "backups", stamp - timedelta(days=i), verified=i == 0)
        for i in range(20)
    ]
    with pytest.raises(InterruptedError):
        backup.prune_backups()
    assert all(p.exists() for p in paths)


def seed_results(status="SUCCEEDED", count=4, *, expires_in=timedelta(hours=12)):
    with session() as s, s.begin():
        security = Security(symbol="SYNTH" + uuid.uuid4().hex[:5].upper(), metadata_json={})
        batch = Batch(
            request_id=uuid.uuid4().hex,
            scope_key=uuid.uuid4().hex,
            kind="market_history",
            title="synthetic analysis",
            params={},
            status=status,
        )
        s.add_all([security, batch])
        s.flush()
        request = AnalysisRequest(batch_id=batch.id, params={"kind": "monthly", "cutoff_date": "2026-09-22", "historical_years": 8})
        s.add(request)
        s.flush()
        s.add(RequestScope(batch_id=batch.id, symbol=security.symbol, security_id=security.id))
        ids = []
        for index in range(count):
            result = AnalysisResult(
                analysis_id=request.id,
                security_id=security.id,
                input_key=uuid.uuid4().hex,
                inputs={},
                data={"synthetic": "x" * 1000},
                created_at=now() - timedelta(hours=1, minutes=count - index),
                accessed_at=now() - timedelta(hours=1),
                expires_at=now() + expires_in,
            )
            s.add(result)
            s.flush()
            ids.append(result.id)
        return ids


def test_maintenance_deletes_only_expired_results_and_marks_their_research(isolated):
    live = seed_results()
    expired = seed_results(expires_in=-timedelta(seconds=1))
    with session() as s, s.begin():
        s.add(ExportManifest(result_ids=[live[1]], params={}, expires_at=now() - timedelta(days=10)))
        s.add(FeedSession(revision_ids=[], filters={}, expires_at=now() - timedelta(seconds=1)))
    details = maintenance.cleanup()["data"]
    with session() as s:
        assert set(s.scalars(select(AnalysisResult.id))) == set(live)
        assert s.scalar(select(func.count()).select_from(FeedSession)) == 0
        marked = s.scalars(select(RequestScope.checkpoint)).all()
        assert sum(bool(item.get("results_expired_at")) for item in marked) == 1
    assert details["analysis_results"] == len(expired)
    assert details["reading_sessions_removed"] == 1


def test_expired_reading_indexes_delete_one_per_transaction(isolated):
    with session() as s, s.begin():
        s.add_all(
            FeedSession(
                revision_ids=[uuid.uuid4().hex for _ in range(1000)],
                filters={"purpose": "entity", "schema": "entity-index-v1"},
                expires_at=now() - timedelta(seconds=1),
            )
            for _ in range(3)
        )
        live = FeedSession(revision_ids=[], filters={}, expires_at=now() + timedelta(hours=1))
        s.add(live)
        s.flush()
        preserved = live.id
    batches = []

    def deleted(connection, _cursor, statement, parameters, _context, _executemany):
        if statement.startswith("DELETE FROM feed_session"):
            batches.append((connection.get_transaction(), len(parameters)))

    event.listen(engine(), "before_cursor_execute", deleted)
    try:
        result = maintenance.cleanup()["data"]
    finally:
        event.remove(engine(), "before_cursor_execute", deleted)
    assert result["reading_sessions_removed"] == 3
    assert len(batches) == 3
    assert all(count == 1 for _transaction, count in batches)
    assert len({id(transaction) for transaction, _count in batches}) == 3
    with session() as s:
        assert list(s.scalars(select(FeedSession.id))) == [preserved]


def test_export_reference_lock_serializes_cleanup_and_export(isolated):
    ids = seed_results()
    acquired, release, finished = threading.Event(), threading.Event(), threading.Event()

    def export():
        with session() as s, s.begin():
            maintenance.lock_analysis_references(s)
            assert s.get(AnalysisResult, ids[0]) is not None
            acquired.set()
            assert release.wait(3)
            s.add(
                ExportManifest(result_ids=[ids[0]], params={}, expires_at=now() + timedelta(days=1))
            )
        finished.set()

    thread = threading.Thread(target=export)
    thread.start()
    assert acquired.wait(3)
    with session() as s, s.begin():
        assert maintenance.lock_analysis_references(s, wait=False) is False
    release.set()
    assert finished.wait(3)
    thread.join()
    maintenance.cleanup()
    with session() as s:
        assert s.get(AnalysisResult, ids[0]) is not None
        assert s.get(AnalysisResult, ids[-1]) is not None
        assert s.get(AnalysisResult, ids[1]) is not None


def test_cleanup_observes_pause_between_small_pages(isolated, monkeypatch):
    with session() as s, s.begin():
        s.add_all(FeedSession(revision_ids=[], filters={}, expires_at=now() - timedelta(seconds=1))
                  for _ in range(3))
    job = make_job("maintenance_clean")
    original_fence = __import__("iirp.jobs.queue", fromlist=["fenced"]).fenced
    pages = []

    def fenced(*args, **kwargs):
        ok = original_fence(*args, **kwargs)
        pages.append(ok)
        if ok and len(pages) == 1:
            with session() as s, s.begin():
                current = s.get(Job, job.id)
                current.requested_action = "pause"
                current.control_version += 1
                current.status = "PAUSE_REQUESTED"
        return ok

    monkeypatch.setattr("iirp.jobs.queue.fenced", fenced)
    assert maintenance.cleanup(job) is None
    with session() as s:
        assert s.scalar(select(func.count()).select_from(FeedSession)) == 2
        assert s.scalar(select(func.count()).select_from(MaintenanceRun)) == 0
        assert s.get(Job, job.id).status == "PAUSE_REQUESTED"


@pytest.mark.parametrize(
    "day,work,stock",
    [
        (date(2026, 4, 3), True, False),  # Good Friday: SEC business day, NYSE closed.
        (date(2026, 10, 12), False, True),  # Federal Columbus Day: NYSE open.
        (date(2026, 11, 11), False, True),
        (date(2026, 7, 3), False, False),
    ],
)
def test_sec_federal_calendar_is_not_exchange_calendar(day, work, stock):
    assert schedule.sec_workday(day) is work
    assert bool(sessions(day, day)) is stock


@pytest.mark.parametrize(
    "value,seconds",
    [
        ("2026-09-08T05:59:00-04:00", 1800),
        ("2026-09-08T06:00:00-04:00", 120),
        ("2026-09-08T09:29:00-04:00", 120),
        ("2026-09-08T09:30:00-04:00", 60),
        ("2026-09-08T16:01:00-04:00", 60),
        ("2026-09-08T22:00:00-04:00", 1800),
        ("2026-04-03T12:00:00-04:00", 120),
        ("2026-10-12T12:00:00-04:00", 3600),
    ],
)
def test_sec_time_boundaries(value, seconds):
    assert schedule.sec_poll_seconds(datetime.fromisoformat(value)) == seconds


def test_disabled_schedules_and_maintenance_have_no_security_side_effect(isolated, monkeypatch):
    instant = datetime(2026, 9, 8, 10, tzinfo=ET)
    monkeypatch.setattr(schedule, "now", lambda: instant)
    schedule.schedule_tick()
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Batch)) == 0
    with session() as s, s.begin():
        p = s.get(CollectionStrategy, "backup")
        p.enabled = True
        p.next_run_at = instant - timedelta(days=12)
    schedule.schedule_tick()
    schedule.schedule_tick()
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Batch)) == 1
        assert s.scalar(select(func.count()).select_from(Security)) == 0
        assert s.scalar(select(func.count()).select_from(Job)) == 1
        assert s.scalar(select(RequestScope.symbol)) == "maintenance"


def test_sec_sleep_coalesces_slots_and_completed_latest_is_not_starved(isolated, monkeypatch):
    from iirp.jobs import auto_update

    instant = datetime(2026, 9, 12, 9, tzinfo=ET)
    monkeypatch.setattr(schedule, "now", lambda: instant)
    monkeypatch.setattr(auto_update, "now", lambda: instant)
    with session() as s, s.begin():
        p = s.get(CollectionStrategy, "sec")
        p.enabled = True
        s.get(Policy, 1).sec_enabled = True
        p.next_run_at = instant - timedelta(days=20)
    schedule.schedule_tick()
    with session() as s, s.begin():
        batches = s.scalars(select(Batch)).all()
        assert sorted(b.kind for b in batches) == ["sec_history", "sec_latest"]
        # ORM timestamp defaults use the real clock. Align this synthetic
        # scheduling scenario with its explicitly frozen September 12 instant.
        for batch in batches:
            batch.created_at = instant
        latest = next(b for b in batches if b.kind == "sec_latest")
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == latest.id))
        discover = Job(
            kind="sec_discover",
            title="synthetic done latest scan",
            target={"mode": "latest"},
            status="SUCCEEDED",
            idempotency_key=uuid.uuid4().hex,
        )
        history = Job(
            kind="sec_document",
            title="synthetic slow document",
            target={},
            status="RUNNING",
            idempotency_key=uuid.uuid4().hex,
        )
        s.add_all([discover, history])
        s.flush()
        s.add_all(
            [
                BatchJob(scope_id=scope.id, job_id=discover.id),
                BatchJob(scope_id=scope.id, job_id=history.id),
            ]
        )
        latest_id, scope_id = latest.id, scope.id
        old_head_id, document_id = discover.id, history.id
        s.get(CollectionStrategy, "sec").next_run_at = instant - timedelta(seconds=1)
    schedule.schedule_tick()
    with session() as s:
        assert (
            s.scalar(select(func.count()).select_from(Batch).where(Batch.kind == "sec_latest")) == 1
        )
    # A due head must proceed while the old document is still running, but
    # repeated same-day rounds share one durable automatic demand.
    instant += timedelta(seconds=31)
    schedule.schedule_tick()
    with session() as s:
        # A worker tick does not bypass the non-workday 3,600-second cadence.
        assert s.scalar(select(func.count()).select_from(Job).where(
            Job.kind == "sec_discover", Job.status == "QUEUED")) == 0
    instant += timedelta(seconds=3600 - 31)
    schedule.schedule_tick()
    with session() as s:
        assert (
            s.scalar(select(func.count()).select_from(Batch).where(Batch.kind == "sec_latest")) == 1
        )
        assert (
            s.scalar(select(func.count()).select_from(Batch).where(Batch.kind == "sec_history"))
            == 1
        )

        # The latest feed is polled from one source row, not a new head job.
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "sec_discover")) == 1
        assert s.get(Batch, latest_id).created_at == instant - timedelta(seconds=3600)
        assert s.get(BatchJob, (scope_id, old_head_id)).active
        assert s.get(BatchJob, (scope_id, document_id)).active
        assert s.get(Job, old_head_id).status == "SUCCEEDED"
        assert s.get(Job, document_id).status == "RUNNING"
    from iirp.sec import poll as sec_poll

    monkeypatch.setattr(sec_poll, "now", lambda: instant)
    assert sec_poll.poll_due()


def test_log_rotation_keeps_open_inode_and_bounds_tail(isolated, monkeypatch):
    logs = isolated.runtime / "logs"
    logs.mkdir()
    path = logs / "postgres.log"
    path.write_bytes(b"0123456789" * 30)
    monkeypatch.setattr(maintenance, "LOG_BYTES", 64)
    monkeypatch.setattr(maintenance, "_LAST_LOG_ROTATION", 0)
    inode = path.stat().st_ino
    with path.open("ab") as writer:
        maintenance.operational_log_tick()
        writer.write(b"later\n")
        writer.flush()
    assert path.stat().st_ino == inode
    assert path.read_bytes() == b"later\n"
    assert (logs / "postgres.log.1").stat().st_size == 64


def slow_dump(isolated, monkeypatch):
    """Block a real backup at pg_dump while keeping actual snapshot/lease code."""
    marker, release = isolated.runtime / "dump-processes.json", isolated.runtime / "release-dump"
    binaries = isolated.runtime / "pg-bin"
    binaries.mkdir()
    dump = binaries / "pg_dump"
    dump.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys,time\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text(json.dumps({{'dump_pid':os.getpid(),'backup_pid':os.getppid()}}))\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.1)\n"
        f"os.execv({str(isolated.backup.PGBIN / 'pg_dump')!r}, ['pg_dump',*sys.argv[1:]])\n"
    )
    dump.chmod(0o700)
    (binaries / "pg_restore").symlink_to(isolated.backup.PGBIN / "pg_restore")
    monkeypatch.setenv("IIRP_PG_BIN", str(binaries))
    return marker, release


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("bounded maintenance wait expired")


def process_live(pid):
    if sys.platform.startswith("linux"):
        # Slim containers expose procfs but need not install the ps executable.
        # Zombies have exited and cannot produce or publish further work.
        try:
            fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            return False
        return fields[0] != "Z"
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


@pytest.mark.parametrize(
    "action", ["renew", "pause", "cancel", "expired", "token", "parent_stop", "child_sigkill"]
)
@pytest.mark.slow
def test_real_long_backup_control_and_group_reaping(isolated, monkeypatch, action):
    marker, release = slow_dump(isolated, monkeypatch)
    job = make_job()
    stopped = threading.Event()
    errors = []

    def run():
        try:
            maintenance.execute_maintenance(job, stopping=stopped.is_set)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    processes = None
    try:
        wait_until(marker.exists)
        processes = json.loads(marker.read_text())
        if action == "renew":
            with session() as s, s.begin():
                s.get(Job, job.id).lease_until = now() + timedelta(seconds=1)
            time.sleep(1.3)
            with session() as s:
                assert s.get(Job, job.id).lease_until > now() + timedelta(seconds=20)
            release.touch()
        elif action == "parent_stop":
            stopped.set()
        elif action == "child_sigkill":
            os.kill(processes["backup_pid"], signal.SIGKILL)
        else:
            with session() as s, s.begin():
                current = s.get(Job, job.id)
                if action == "expired":
                    current.lease_until = now() - timedelta(seconds=1)
                elif action == "token":
                    current.lease_token = str(uuid.uuid4())
                else:
                    current.requested_action = action
                    current.control_version += 1
                    current.status = "PAUSE_REQUESTED" if action == "pause" else "CANCEL_REQUESTED"
        if action in {"pause", "cancel"}:
            expected = {"pause": "PAUSED", "cancel": "CANCELLED"}[action]

            def acknowledged():
                with session() as s:
                    return s.get(Job, job.id).status == expected

            # Prompt control remains bounded independently of scratch fsync.
            wait_until(acknowledged)
        # Success performs a real restore and checkpoint; cancellation still
        # retains the original short producer/cleanup completion budget.
        thread.join(
            timeout=isolated.backup.SCRATCH_DATABASE_DROP_SECONDS + 20 if action == "renew" else 12
        )
        assert not thread.is_alive()
        wait_until(lambda: all(not process_live(pid) for pid in processes.values()))
        with session() as s:
            result = s.get(Job, job.id)
            if action == "renew":
                assert not errors, [(type(exc).__name__, str(exc)) for exc in errors]
                assert result.status == "SUCCEEDED"
                assert s.scalar(select(func.count()).select_from(MaintenanceRun)) == 1
                assert not errors
            else:
                assert result.status != "SUCCEEDED"
                assert result.status == {"pause": "PAUSED", "cancel": "CANCELLED"}.get(
                    action, "RUNNING"
                )
                assert s.scalar(select(func.count()).select_from(MaintenanceRun)) == 0
        assert not list((isolated.runtime / "backups").glob("*/database.dump.partial"))
        if action != "renew":
            assert not isolated.backup.completed_backups()
            reports = [
                json.loads(p.read_text())
                for p in (isolated.runtime / "maintenance-operations").glob("*.json")
            ]
            assert any(r["status"] == "ROLLED_BACK" for r in reports)
    finally:
        stopped.set()
        release.touch()
        if processes:
            for pid in processes.values():
                if process_live(pid):
                    os.kill(pid, signal.SIGKILL)
        thread.join(timeout=5)


def test_sigkill_parent_is_detected_by_child_during_blocking_dump(isolated, monkeypatch):
    marker, release = slow_dump(isolated, monkeypatch)
    job = make_job()
    code = (
        "from iirp.db import session\nfrom iirp.models import Job\n"
        "from iirp.storage.maintenance import execute_maintenance\n"
        f"with session() as s: job=s.get(Job,{job.id!r})\nexecute_maintenance(job)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    processes = None
    try:
        wait_until(marker.exists)
        processes = json.loads(marker.read_text())
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=3)
        wait_until(lambda: all(not process_live(pid) for pid in processes.values()))
        reports = [
            json.loads(p.read_text())
            for p in (isolated.runtime / "maintenance-operations").glob("*.json")
        ]
        assert any(r["status"] == "ROLLED_BACK" for r in reports)
        assert not isolated.backup.completed_backups()
        with session() as s:
            assert s.get(Job, job.id).status == "RUNNING"
    finally:
        release.touch()
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=3)
        for pid in (processes or {}).values():
            if process_live(pid):
                os.kill(pid, signal.SIGKILL)


def test_restore_rejects_fact_content_change_and_discards_own_database(isolated, monkeypatch):
    backup = isolated.backup
    seed_results(count=2)
    directory = backup.backup()
    original = backup.pg_command
    names = []

    def changed(tool, database, *args):
        original(tool, database, *args)
        names.append(database)
        with backup.connection(database) as conn:
            conn.execute("UPDATE analysis_result SET data='{}'::jsonb")

    monkeypatch.setattr(backup, "pg_command", changed)
    with pytest.raises(ValueError, match="事实内容哈希"):
        backup.restore_verify(directory)
    assert (
        isolated.admin.execute(
            "SELECT datname FROM pg_database WHERE datname=ANY(%s)", (names,)
        ).fetchall()
        == []
    )
    assert not list((isolated.runtime / "restores").iterdir())
    assert (directory / "manifest.json").is_file()
    assert json.loads((directory / "manifest.json").read_text())["restore_verified_at"] is None


def test_object_copy_interruption_discards_only_own_partial_and_keeps_original(
    isolated, monkeypatch
):
    backup = isolated.backup
    source = save_object(b"synthetic copy boundary evidence", "text/plain")
    with session() as s, s.begin():
        s.add(SourceObject(**source))
    original = backup.shutil.copy2

    def broken(src, dest):
        original(src, dest)
        raise OSError("injected after source copy")

    monkeypatch.setattr(backup.shutil, "copy2", broken)
    with pytest.raises(OSError, match="injected"):
        backup.backup()
    assert (
        isolated.runtime / source["relative_path"]
    ).read_bytes() == b"synthetic copy boundary evidence"
    assert not list((isolated.runtime / "backups").rglob("*.partial-*"))
    assert not list((isolated.runtime / "backups").glob(".pool-staging-*"))
    journals = [
        json.loads(path.read_text())
        for path in (isolated.runtime / "maintenance-operations").glob("*.json")
    ]
    assert all(record["status"] == "ROLLED_BACK" for record in journals)
    assert all(
        entry.get("released")
        for record in journals
        for entry in record["resources"]
        if entry["kind"] == "pool_staging_directory"
    )
    assert not backup.completed_backups()


def test_legacy_partial_journal_recovery_preserves_other_operations_and_published_pool(isolated):
    backup = isolated.backup
    operation_id = uuid.uuid4().hex
    pool = isolated.runtime / "backups" / "object-pool" / "objects" / "aa"
    pool.mkdir(parents=True)
    own = pool / ("synthetic.partial-" + operation_id)
    other = pool / ("synthetic.partial-" + uuid.uuid4().hex)
    published = pool / "synthetic-published"
    for path in (own, other, published):
        path.write_bytes(b"synthetic preserved source")
    backup._journal_write(
        {
            "id": operation_id,
            "source_database": isolated.database,
            "status": "RUNNING",
            "resources": [{"kind": "pool_partial", "value": str(own.resolve())}],
        }
    )
    result = backup.cleanup_owned_operation(operation_id)
    assert not result["failures"]
    assert not own.exists()
    assert other.read_bytes() == published.read_bytes() == b"synthetic preserved source"
    assert backup.cleanup_owned_operation(operation_id)["removed"] == []


def test_rollback_is_idempotent_and_never_redeletes_recreated_scratch(isolated, monkeypatch):
    backup = isolated.backup
    directory = backup.backup()
    names = []

    def broken(_tool, database, *_args):
        names.append(database)
        raise RuntimeError("injected")

    monkeypatch.setattr(backup, "pg_command", broken)
    with pytest.raises(RuntimeError):
        backup.restore_verify(directory)
    report = next(
        json.loads(p.read_text())
        for p in (isolated.runtime / "maintenance-operations").glob("*.json")
        if json.loads(p.read_text())["status"] == "ROLLED_BACK"
    )
    scratch = isolated.runtime / "restores" / names[0]
    scratch.mkdir()
    (scratch / "new-user-file").write_text("preserve")
    assert backup.cleanup_owned_operation(report["id"])["removed"] == []
    assert (scratch / "new-user-file").read_text() == "preserve"


@pytest.mark.slow
def test_table_hash_streams_large_synthetic_table_with_bounded_python_memory(isolated):
    import tracemalloc

    backup = isolated.backup
    with backup.connection() as conn:
        conn.execute("CREATE TABLE synthetic_hash_rows(id int PRIMARY KEY, payload text NOT NULL)")
        conn.execute(
            "INSERT INTO synthetic_hash_rows SELECT n, repeat('synthetic-',100) FROM generate_series(1,100000) n"
        )
    tracemalloc.start()
    started = time.monotonic()
    try:
        with backup.connection() as conn:
            original_fraction = conn.execute("SHOW cursor_tuple_fraction").fetchone()[0]
            fingerprints = backup.table_fingerprints(conn)
            assert conn.execute("SHOW cursor_tuple_fraction").fetchone()[0] == original_fraction
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    duration = time.monotonic() - started
    assert fingerprints["synthetic_hash_rows"]["count"] == 100000
    # Independent known rows and ordering, without the streaming SQL implementation.
    expected = hashlib.sha256()
    for identifier in range(1, 100001):
        payload = json.dumps({"id": identifier, "payload": "synthetic-" * 100}).encode()
        expected.update(len(payload).to_bytes(8, "big"))
        expected.update(payload)
    assert fingerprints["synthetic_hash_rows"]["sha256"] == expected.hexdigest()
    assert peak < 8 * 1024**2
    (isolated.runtime / "synthetic-hash-measurement.json").write_text(
        json.dumps({"rows": 100000, "python_peak_bytes": peak, "seconds": duration})
    )
