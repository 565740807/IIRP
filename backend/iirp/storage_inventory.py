"""Persistent, coalesced storage inventory; HTTP reads never walk object trees."""

import fcntl
import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import func, select, text

from iirp.business_models import MaintenanceRun
from iirp.capacity import capacity_sufficient, temporary_bytes
from iirp.config import settings
from iirp.db import session
from iirp.models import SourceObject

FRESH_SECONDS = 600
RETRY_SECONDS = 60
RUNNING_SECONDS = 300
_THREAD_LOCK = threading.Lock()
_THREAD = None


def _file():
    return settings().runtime_dir / "storage-inventory.json"


def _read():
    try:
        return json.loads(_file().read_text())
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _write(value):
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.partial")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _seconds_since(value):
    if not value:
        return float("inf")
    try:
        return max(0.0, time.time() - datetime.fromisoformat(value).timestamp())
    except (TypeError, ValueError):
        return float("inf")


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _collect():
    # One SQL session after the tree walk. Costs are recorded per segment.
    from iirp.maintenance import _path_usage

    started = time.monotonic()
    totals, paths, durations = {}, {}, {}
    for label, relative in (
        ("evidence", "objects"), ("cache", "provider-cache"),
        ("logs", "logs"), ("backups", "backups"), ("restores", "restores"),
    ):
        segment = time.monotonic()
        usage = _path_usage(settings().runtime_dir / relative)
        paths[label] = usage
        totals[label] = usage["logical_bytes"]
        durations[label] = round(time.monotonic() - segment, 3)
    segment = time.monotonic()
    with session() as s:
        totals["database"] = s.scalar(text("SELECT pg_database_size(current_database())"))
        totals["objects"] = s.scalar(select(func.count()).select_from(SourceObject))
        totals["bytes"] = s.scalar(select(func.coalesce(func.sum(SourceObject.byte_size), 0)))
        from iirp.result_storage import RESULT_BYTES_SQL

        totals["analysis_cache_bytes"] = s.scalar(text(RESULT_BYTES_SQL))
        recent = s.scalars(select(MaintenanceRun).order_by(MaintenanceRun.created_at.desc()).limit(10)).all()
        totals["recent_maintenance"] = [
            {"kind": x.kind, "status": x.status, "created_at": x.created_at.isoformat(), "details": x.details}
            for x in recent
        ]
    durations["sql"] = round(time.monotonic() - segment, 3)
    totals["paths"] = paths
    totals["backup_estimated_temporary_bytes"] = temporary_bytes(
        "backup", totals["database"], totals["bytes"], totals["objects"]
    )
    totals["restore_estimated_temporary_bytes"] = temporary_bytes(
        "restore", totals["database"], totals["bytes"], totals["objects"]
    )
    totals["estimate_scope"] = "当前主库和已登记来源对象；备份估算假设对象池均未复用，恢复执行以选定manifest重算"
    totals["segment_seconds"] = durations
    totals["total_seconds"] = round(time.monotonic() - started, 3)
    return totals


def _refresh():
    lock = settings().runtime_dir / "storage-inventory.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        old = _read()
        started_at = _utc()
        _write({**old, "status": "refreshing", "attempted_at": started_at, "error": None})
        try:
            totals = _collect()
            _write({"status": "fresh", "attempted_at": started_at,
                    "measured_at": _utc(), "error": None, "totals": totals})
        except Exception as exc:
            _write({**old, "status": "failed", "attempted_at": started_at,
                    "error": f"{type(exc).__name__}: {str(exc)[:240]}"})


def _start_refresh():
    global _THREAD
    with _THREAD_LOCK:
        if _THREAD is None or not _THREAD.is_alive():
            _THREAD = threading.Thread(target=_refresh, name="storage-inventory", daemon=True)
            _THREAD.start()


def storage_state():
    record = _read()
    measured_age = _seconds_since(record.get("measured_at"))
    attempt_age = _seconds_since(record.get("attempted_at"))
    status = record.get("status", "unmeasured")
    if status == "fresh" and measured_age >= FRESH_SECONDS:
        status = "stale"
    if status == "refreshing" and attempt_age >= RUNNING_SECONDS:
        status = "stale" if record.get("measured_at") else "unmeasured"
    should_refresh = (status in {"unmeasured", "stale"} or
                      status == "failed" and attempt_age >= RETRY_SECONDS)
    if should_refresh:
        _start_refresh()
        if status == "unmeasured":
            status = "refreshing"
    fresh = status == "fresh"
    runtime = settings().runtime_dir
    disk_error = None
    try:
        free = shutil.disk_usage(runtime).free
    except OSError as exc:
        free = None
        disk_error = f"{type(exc).__name__}: {str(exc)[:240]}"
    reserve = settings().min_free_bytes
    totals = dict(record.get("totals") or {})
    backup_required = totals.get("backup_estimated_temporary_bytes") if fresh else None
    restore_required = totals.get("restore_estimated_temporary_bytes") if fresh else None
    totals.update({
        "inventory_status": status,
        "inventory_measured_at": record.get("measured_at"),
        "inventory_attempted_at": record.get("attempted_at"),
        "inventory_error": disk_error or record.get("error"),
        "inventory_age_seconds": round(measured_age, 1) if measured_age != float("inf") else None,
        "free_bytes": free,
        "min_free_bytes": reserve,
        "disk_pressure": free < reserve if free is not None else None,
        "backup_capacity_sufficient": capacity_sufficient(free, backup_required, reserve),
        "restore_capacity_sufficient": capacity_sufficient(free, restore_required, reserve),
    })
    return totals
