"""Durable coalescing schedules and reference-aware, controlled maintenance."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import delete, select, text
from sqlalchemy.exc import OperationalError

from iirp.config import ROOT, settings
from iirp.db import session
from iirp.messages import UserError
from iirp.models import (
    FeedSession,
    Job,
    MaintenanceRun,
    now,
)

LOG_BYTES = 5 * 1024**2


LOG_COPIES = 5


MAINTENANCE_TIMEOUT = 3600


BACKUP_MAX_SECONDS = 6 * 3600


BACKUP_STALL_SECONDS = 15 * 60


MAINTENANCE_DB_GRACE_SECONDS = 15


# PostgreSQL DROP DATABASE can wait on checkpoint fsync. Keep scratch recovery
# bounded separately from the already-acknowledged pause/cancel control path.
SCRATCH_DATABASE_DROP_SECONDS = 30


MAINTENANCE_RECOVERY_TIMEOUT = SCRATCH_DATABASE_DROP_SECONDS + 15


_LAST_LOG_ROTATION = 0.0


def lock_analysis_references(s, *, wait=True):
    """Export/read publication and cache deletion share one transaction lock."""
    function = "pg_advisory_xact_lock" if wait else "pg_try_advisory_xact_lock"
    result = s.scalar(text(f"SELECT {function}(1229541968,1128350536)"))
    return True if wait else bool(result)


def operational_log_tick():
    """Operational log bounds remain active when all source strategies are off."""
    import fcntl

    global _LAST_LOG_ROTATION
    if time.monotonic() - _LAST_LOG_ROTATION < 60:
        return
    path = settings().runtime_dir / "log-rotation.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        rotate_logs()
        _LAST_LOG_ROTATION = time.monotonic()


def _path_usage(path):
    logical, allocated, files, seen = 0, 0, 0, set()
    if path.exists():
        for item in path.rglob("*"):
            if item.is_symlink():
                continue
            try:
                stat = item.stat()
            except FileNotFoundError:
                continue
            if not item.is_file():
                continue
            logical += stat.st_size
            files += 1
            identity = (stat.st_dev, stat.st_ino)
            if identity not in seen:
                allocated += stat.st_blocks * 512
                seen.add(identity)
    return {
        "path": str(path),
        "logical_bytes": logical,
        "allocated_bytes": allocated,
        "files": files,
    }


def storage_state():
    from iirp.storage.inventory import storage_state as cached_storage_state

    return cached_storage_state()


@contextmanager
def maintenance_lock():
    import fcntl

    path = settings().runtime_dir / "maintenance.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UserError("maintenance.busy") from None
        yield


def rotate_logs():
    """Bound redirected web/PG logs without replacing the writer's open inode.

    Copy-truncate can lose a concurrent log line; logs are operational evidence,
    never durable facts. Worker rotating-handler files are managed by that handler.
    """
    directory = settings().runtime_dir / "logs"
    details = {"rotated": [], "expired_removed": []}
    if not directory.exists():
        return details
    cutoff = now().timestamp() - 90 * 86400
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        if path.name in {"web.log", "postgres.log"} and path.stat().st_size > LOG_BYTES:
            for index in range(LOG_COPIES, 1, -1):
                previous = path.with_name(f"{path.name}.{index - 1}")
                if previous.exists() and not previous.is_symlink():
                    previous.replace(path.with_name(f"{path.name}.{index}"))
            # Keep a bounded tail even after a long sleep/maintenance outage.
            with path.open("rb+") as source:
                source.seek(max(0, path.stat().st_size - LOG_BYTES))
                tail = source.read(LOG_BYTES)
                target = path.with_name(f"{path.name}.1")
                if target.is_symlink():
                    continue
                target.write_bytes(tail)
                source.truncate(0)
            details["rotated"].append(path.name)
        elif path.name.rsplit(".", 1)[-1].isdigit() and path.stat().st_mtime < cutoff:
            path.unlink()
            details["expired_removed"].append(path.name)
    return details


def _cache_bytes(s):
    from iirp.models.compressed import RESULT_BYTES_SQL

    return s.scalar(text(RESULT_BYTES_SQL))


def cleanup(job=None):
    from iirp.jobs.queue import fenced
    from iirp.market.cache import expire_price_cache

    with maintenance_lock():
        # Research results live as long as their price caches; the worker
        # also runs this every ten minutes.
        details = {"reading_sessions_removed": 0, **expire_price_cache()}
        while True:
            expired_count = 0

            def expire(s, _current=None):
                nonlocal expired_count
                ids = list(
                    s.scalars(
                        # One entity snapshot can contain a very large immutable
                        # index. Release the control transaction after each one.
                        select(FeedSession.id).where(FeedSession.expires_at < now()).limit(1)
                    )
                )
                expired_count = len(ids)
                if ids:
                    s.execute(delete(FeedSession).where(FeedSession.id.in_(ids)))

            if job is None:
                with session() as s, s.begin():
                    expire(s)
            elif not fenced(job, acknowledge_control=False, business_write=expire):
                return None
            details["reading_sessions_removed"] += expired_count
            if not expired_count:
                break
        with session() as s:
            details["analysis_bytes_remaining"] = _cache_bytes(s)
        details["logs"] = rotate_logs()

        def finish(s, _current=None):
            s.add(MaintenanceRun(kind="cache_cleanup", status="SUCCEEDED", details=details))

        if job is None:
            with session() as s, s.begin():
                finish(s)
        elif not fenced(job, status="SUCCEEDED", done=1, result=details, business_write=finish):
            return None
        return {"items": [], "data": details}


def maintenance_timeout(started, operation_id=None):
    """Backup progress may extend work, with both stall and absolute bounds.

    Progress is written only after real table/object work. Lease renewal alone
    cannot keep a stuck backup alive. Cleanup retains its original one-hour cap.
    """
    elapsed = time.monotonic() - started
    if operation_id is None:
        return elapsed > MAINTENANCE_TIMEOUT
    if elapsed > BACKUP_MAX_SECONDS:
        return True
    last_progress_age = elapsed
    path = settings().runtime_dir / "maintenance-operations" / (operation_id + ".json")
    try:
        record = json.loads(path.read_text())
        if record.get("id") == operation_id:
            updated = datetime.fromisoformat(record.get("progress", {}).get("updated_at", ""))
            last_progress_age = max(0, (now() - updated).total_seconds())
    except (OSError, ValueError, TypeError):
        pass
    return last_progress_age > BACKUP_STALL_SECONDS


def maintenance_fence_with_grace(fence, last_success):
    """One transient DB lock timeout cannot discard an hour of verified backup work.

    Publication still uses its own strict lease guard. A prolonged inability to
    confirm the parent lease remains bounded to less than half its 30s lifetime.
    """
    try:
        valid = fence()
    except OperationalError:
        if time.monotonic() - last_success >= MAINTENANCE_DB_GRACE_SECONDS:
            raise
        return True, last_success
    return valid, time.monotonic()


def parent_watchdog(descriptor):
    """Orphaned maintenance groups stop even during blocking dump/copy operations."""
    parent_pid = os.getppid()
    stopped = threading.Event()
    started = time.monotonic()
    last_confirmed = started

    def supervise():
        nonlocal last_confirmed
        while not stopped.wait(0.5):
            invalid = os.getppid() != parent_pid or maintenance_timeout(started, descriptor.get("operation_id"))
            if not invalid:
                try:
                    with session() as s:
                        current = s.get(Job, descriptor["id"])
                        invalid = (
                            not current
                            or current.lease_token != descriptor["lease_token"]
                            or current.control_version != descriptor["control_version"]
                            or not current.lease_until
                            or current.lease_until <= now()
                            or current.requested_action
                            or current.status != "RUNNING"
                        )
                    last_confirmed = time.monotonic()
                except Exception:
                    invalid = time.monotonic() - last_confirmed >= MAINTENANCE_DB_GRACE_SECONDS
            if invalid:
                # A new process session is an execute_maintenance contract. A
                # direct Python call never starts this watchdog automatically.
                if os.getpgrp() == os.getpid():
                    os.killpg(os.getpid(), signal.SIGTERM)
                else:
                    os.kill(os.getpid(), signal.SIGTERM)
                return

    threading.Thread(target=supervise, name="maintenance-parent-watchdog", daemon=True).start()
    return stopped


def execute_maintenance(job, stopping=lambda: False):
    """One bounded subprocess; parent heartbeats/control remain independent."""
    from iirp.jobs.queue import fenced

    if not fenced(job, acknowledge_control=False):
        fenced(job)
        return None
    descriptor = {
        "id": job.id,
        "lease_token": job.lease_token,
        "control_version": job.control_version,
    }
    operation_id = uuid.uuid4().hex
    command = (
        [sys.executable, "-m", "iirp.storage.maintenance", "--job", json.dumps(descriptor)]
        if job.kind == "maintenance_clean"
        else [
            sys.executable,
            str(ROOT / "scripts/backup.py"),
            "managed",
            "--job",
            json.dumps(descriptor),
            "--operation",
            operation_id,
        ]
    )
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(
            command,
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT / "backend"),
                "IIRP_RUNTIME_DIR": str(settings().runtime_dir),
                "IIRP_MIN_FREE_BYTES": str(settings().min_free_bytes),
            },
            stdout=output,
            stderr=errors,
            start_new_session=True,
        )
        started = time.monotonic()
        last_fence = started
        result = None
        try:
            while proc.poll() is None:
                if stopping():
                    return None
                valid, last_fence = maintenance_fence_with_grace(
                    lambda: fenced(job, acknowledge_control=False), last_fence
                )
                if not valid:
                    return None
                if maintenance_timeout(started, operation_id if job.kind == "maintenance_backup" else None):
                    raise TimeoutError("maintenance stalled or exceeded its total time limit; "
                                       "completed backups and the unfinished range are kept")
                time.sleep(0.5)
            if proc.returncode:
                if stopping() or not fenced(job, acknowledge_control=False):
                    return None
                # Keep bounded diagnostics with this operation's ignored runtime
                # journal; never embed subprocess output or credentials in UI.
                failure = (
                    settings().runtime_dir
                    / "maintenance-operations"
                    / (operation_id + ".child-error.log")
                )
                failure.parent.mkdir(parents=True, exist_ok=True)
                errors.seek(0)
                failure.write_bytes(errors.read(65536))
                raise RuntimeError(
                    "scheduled backup or restore check failed; unverified backups were not pruned; "
                    "operation " + operation_id
                )
            output.seek(0)
            lines = output.read(2 * 1024**2).decode().strip().splitlines()
            result = json.loads(lines[-1])
        finally:
            if proc.poll() is None or proc.returncode:
                # The leader may already have died while pg_dump/pg_restore
                # still owns the process group. Reap that group before rollback.
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=2)
            # Confirm user control as soon as no producer can still run. Private
            # rollback can take longer and must not delay PAUSED/CANCELLED.
            fenced(job)
            if job.kind == "maintenance_backup":
                # The process group is gone before cleanup. Recover only the
                # operation ID we generated, never scan other restores/backups.
                recovery = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts/backup.py"),
                        "recover",
                        "--operation",
                        operation_id,
                    ],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "PYTHONPATH": str(ROOT / "backend"),
                        "IIRP_RUNTIME_DIR": str(settings().runtime_dir),
                        "IIRP_MIN_FREE_BYTES": str(settings().min_free_bytes),
                    },
                    capture_output=True,
                    timeout=MAINTENANCE_RECOVERY_TIMEOUT,
                    check=False,
                )
                if recovery.returncode:
                    failure = (
                        settings().runtime_dir
                        / "maintenance-operations"
                        / (operation_id + ".recovery-error.log")
                    )
                    failure.parent.mkdir(parents=True, exist_ok=True)
                    failure.write_bytes(recovery.stderr[-65536:])
                    raise RuntimeError(
                        "maintenance child stopped; cleaning up its temporary resources failed; "
                        "operation " + operation_id
                    )
            fenced(job)
        if job.kind == "maintenance_clean":
            return result
        if result is not None:

            def commit(s, current):
                s.add(MaintenanceRun(kind="backup", status="SUCCEEDED", details=result))

            return fenced(job, status="SUCCEEDED", done=1, result=result, business_write=commit)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    descriptor = json.loads(args.job)
    with session() as s:
        current = s.get(Job, descriptor["id"])
    if (
        current is None
        or current.lease_token != descriptor["lease_token"]
        or current.control_version != descriptor["control_version"]
    ):
        raise SystemExit(2)
    stopped = parent_watchdog(descriptor)
    try:
        print(json.dumps(cleanup(current), ensure_ascii=False))
    finally:
        stopped.set()
