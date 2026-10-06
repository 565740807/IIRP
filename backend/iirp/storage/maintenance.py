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
from datetime import datetime, timedelta

from sqlalchemy import delete, select, text
from sqlalchemy.exc import OperationalError

from iirp.analysis.calendar import ET, sessions
from iirp.config import ROOT, settings
from iirp.db import session
from iirp.jobs.providers import sec_configured
from iirp.models import (
    Batch,
    CollectionStrategy,
    FeedSession,
    Job,
    MaintenanceRun,
    RequestReceipt,
    RequestScope,
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
            raise ValueError("备份、恢复或清理正在进行，稍后重试") from None
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
        # Research results live as long as their price caches (D14); the worker
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


def sec_workday(day):
    from pandas.tseries.holiday import USFederalHolidayCalendar

    return day.weekday() < 5 and not len(USFederalHolidayCalendar().holidays(start=day, end=day))


def sec_poll_seconds(stamp):
    current = stamp.astimezone(ET)
    if not sec_workday(current.date()):
        return 3600
    minute = current.hour * 60 + current.minute
    if not 360 <= minute < 1320:
        return 1800
    if not sessions(current.date(), current.date()) or minute < 570:
        return 120
    return 60


def _has_running(s, key, kind=None):
    query = select(Batch.id).where(
        Batch.policy_key == key,
        Batch.requested_action.is_(None),
        Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT")),
    )
    if kind:
        query = query.where(Batch.kind == kind)
    return s.scalar(query.limit(1)) is not None


def _maintenance_batch(s, policy, request_id):
    from iirp.jobs.lifecycle import add_job, digest

    params = {
        "kind": "maintenance",
        "purpose": "每日一致备份" if policy == "backup" else "派生缓存与日志维护",
    }
    key = digest(params)
    batch = Batch(
        request_id=request_id,
        scope_key=key,
        kind="maintenance",
        title=params["purpose"],
        params=params,
        trigger="automatic",
        policy_key=policy,
    )
    s.add(batch)
    s.flush()
    s.add(RequestReceipt(request_id=request_id, batch_id=batch.id, scope_key=key))
    scope = RequestScope(batch_id=batch.id, symbol="维护")
    s.add(scope)
    s.flush()
    add_job(
        s,
        scope,
        "maintenance_backup" if policy == "backup" else "maintenance_clean",
        {"round": request_id},
        50,
    )


def _sec_schedule(s, policy, current, request_id):
    from datetime import date

    from iirp.insider.facts import _sec_recent_days
    from iirp.jobs.auto_update import ensure_fresh_in_session
    from iirp.jobs.lifecycle import _create, product_preferences, scope_range
    from iirp.models import Preferences

    ensure_fresh_in_session(s, {"sources": ["sec"], "reason": "scheduler"})
    saved = s.get(Preferences, 1)
    preferences = {**product_preferences(), **(saved.values if saved else {})}
    options = dict(policy.options)
    policy.next_run_at = current + timedelta(seconds=sec_poll_seconds(current))
    # A scheduled range is not a completed range. Advance the frontier only after
    # its whole manifest and documents have committed; failed/paused work stays visible.
    pending_id = options.get("history_pending_batch")
    pending = s.get(Batch, pending_id) if pending_id else None
    if pending and pending.status == "SUCCEEDED":
        start, end = date.fromisoformat(pending.params["start_date"]), date.fromisoformat(pending.params["end_date"])
        old_start, old_end = options.get("history_start"), options.get("history_end")
        options["history_start"] = str(min(start, date.fromisoformat(old_start))) if old_start else str(start)
        options["history_end"] = str(max(end, date.fromisoformat(old_end))) if old_end else str(end)
        if pending.params.get("intent") == "refresh":
            options["sec_weekly"] = pending.created_at.astimezone(ET).strftime("%G-W%V")
        options.pop("history_pending_batch", None)
    elif pending and pending.status != "CANCELLED":
        policy.options = options
        return
    else:
        options.pop("history_pending_batch", None)
    if not preferences.get("automatic_history", True):
        policy.options = options
        return
    existing = s.scalar(select(Batch).where(
        Batch.kind == "sec_history", Batch.policy_key == "sec", Batch.trigger == "automatic",
        Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT", "PAUSED", "PAUSE_REQUESTED", "PARTIAL", "FAILED")),
    ).order_by(Batch.created_at.desc()).limit(1))
    if existing:
        policy.options = {**options, "history_pending_batch": existing.id}
        return
    first, _ = scope_range({"kind": "sec_history", "history_months": preferences["history_months"]})
    end = _sec_recent_days(current.date() - timedelta(days=1), 1)[0]
    previous_first, previous_end = options.get("history_start"), options.get("history_end")
    start, target_end, intent = first, end, "fetch"
    weekly_key = current.strftime("%G-W%V")
    due = not previous_first
    if previous_end and end > date.fromisoformat(previous_end):
        # Recent missing days precede any earlier extension requested in preferences.
        start = max(first, date.fromisoformat(previous_end) + timedelta(days=1))
        due = True
    elif previous_first and first < date.fromisoformat(previous_first):
        target_end = min(end, date.fromisoformat(previous_first) - timedelta(days=1))
        due = True
    elif options.get("sec_weekly") != weekly_key:
        due, intent = True, "refresh"
    if due and start <= target_end:
        batch, _ = _create(s, {
            "kind": "sec_history", "request_id": request_id + ":history",
            "purpose": "有界历史回补" if intent == "fetch" else "范围内周度核对",
            "start_date": str(start), "end_date": str(target_end),
            "history_months": preferences["history_months"], "intent": intent,
        }, trigger="automatic", policy_key="sec")
        options["history_pending_batch"] = batch.id
        if not previous_first:
            options["sec_weekly"] = weekly_key
    policy.options = {**policy.options, **options}


def schedule_tick():
    from iirp.jobs.auto_update import ensure_fresh_in_session
    from iirp.jobs.lifecycle import defaults

    with session() as s, s.begin():
        defaults(s)
        ensure_fresh_in_session(s, {"sources": ["market"], "reason": "scheduler"})
        current = now().astimezone(ET)
        strategies = s.scalars(
            select(CollectionStrategy)
            .where(CollectionStrategy.enabled.is_(True), CollectionStrategy.next_run_at <= current)
            .with_for_update(skip_locked=True)
        ).all()
        for policy in strategies:
            request_id = f"auto:{policy.key}:{uuid.uuid4()}"
            if policy.key == "sec" and not sec_configured():
                # Stay enabled but idle: no SEC history round without a real contact.
                policy.next_run_at = current + timedelta(minutes=5)
                continue
            if policy.key == "sec":
                _sec_schedule(s, policy, current, request_id)
            elif policy.key == "market":
                # Prices are a 24-hour cache fetched on demand (D14); home quotes
                # refresh while shown. Nothing is scheduled for this key.
                policy.next_run_at = None
            elif policy.key in ("backup", "maintenance"):
                if not _has_running(s, policy.key):
                    _maintenance_batch(s, policy.key, request_id)
                due = current.replace(
                    hour=3 if policy.key == "backup" else 4, minute=30, second=0, microsecond=0
                )
                policy.next_run_at = due if due > current else due + timedelta(days=1)
            policy.last_run_at = current


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
                    raise TimeoutError("维护超过进度停滞或总时限；已保留已完成备份和未完成范围")
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
                    "定时备份或恢复核对失败，未执行未验证备份清理；操作 " + operation_id
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
                        "维护子进程已停止；本轮临时资源回收失败，操作 " + operation_id
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
