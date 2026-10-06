"""Persistent, coalesced storage inventory; HTTP reads never walk object trees.

The routine summary comes from records that already exist: source sizes from
``source_object``, backup sizes from each backup manifest (cached by file
identity, so an unchanged manifest is parsed once), restore copies from their
reports and database sizes from PostgreSQL. Walking the object, backup and
restore trees (about 1.3 million files on a rotational disk) is an explicit
exact check: started from the data page, or by the worker at most once a day.
"""

import fcntl
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import func, select, text

from iirp.config import settings
from iirp.db import session
from iirp.models import MaintenanceRun, SourceObject
from iirp.storage.capacity import capacity_sufficient, temporary_bytes

FRESH_SECONDS = 600
RETRY_SECONDS = 60
RUNNING_SECONDS = 300
EXACT_INTERVAL_SECONDS = 86400
EXACT_RUNNING_SECONDS = 3 * 3600
_THREAD_LOCK = threading.Lock()
_THREADS = {}


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
    temporary = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.partial")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _update(**values):
    # Summary and exact check run in separate threads/processes and each owns
    # its own keys; re-read under a short lock before writing to keep the other's.
    lock = settings().runtime_dir / "storage-inventory.write.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        _write({**_read(), **values})


def _seconds_since(value):
    if not value:
        return float("inf")
    try:
        return max(0.0, time.time() - datetime.fromisoformat(value).timestamp())
    except (TypeError, ValueError):
        return float("inf")


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _small_directory(path):
    from iirp.storage.maintenance import _path_usage

    return _path_usage(path)


_TOP_LEVEL = re.compile(r'^  "(completed_at|restore_verified_at)": (?:"([^"]*)"|null)')
_BYTE_SIZE = re.compile(r'"byte_size": (\d+)')


def _manifest_totals(path):
    """Stream an indented manifest (up to ~150 MB) line by line, never json.loads it.

    Loading the whole object list would need over 1 GB in a 768 MiB web container.
    """
    totals = {"completed_at": None, "restore_verified_at": None, "object_count": 0, "object_bytes": 0}
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if match := _BYTE_SIZE.search(line):
                totals["object_count"] += 1
                totals["object_bytes"] += int(match.group(1))
            elif match := _TOP_LEVEL.match(line):
                totals[match.group(1)] = match.group(2)
    return totals


def _backups(cached):
    """Per-backup sizes from manifests; parse a manifest only when it changed."""
    root = settings().runtime_dir / "backups"
    entries, result = {}, []
    if root.exists():
        for directory in sorted(root.iterdir()):
            manifest = directory / "manifest.json"
            if (directory.name == "object-pool" or directory.name.startswith(".")
                    or directory.is_symlink() or not directory.is_dir()
                    or manifest.is_symlink() or not manifest.is_file()):
                continue
            stat = manifest.stat()
            identity = [stat.st_mtime_ns, stat.st_size]
            entry = cached.get(directory.name)
            if not entry or entry.get("identity") != identity:
                try:
                    entry = {"identity": identity, **_manifest_totals(manifest)}
                except (OSError, ValueError):
                    continue
            dump = directory / "database.dump"
            entry = {**entry, "dump_bytes": dump.stat().st_size if dump.is_file() else 0}
            entries[directory.name] = entry
            result.append({"name": directory.name, **{k: v for k, v in entry.items() if k != "identity"}})
    # Backups hard-link one immutable object pool, so their object bytes overlap.
    # The newest manifest approximates the pool; older dumps are separate files.
    newest = max(result, key=lambda item: item["name"], default=None)
    estimate = sum(item["dump_bytes"] for item in result) + (newest["object_bytes"] if newest else 0)
    return entries, result, estimate


def _restores():
    root = settings().runtime_dir / "restores"
    result = []
    if root.exists():
        for directory in sorted(root.iterdir()):
            if directory.is_symlink() or not directory.is_dir():
                continue
            report = {}
            try:
                report = json.loads((directory / "restore-report.json").read_text())
            except (OSError, ValueError):
                pass
            result.append({"name": directory.name, "verified_at": report.get("verified_at"),
                           "object_bytes": report.get("object_bytes")})
    return result


def _collect(cached_backups=None):
    started = time.monotonic()
    totals, paths, durations = {}, {}, {}
    for label, relative in (("cache", "provider-cache"), ("logs", "logs")):
        segment = time.monotonic()
        usage = _small_directory(settings().runtime_dir / relative)
        paths[label] = usage
        totals[label] = usage["logical_bytes"]
        durations[label] = round(time.monotonic() - segment, 3)
    segment = time.monotonic()
    backup_cache, backups, totals["backups"] = _backups(cached_backups or {})
    totals["backup_list"] = backups
    restores = _restores()
    totals["restore_list"] = restores
    totals["restores"] = sum(item["object_bytes"] or 0 for item in restores)
    durations["manifests"] = round(time.monotonic() - segment, 3)
    segment = time.monotonic()
    with session() as s:
        totals["database"] = s.scalar(text("SELECT pg_database_size(current_database())"))
        totals["database_server"] = s.scalar(text("SELECT sum(pg_database_size(oid))::bigint FROM pg_database"))
        totals["objects"], totals["bytes"] = s.execute(
            select(func.count(), func.coalesce(func.sum(SourceObject.byte_size), 0))
        ).one()
        totals["evidence"] = totals["bytes"]
        totals["expiring_objects"], totals["expiring_bytes"] = s.execute(
            select(func.count(), func.coalesce(func.sum(SourceObject.byte_size), 0))
            .where(SourceObject.expires_at.is_not(None))
        ).one()
        from iirp.models.compressed import RESULT_BYTES_SQL

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
    totals["estimate_scope"] = (
        "原文大小取自来源记录，备份取自各自清单（共享原文池按最新清单估算），数据库取自 PostgreSQL；"
        "目录实际占用以精确核对为准。备份估算假设对象池均未复用，恢复执行以选定清单重算"
    )
    totals["segment_seconds"] = durations
    totals["total_seconds"] = round(time.monotonic() - started, 3)
    return totals, backup_cache


def _exact():
    from iirp.storage.maintenance import _path_usage

    started = time.monotonic()
    result = {}
    for label, relative in (("evidence", "objects"), ("backups", "backups"), ("restores", "restores")):
        result[label] = _path_usage(settings().runtime_dir / relative)
    result["total_seconds"] = round(time.monotonic() - started, 3)
    return result


def _locked(name, work):
    lock = settings().runtime_dir / name
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        work()


def _refresh():
    def work():
        old = _read()
        started_at = _utc()
        _update(status="refreshing", attempted_at=started_at, error=None)
        try:
            totals, backup_cache = _collect(old.get("backup_cache"))
            _update(status="fresh", attempted_at=started_at, measured_at=_utc(), error=None,
                    totals=totals, backup_cache=backup_cache)
        except Exception as exc:
            _update(status="failed", attempted_at=started_at,
                    error=f"{type(exc).__name__}: {str(exc)[:240]}")

    _locked("storage-inventory.lock", work)


def _refresh_exact():
    def work():
        started_at = _utc()
        _update(exact_status="refreshing", exact_attempted_at=started_at, exact_error=None)
        try:
            exact = _exact()
            _update(exact_status="fresh", exact_attempted_at=started_at,
                    exact_measured_at=_utc(), exact_error=None, exact=exact)
        except Exception as exc:
            _update(exact_status="failed", exact_attempted_at=started_at,
                    exact_error=f"{type(exc).__name__}: {str(exc)[:240]}")

    _locked("storage-inventory-exact.lock", work)


def _start(name, target):
    with _THREAD_LOCK:
        thread = _THREADS.get(name)
        if thread is None or not thread.is_alive():
            thread = threading.Thread(target=target, name=f"storage-{name}", daemon=True)
            _THREADS[name] = thread
            thread.start()
        return thread


def _exact_running(record):
    return (record.get("exact_status") == "refreshing"
            and _seconds_since(record.get("exact_attempted_at")) < EXACT_RUNNING_SECONDS)


def request_exact():
    """Explicit exact check from the data page; concurrent requests coalesce."""
    if not _exact_running(_read()):
        _start("exact", _refresh_exact)
    return storage_state()


def exact_tick():
    """Worker: at most one background exact check per day, outside US trading hours."""
    from zoneinfo import ZoneInfo

    record = _read()
    if _exact_running(record) or _seconds_since(record.get("exact_attempted_at")) < EXACT_INTERVAL_SECONDS:
        return None
    if not 0 <= datetime.now(ZoneInfo("America/New_York")).hour < 5:
        return None
    return _start("exact", _refresh_exact)


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
        _start("summary", _refresh)
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
    exact = record.get("exact") or {}
    exact_status = record.get("exact_status", "unmeasured")
    if exact_status == "refreshing" and not _exact_running(record):
        exact_status = "failed"
    if exact:
        # Allocated directory bytes only come from an exact check.
        totals["paths"] = {**(totals.get("paths") or {}), **{k: v for k, v in exact.items() if isinstance(v, dict)}}
    totals.update({
        "inventory_status": status,
        "inventory_measured_at": record.get("measured_at"),
        "inventory_attempted_at": record.get("attempted_at"),
        "inventory_error": disk_error or record.get("error"),
        "inventory_age_seconds": round(measured_age, 1) if measured_age != float("inf") else None,
        "exact_status": exact_status,
        "exact_measured_at": record.get("exact_measured_at"),
        "exact_error": record.get("exact_error"),
        "exact_seconds": exact.get("total_seconds"),
        "free_bytes": free,
        "min_free_bytes": reserve,
        "disk_pressure": free < reserve if free is not None else None,
        "backup_capacity_sufficient": capacity_sufficient(free, backup_required, reserve),
        "restore_capacity_sufficient": capacity_sufficient(free, restore_required, reserve),
    })
    return totals
