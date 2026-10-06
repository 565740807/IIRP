"""Consistent pg_dump + same-snapshot immutable object manifest; isolated restore only."""

import argparse
import hashlib
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from iirp.capacity import capacity_sufficient, temporary_bytes
from iirp.config import settings
from iirp.maintenance import SCRATCH_DATABASE_DROP_SECONDS
from psycopg import sql
from sqlalchemy.engine import make_url

PGBIN = Path(os.environ.get("IIRP_PG_BIN", "/opt/homebrew/opt/postgresql@18/bin"))
MANAGED_JOB = None
OPERATION_ID = None
_OPERATION = None
_LAST_PROGRESS = 0.0
# Only the newest backups are kept; older ones are deleted after each backup.
KEEP_BACKUPS = 2


@contextmanager
def maintenance_lock():
    # Reentrant composition is explicit: public wrappers lock, private helpers do not.
    from iirp.maintenance import maintenance_lock as acquire

    with acquire():
        yield


def _journal_write(record):
    path = settings().runtime_dir / "maintenance-operations" / (record["id"] + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    with temporary.open("w") as stream:
        json.dump(record, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def progress(stage, completed=None, total=None, *, force=False):
    """Persist actual advancing work, never a timer-only heartbeat."""
    global _LAST_PROGRESS
    current = time.monotonic()
    if not force and current - _LAST_PROGRESS < 5:
        return
    _LAST_PROGRESS = current
    record = {"stage": stage, "completed": completed, "total": total,
              "updated_at": datetime.now(timezone.utc).isoformat()}
    if MANAGED_JOB is not None:
        with publication_guard():
            pass
    if _OPERATION is not None:
        _OPERATION["progress"] = record
        _journal_write(_OPERATION)
    print(json.dumps(record), flush=True)


def physical_offset(path):
    """Linux FIEMAP is only an ordering hint; all bytes still require SHA checks.

    Unsupported filesystems/permissions safely use path order. Never use extents
    to read device bytes: regular file IO preserves filesystem semantics.
    """
    if not sys.platform.startswith("linux"):
        return None
    import fcntl

    try:
        with path.open("rb") as stream:
            # struct fiemap (32 B), followed by one fiemap_extent (56 B).
            request = bytearray(struct.pack("QQIIII", 0, 2**64 - 1, 0, 0, 1, 0) + bytes(56))
            fcntl.ioctl(stream.fileno(), 0xC020660B, request)
        count = struct.unpack_from("I", request, 20)[0]
        return struct.unpack_from("Q", request, 40)[0] if count else None
    except (OSError, ValueError):
        return None


def ordered_objects(root, entries, stage):
    """Order file reads for disk locality without changing canonical manifests."""
    indexed = []
    for number, entry in enumerate(entries, 1):
        offset = physical_offset(object_path(root, entry))
        indexed.append(((offset is None, offset or 0, entry["sha256"]), entry))
        if number % 1000 == 0:
            progress(stage + "_layout", number, len(entries))
    indexed.sort(key=lambda row: row[0])
    progress(stage + "_layout", len(entries), len(entries), force=True)
    return [row[1] for row in indexed]


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_objects(root, entries):
    """Flush verified independent files before publishing a completed manifest.

    syncfs flushes this filesystem once on Linux, avoiding hundreds of thousands
    of small fsync barriers. Portable fallback fsyncs files individually.
    """
    if sys.platform.startswith("linux"):
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        syncfs = getattr(libc, "syncfs", None)
        if syncfs is not None:
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                if syncfs(descriptor) != 0:
                    raise OSError(ctypes.get_errno(), "backup filesystem sync failed")
            finally:
                os.close(descriptor)
            progress("objects_durable", len(entries), len(entries), force=True)
            return
    for number, entry in enumerate(entries, 1):
        with object_path(root, entry).open("rb") as stream:
            os.fsync(stream.fileno())
        progress("objects_durable", number, len(entries))
    for path in {object_path(root, entry).parent for entry in entries}:
        sync_directory(path)
    sync_directory(root)


def _track(kind, value, **metadata):
    if _OPERATION is not None:
        entry = {"kind": kind, "value": str(value), **metadata}
        _OPERATION["resources"].append(entry)
        _journal_write(_OPERATION)
        return entry
    return None


def owned_directory(kind, path):
    """Journal intent before mkdir, but never claim an existing directory.

    Even a random-name collision does not grant cleanup ownership. In
    particular, a failed restore must not delete someone else's restore point.
    """
    if path.exists() or path.is_symlink():
        raise FileExistsError("维护目标已存在；不会覆盖或清理已有目录")
    entry = _track(kind, path.resolve(), ownership="pending")
    try:
        path.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        # A different creator may win between exists() and mkdir().
        if entry is not None:
            entry["released"] = True
            _journal_write(_OPERATION)
        raise
    if entry is not None:
        created = path.stat()
        entry["ownership"] = {"device": created.st_dev, "inode": created.st_ino}
        _journal_write(_OPERATION)


def _remove_owned_resource(entry, operation_id):
    """Apply the same identity fence to rollback and successful scratch discard.

    Legacy per-file partial journals remain readable. Missing directory/DB
    ownership cannot prove that a target has not been replaced, so retain it.
    """
    runtime = settings().runtime_dir.resolve()
    kind, value = entry.get("kind"), entry.get("value", "")
    if kind == "restore_database":
        expected = "iirp_v1_test_restore_" + operation_id[:12]
        if value != expected:
            raise ValueError("拒绝清理非本轮随机恢复数据库")
        with connection("postgres", autocommit=True) as conn:
            existing = conn.execute(
                "SELECT oid FROM pg_database WHERE datname=%s", (value,)
            ).fetchone()
            if not existing:
                return True
            if existing and entry.get("ownership") != {"database_oid": existing[0]}:
                raise ValueError("恢复库所有权未确认或已替换；保留目标供核对")
            conn.execute(f"SET statement_timeout='{SCRATCH_DATABASE_DROP_SECONDS}s'")
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(value))
            )
        return True
    path = Path(value)
    if path.is_symlink() or path.resolve() != path or not path.is_relative_to(runtime):
        raise ValueError("拒绝清理无效维护路径")
    if kind == "pool_partial":
        pool = runtime / "backups" / "object-pool" / "objects"
        if not path.is_relative_to(pool) or not path.name.endswith(".partial-" + operation_id):
            raise ValueError("拒绝清理非本轮暂存原文")
        path.unlink(missing_ok=True)
        return True
    if kind == "restore_directory":
        expected = runtime / "restores" / ("iirp_v1_test_restore_" + operation_id[:12])
        if path != expected:
            raise ValueError("拒绝清理非本轮恢复目录")
    elif kind == "backup_directory":
        if path.parent != runtime / "backups" or not path.name.endswith(operation_id[:12]):
            raise ValueError("拒绝清理非本轮备份目录")
        if (path / "manifest.json").exists():
            return False
    elif kind == "pool_staging_directory":
        if path != runtime / "backups" / (".pool-staging-" + operation_id):
            raise ValueError("拒绝清理非本轮原文暂存目录")
    elif kind == "retention_trash":
        if path.parent != runtime / "backups" / (".trash-" + operation_id):
            raise ValueError("拒绝清理非本轮保留删除暂存")
    else:
        raise ValueError("未知维护资源类型")
    if path.exists():
        current = path.stat()
        if entry.get("ownership") != {"device": current.st_dev, "inode": current.st_ino}:
            raise ValueError("目录所有权未确认或已替换；保留目标供核对")
        shutil.rmtree(path)
    return True


def cleanup_owned_operation(operation_id):
    """Reclaim only unpublished resources registered by this exact invocation.

    This is rollback of private scratch space, not retention of existing backups.
    No directory/database scan or user-supplied restore target is accepted.
    """
    if (
        not isinstance(operation_id, str)
        or len(operation_id) != 32
        or any(c not in "0123456789abcdef" for c in operation_id)
    ):
        raise ValueError("无效维护操作标识")
    runtime = settings().runtime_dir.resolve()
    journal = runtime / "maintenance-operations" / (operation_id + ".json")
    if not journal.is_file() or journal.is_symlink():
        return {"removed": [], "reason": "没有本次操作记录"}
    record = json.loads(journal.read_text())
    database = make_url(settings().database_url).database
    if record.get("id") != operation_id or record.get("source_database") != database:
        raise ValueError("维护操作不属于当前项目数据库")
    if record.get("status") in {"SUCCEEDED", "ROLLED_BACK"}:
        return {"removed": [], "reason": "该操作已完成，不重复删除资源"}
    removed, failures = [], []
    for entry in reversed(record.get("resources", [])):
        if entry.get("released"):
            continue
        kind, value = entry.get("kind"), entry.get("value", "")
        try:
            if not _remove_owned_resource(entry, operation_id):
                continue
            entry["released"] = True
            _journal_write(record)
            removed.append({"kind": kind, "value": value})
        except (OSError, ValueError, psycopg.Error) as exc:
            failures.append({"kind": kind, "error": type(exc).__name__})
    record["cleanup"] = {"removed": removed, "failures": failures}
    record["status"] = "CLEANUP_FAILED" if failures else "ROLLED_BACK"
    _journal_write(record)
    return record["cleanup"]


@contextmanager
def operation():
    global _OPERATION
    identifier = OPERATION_ID or uuid.uuid4().hex
    if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
        raise ValueError("无效维护操作标识")
    journal = settings().runtime_dir / "maintenance-operations" / (identifier + ".json")
    if journal.exists():
        raise ValueError("维护操作标识已存在，不覆盖既有操作记录")
    _OPERATION = {
        "id": identifier,
        "source_database": make_url(settings().database_url).database,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "RUNNING",
        "resources": [],
    }
    _journal_write(_OPERATION)
    try:
        yield
    except BaseException as exc:
        _OPERATION["status"] = "FAILED"
        _OPERATION["error"] = type(exc).__name__
        _journal_write(_OPERATION)
        cleanup_owned_operation(identifier)
        raise
    else:
        _OPERATION["status"] = "SUCCEEDED"
        _journal_write(_OPERATION)
    finally:
        _OPERATION = None


def require_capacity(required, purpose):
    free = shutil.disk_usage(settings().runtime_dir).free
    if not capacity_sufficient(free, required, settings().min_free_bytes):
        raise OSError(f"{purpose}预计需要 {required} 字节临时空间，保留空间不足；未删除已有历史。")
    return {
        "estimated_temporary_bytes": required,
        "free_bytes_before": free,
        "reserved_free_bytes": settings().min_free_bytes,
    }


@contextmanager
def publication_guard():
    """Serialize small filesystem publication/deletion steps with durable control.

    The worker renews the lease independently. The child cannot publish a
    manifest or prune backups after that lease/control version becomes stale.
    Additive partial files may remain after termination and are never backups.
    """
    if MANAGED_JOB is None:
        yield
        return
    with connection() as conn:
        row = conn.execute(
            "SELECT lease_token,control_version,lease_until,requested_action,status "
            "FROM job WHERE id=%s FOR UPDATE",
            (MANAGED_JOB["id"],),
        ).fetchone()
        if (
            not row
            or row[0] != MANAGED_JOB["lease_token"]
            or row[1] != MANAGED_JOB["control_version"]
            or not row[2]
            or row[2] <= datetime.now(timezone.utc)
            or row[3]
            or row[4] != "RUNNING"
        ):
            raise InterruptedError("维护租约或控制版本已失效；停止发布与清理。")
        yield


def atomic_json(path, data):
    temporary = path.with_name(path.name + ".partial")
    with temporary.open("w") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    with publication_guard():
        temporary.replace(path)
        sync_directory(path.parent)


def table_fingerprints(conn):
    """Content, not only counts, from the same snapshot as pg_dump.

    PostgreSQL's JSONB serialization normalizes key ordering. Primary-key order
    avoids materializing a whole table in Python; a server cursor streams rows.
    """
    # We consume every row. PostgreSQL's default 0.1 cursor fraction can choose
    # random UUID index scans optimized for an early stop and thrash disk on the
    # complete evidence pass. Keep the exact ordering and streamed row hashes.
    previous_fraction = conn.execute("SHOW cursor_tuple_fraction").fetchone()[0]
    conn.execute("SELECT set_config('cursor_tuple_fraction','1.0',true)")
    try:
        tables = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).fetchall()
        result = {}
        for (table,) in tables:
            started = time.perf_counter()
            keys = conn.execute(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
                "WHERE i.indrelid=%s::regclass AND i.indisprimary ORDER BY a.attnum",
                (table,),
            ).fetchall()
            ordering = (
                sql.SQL(",").join(sql.Identifier(key[0]) for key in keys)
                if keys
                else sql.SQL("to_jsonb(t)::text")
            )
            fingerprint, count = hashlib.sha256(), 0
            with conn.cursor(name="backup_" + uuid.uuid4().hex) as cursor:
                cursor.execute(
                    sql.SQL("SELECT to_jsonb(t)::text FROM {} t ORDER BY {}").format(
                        sql.Identifier(table), ordering
                    )
                )
                for (row,) in cursor:
                    payload = row.encode()
                    fingerprint.update(len(payload).to_bytes(8, "big"))
                    fingerprint.update(payload)
                    count += 1
                    if count % 5000 == 0:
                        progress("table_fingerprint:" + table, count)
            result[table] = {"count": count, "sha256": fingerprint.hexdigest()}
            print(json.dumps({"stage": "table_fingerprint", "table": table, "rows": count,
                              "seconds": time.perf_counter() - started}), flush=True)
        return result
    finally:
        if not conn.closed and conn.info.transaction_status != psycopg.pq.TransactionStatus.INERROR:
            conn.execute("SELECT set_config('cursor_tuple_fraction',%s,true)", (previous_fraction,))


def connection(database=None, autocommit=False):
    url = make_url(settings().database_url)
    return psycopg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname=database or url.database,
        autocommit=autocommit,
        connect_timeout=3,
        application_name="iirp_backup",
        options="-c statement_timeout=300000 -c lock_timeout=5000",
    )


def pg_command(tool, database, *args):
    url = make_url(settings().database_url)
    env = {
        **os.environ,
        "PGHOST": str(url.host),
        "PGPORT": str(url.port),
        "PGUSER": url.username,
        "PGPASSWORD": url.password,
        "PGDATABASE": database,
        "PGAPPNAME": "iirp_backup",
    }
    progress(tool + "_started", force=True)
    subprocess.run([str(PGBIN / tool), *map(str, args)], env=env, check=True, capture_output=True)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def object_path(root, entry):
    path = (root / entry["relative_path"]).resolve()
    expected = f"objects/{entry['sha256'][:2]}/{entry['sha256']}"
    if entry["relative_path"] != expected or not path.is_relative_to(root.resolve()):
        raise ValueError("备份包含无效来源对象路径。")
    return path


def backup():
    with maintenance_lock(), operation():
        directory = _backup()
        _prune_backups()
        return directory


def _backup():
    runtime = settings().runtime_dir
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + _OPERATION["id"][:12]
    directory = runtime / "backups" / stamp
    owned_directory("backup_directory", directory)
    if shutil.disk_usage(directory).free < settings().min_free_bytes:
        raise OSError("备份前剩余空间不足。")
    with connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        rows = conn.execute(
            "SELECT sha256, relative_path, byte_size FROM source_object ORDER BY sha256"
        ).fetchall()
        count = conn.execute("SELECT count(*) FROM job").fetchone()[0]
        policy = conn.execute(
            "SELECT sec_enabled, version FROM collection_policy WHERE id=1"
        ).fetchone()
        business_counts = {}
        strategies = []
        if conn.execute("SELECT to_regclass('batch')").fetchone()[0]:
            for table in (
                "batch",
                "request_scope",
                "transaction_event",
                "earnings_event",
                "analysis_request",
                "analysis_result",
            ):
                business_counts[table] = conn.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                ).fetchone()[0]
            strategies = [
                list(row)
                for row in conn.execute(
                    "SELECT key,enabled,version FROM collection_strategy ORDER BY key"
                ).fetchall()
            ]
        fingerprints = table_fingerprints(conn)
        estimated_dump = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        pool = runtime / "backups" / "object-pool"
        missing_sizes = [
            size for sha, rel, size in rows
            if not object_path(pool, {"sha256": sha, "relative_path": rel}).exists()
        ]
        required = temporary_bytes("backup", estimated_dump, sum(missing_sizes), len(missing_sizes))
        capacity = require_capacity(required, "一致备份")
        pg_command(
            "pg_dump",
            make_url(settings().database_url).database,
            "--format=custom",
            "--no-owner",
            "--no-acl",
            "--snapshot",
            snapshot,
            "--file",
            directory / "database.dump",
        )
    objects = [{"sha256": sha, "relative_path": rel, "byte_size": size} for sha, rel, size in rows]
    staging = runtime / "backups" / (".pool-staging-" + _OPERATION["id"])
    # Journal one private namespace before writing any temporary source. Keeping
    # every published object's former temporary path made each journal rewrite
    # grow with history (quadratic total I/O). Recovery still owns exactly this
    # invocation's scratch space; published immutable pool objects are preserved.
    owned_directory("pool_staging_directory", staging)
    # Source and existing pool live in unrelated disk locations. Complete the
    # source pass first, then verify the independent pool in its own order.
    created_hashes = set()
    for object_number, entry in enumerate(ordered_objects(runtime, objects, "backup_source"), 1):
        sha, size = entry["sha256"], entry["byte_size"]
        source = object_path(runtime, entry)
        if not source.is_file() or digest(source) != sha or source.stat().st_size != size:
            raise ValueError("源文件缺失或校验失败；备份未完成。")
        cached = object_path(pool, entry)
        cached.parent.mkdir(parents=True, exist_ok=True)
        if not cached.exists():
            temporary = staging / (sha + ".partial")
            require_capacity(size + 4096, "备份原文复制")
            shutil.copy2(source, temporary)
            if digest(temporary) != sha or temporary.stat().st_size != size:
                raise ValueError("备份池复制校验失败。")
            temporary.chmod(0o400)
            # Immutable, unreferenced pool content can survive a cancelled run.
            # Timely control is checked at progress checkpoints; the completed
            # backup manifest always retains the strict publication row lock.
            temporary.replace(cached)
            created_hashes.add(sha)
        progress("backup_source_verified", object_number, len(objects))
    progress("backup_source_verified", len(objects), len(objects), force=True)
    for object_number, entry in enumerate(ordered_objects(pool, objects, "backup_pool"), 1):
        cached = object_path(pool, entry)
        # New copies were already independently hash verified before publication.
        if (entry["sha256"] not in created_hashes and digest(cached) != entry["sha256"]) or cached.stat().st_size != entry["byte_size"]:
            raise ValueError("备份池原文校验失败；没有复用损坏对象。")
        dest = object_path(directory, entry)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.link(cached, dest)
        if not os.path.samefile(cached, dest) or dest.stat().st_size != entry["byte_size"]:
            raise ValueError("备份复制链接校验失败。")
        progress("backup_pool_verified", object_number, len(objects))
    progress("backup_pool_verified", len(objects), len(objects), force=True)
    durable_objects(directory, objects)
    with (directory / "database.dump").open("rb") as dump:
        os.fsync(dump.fileno())
    staging.rmdir()
    manifest = {
        "format_version": 3,
        "capacity": capacity,
        "database_size_bytes": estimated_dump,
        "table_fingerprints": fingerprints,
        "business_counts": business_counts,
        "strategies": strategies,
        "created_at": stamp,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "migration": revision,
        "app_version": "0.1.0",
        "database_sha256": digest(directory / "database.dump"),
        "job_count": count,
        "policy": list(policy) if policy else None,
        "objects": objects,
        "restore_verified_at": None,
    }
    atomic_json(directory / "manifest.json", manifest)
    print(directory)
    return directory


def restore_verify(directory, *, discard=False):
    with maintenance_lock(), operation():
        return _restore_verify(directory, discard=discard)


def _restore_verify(directory, *, discard=False):
    if (
        directory.is_symlink()
        or directory.resolve().parent != (settings().runtime_dir / "backups").resolve()
    ):
        raise ValueError("仅接受本项目备份目录，恢复不会更改其他项目文件")
    directory = directory.resolve()
    for filename in ("manifest.json", "database.dump"):
        path = directory / filename
        if path.is_symlink() or not path.is_file():
            raise ValueError("备份不完整或包含符号链接；未创建恢复数据库。")
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("format_version") not in {1, 2, 3}:
        raise ValueError("不支持该备份格式；请使用匹配版本，不覆盖目标。")
    if digest(directory / "database.dump") != manifest["database_sha256"]:
        raise ValueError("数据库备份哈希错误；未创建恢复数据库。")
    for number, entry in enumerate(ordered_objects(directory, manifest["objects"], "restore_backup"), 1):
        path = object_path(directory, entry)
        if digest(path) != entry["sha256"] or path.stat().st_size != entry["byte_size"]:
            raise ValueError("来源备份文件校验失败。")
        progress("restore_backup_verified", number, len(manifest["objects"]))
    # Never accepts a target database name: cannot overwrite the live database.
    name = "iirp_v1_test_restore_" + _OPERATION["id"][:12]
    dest = settings().runtime_dir / "restores" / name
    estimate = int(
        manifest.get("database_size_bytes", (directory / "database.dump").stat().st_size * 5)
    )
    capacity = require_capacity(
        temporary_bytes("restore", estimate,
                        sum(entry["byte_size"] for entry in manifest["objects"]),
                        len(manifest["objects"])), "独立恢复"
    )
    owned_directory("restore_directory", dest)
    # CREATE DATABASE can wait on checkpoint/fsync. This is private, journaled
    # scratch space, so it must not hold the source job row and block renewal.
    with publication_guard():
        pass
    with connection("postgres", autocommit=True) as conn:
        database_entry = _track("restore_database", name, ownership="pending")
        try:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        except psycopg.errors.DuplicateDatabase:
            # Even a random-name collision never grants ownership of that DB.
            for resource in _OPERATION["resources"]:
                if resource["kind"] == "restore_database" and resource["value"] == name:
                    resource["released"] = True
            _journal_write(_OPERATION)
            raise
        if database_entry is not None:
            oid = conn.execute("SELECT oid FROM pg_database WHERE datname=%s", (name,)).fetchone()[0]
            database_entry["ownership"] = {"database_oid": oid}
            _journal_write(_OPERATION)
    with publication_guard():
        pass
    pg_command(
        "pg_restore",
        name,
        "--no-owner",
        "--no-acl",
        "--exit-on-error",
        "--dbname",
        name,
        directory / "database.dump",
    )
    for object_number, entry in enumerate(ordered_objects(directory, manifest["objects"], "restore_copy"), 1):
        path = object_path(dest, entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        require_capacity(entry["byte_size"], "恢复原文复制")
        shutil.copy2(object_path(directory, entry), path)
        progress("restore_objects_copied", object_number, len(manifest["objects"]))
    progress("restore_objects_copied", len(manifest["objects"]), len(manifest["objects"]), force=True)
    durable_objects(dest, manifest["objects"])
    with connection(name) as conn:
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        job_count = conn.execute("SELECT count(*) FROM job").fetchone()[0]
        rows = conn.execute(
            "SELECT sha256, relative_path, byte_size FROM source_object ORDER BY sha256"
        ).fetchall()
        if manifest.get("table_fingerprints"):
            if table_fingerprints(conn) != manifest["table_fingerprints"]:
                raise ValueError("恢复库事实内容哈希与一致快照不匹配。")
        policy = conn.execute(
            "SELECT sec_enabled, version FROM collection_policy WHERE id=1"
        ).fetchone()
        if (
            revision != manifest["migration"]
            or job_count != manifest["job_count"]
            or (list(policy) if policy else None) != manifest["policy"]
        ):
            raise ValueError("恢复库的版本、任务或用户策略与快照不一致。")
        if len(rows) != len(manifest["objects"]):
            raise ValueError("恢复库来源清单数量不一致。")
        restored_objects = [{"sha256": sha, "relative_path": rel, "byte_size": size} for sha, rel, size in rows]
        for number, entry in enumerate(ordered_objects(dest, restored_objects, "restore_verify"), 1):
            path = object_path(dest, entry)
            if digest(path) != entry["sha256"] or path.stat().st_size != entry["byte_size"]:
                raise ValueError("恢复副本来源引用校验失败。")
            progress("restore_objects_verified", number, len(restored_objects))
        progress("restore_objects_verified", len(restored_objects), len(restored_objects), force=True)
        for table, expected_count in manifest.get("business_counts", {}).items():
            actual_count = conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()[0]
            if actual_count != expected_count:
                raise ValueError("恢复业务表计数与一致快照不匹配")
        if manifest.get("strategies"):
            actual_strategies = [
                list(row)
                for row in conn.execute(
                    "SELECT key,enabled,version FROM collection_strategy ORDER BY key"
                ).fetchall()
            ]
            if actual_strategies != manifest["strategies"]:
                raise ValueError("恢复来源策略与快照不匹配")
        # Restored state is readable first; then prevent accidental execution of restored jobs.
        conn.execute("UPDATE collection_policy SET sec_enabled=false, next_run_at=NULL")
        conn.execute(
            "UPDATE job SET status='CANCELLED', lease_token=NULL, lease_until=NULL WHERE status='CANCEL_REQUESTED'"
        )
        conn.execute(
            "UPDATE job SET status='PAUSED', requested_action='pause', lease_token=NULL, lease_until=NULL WHERE status IN ('QUEUED','RUNNING','RETRY_WAIT','PAUSE_REQUESTED')"
        )
        if conn.execute("SELECT to_regclass('batch')").fetchone()[0]:
            conn.execute(
                "UPDATE collection_strategy SET enabled=false,next_run_at=NULL,options=options || jsonb_build_object('user_controlled',true,'restored_paused',true)"
            )
            conn.execute("UPDATE batch SET status='CANCELLED' WHERE status='CANCEL_REQUESTED'")
            conn.execute(
                "UPDATE batch SET status='PAUSED',requested_action='pause' WHERE status IN ('QUEUED','RUNNING','RETRY_WAIT','PAUSE_REQUESTED')"
            )
            conn.execute("UPDATE batch_job SET active=false")
    report = {
        "database": name,
        "capacity": capacity,
        "runtime_directory": str(dest),
        "migration": revision,
        "job_count": job_count,
        "source_objects_verified": len(rows),
        "fact_hashes_verified": len(manifest.get("table_fingerprints", {})),
        "backup": directory.name,
        "object_bytes": sum(entry["byte_size"] for entry in manifest["objects"]),
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "safe_restore": "已核对原始策略，再关闭副本调度并暂停未完成工作；主库不变。",
    }
    atomic_json(dest / "restore-report.json", report)
    if discard:
        # Rollback of our own disposable copy is permitted after cancellation;
        # never hold the job row lock during a potentially slow database drop.
        for entry in reversed(_OPERATION["resources"]):
            if entry["kind"] in {"restore_database", "restore_directory"}:
                _remove_owned_resource(entry, _OPERATION["id"])
                entry["released"] = True
                _journal_write(_OPERATION)
        report["verification_copy_discarded"] = True
        # Only the report remains next to where the copy was.
        atomic_json(dest.with_name(dest.name + "-report.json"), report)
    manifest["restore_verified_at"] = report["verified_at"]
    manifest["restore_verification"] = report
    atomic_json(directory / "manifest.json", manifest)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def completed_backups():
    root = settings().runtime_dir / "backups"
    result = []
    if not root.exists():
        return result
    for directory in root.iterdir():
        if not directory.is_dir() or directory.is_symlink() or directory.name == "object-pool":
            continue
        manifest = {}
        try:
            if (directory / "manifest.json").is_symlink():
                continue
            manifest = json.loads((directory / "manifest.json").read_text())
            if not isinstance(manifest, dict):
                continue
            created = datetime.fromisoformat(
                manifest.get("completed_at", "").replace("Z", "+00:00")
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # Legacy manifests use the sortable UTC directory stamp.
            try:
                created = datetime.strptime(manifest["created_at"][:16], "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc
                )
            except (UnboundLocalError, ValueError, KeyError, TypeError):
                continue
        if (
            created.tzinfo is None
            or created > datetime.now(timezone.utc) + timedelta(minutes=5)
            or not (directory / "database.dump").is_file()
            or (directory / "database.dump").is_symlink()
        ):
            continue
        result.append((created, directory, manifest))
    return sorted(result, reverse=True, key=lambda entry: (entry[0], entry[1].name))


def prune_backups():
    with maintenance_lock(), operation():
        return _prune_backups()


def _prune_backups():
    """Keep the newest KEEP_BACKUPS complete backups; delete the older ones.

    Backups hard-link one shared object pool, so a pool object is deleted only
    when no kept backup references it. Directories without a complete manifest
    and dump are never deletion targets. Caller holds the maintenance lock.
    """
    entries = completed_backups()
    keep = {directory for _created, directory, _manifest in entries[:KEEP_BACKUPS]}
    removed = []
    for _created, directory, _manifest in entries:
        if directory in keep:
            continue
        if _OPERATION is None:
            # Public direct calls only report candidates; actual deletion belongs
            # to managed_backup's lock + per-invocation rollback journal.
            continue
        trash = directory.parent / (".trash-" + _OPERATION["id"]) / directory.name
        if trash.parent.is_symlink() or trash.parent.resolve() != trash.parent:
            raise ValueError("拒绝使用符号链接保留删除暂存目录")
        if trash.exists() or trash.is_symlink():
            raise FileExistsError("保留删除暂存目标已存在；不会覆盖已有目录")
        source_stat = directory.stat()
        entry = _track("retention_trash", trash.resolve(), ownership={
            "device": source_stat.st_dev, "inode": source_stat.st_ino,
        })
        trash.parent.mkdir(exist_ok=True)
        with publication_guard():
            if trash.exists() or trash.is_symlink():
                raise FileExistsError("保留删除暂存目标已存在；不会覆盖已有目录")
            directory.replace(trash)
        _remove_owned_resource(entry, _OPERATION["id"])
        entry["released"] = True
        _journal_write(_OPERATION)
        removed.append(directory.name)
    # Remove only pool objects no completed backup references. A still-linked
    # orphan is kept; pool cleanup never touches runtime/objects or restores.
    referenced = {
        entry["sha256"]
        for _created, directory, manifest in entries
        if directory in keep
        for entry in manifest.get("objects", [])
    }
    pool = settings().runtime_dir / "backups" / "object-pool" / "objects"
    for obj in pool.glob("*/*") if pool.exists() else []:
        if (
            _OPERATION is None
            or obj.is_symlink()
            or not obj.is_file()
            or obj.name in referenced
            or obj.stat().st_nlink > 1
        ):
            continue
        if len(obj.name) == 64 and all(char in "0123456789abcdef" for char in obj.name):
            with publication_guard():
                obj.unlink()
    return {"removed": removed, "kept": sorted(directory.name for directory in keep)}


def verified_timestamp(entry):
    created, _directory, manifest = entry
    try:
        stamp = datetime.fromisoformat(manifest["restore_verified_at"].replace("Z", "+00:00"))
        if (
            stamp.tzinfo is None
            or stamp < created
            or stamp > datetime.now(timezone.utc) + timedelta(minutes=5)
        ):
            return None
        return stamp
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def backup_integrity(entry):
    _created, directory, manifest = entry
    try:
        if digest(directory / "database.dump") != manifest["database_sha256"]:
            return False
        for number, obj in enumerate(ordered_objects(directory, manifest["objects"], "existing_backup"), 1):
            path = object_path(directory, obj)
            if (
                path.is_symlink()
                or digest(path) != obj["sha256"]
                or path.stat().st_size != obj["byte_size"]
            ):
                return False
            progress("existing_backup_verified", number, len(manifest["objects"]))
        return True
    except InterruptedError:
        # Progress checks carry lease/control failures. They must stop the run,
        # never be mistaken for an unreadable historical backup and skipped.
        raise
    except (OSError, TypeError, KeyError, ValueError):
        return False


def managed_backup():
    with maintenance_lock(), operation():
        entries = completed_backups()
        candidates = sorted(
            [(stamp, entry) for entry in entries if (stamp := verified_timestamp(entry))],
            key=lambda pair: pair[0], reverse=True,
        )
        stamps = []
        for stamp, entry in candidates:
            if backup_integrity(entry):
                stamps.append(stamp)
                break
        directory = _backup()
        verified = not stamps or max(stamps) <= datetime.now(timezone.utc) - timedelta(days=7)
        if verified:
            _restore_verify(directory, discard=True)
        retention = _prune_backups()
        manifest = json.loads((directory / "manifest.json").read_text())
        return {
            "backup_path": str(directory),
            "completed_at": manifest["completed_at"],
            "restore_verified_at": manifest["restore_verified_at"],
            "verified": verified,
            "retention": retention,
        }


def background_file_io():
    """Prefer interactive work during source hashing/copying on a shared disk.

    This affects this maintenance process and its children, not the PostgreSQL
    server or application workers. Query planning remains independently bounded.
    """
    if sys.platform.startswith("linux"):
        executable = shutil.which("ionice")
        if executable:
            subprocess.run([executable, "-c", "3", "-p", str(os.getpid())],
                           capture_output=True, check=False, timeout=3)
        try:
            os.nice(5)
        except OSError:
            pass


if __name__ == "__main__":
    background_file_io()
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["create", "verify", "managed", "recover"])
    parser.add_argument("directory", nargs="?", type=Path)
    parser.add_argument("--job", help=argparse.SUPPRESS)
    parser.add_argument("--operation", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.job:
        MANAGED_JOB = json.loads(args.job)

        def interrupted(_signum, _frame):
            raise InterruptedError("父进程已请求停止维护")

        signal.signal(signal.SIGTERM, interrupted)
        from iirp.maintenance import parent_watchdog

        parent_watchdog({**MANAGED_JOB, "operation_id": args.operation})
    OPERATION_ID = args.operation
    if args.command == "recover":
        with maintenance_lock():
            recovered = cleanup_owned_operation(args.operation)
            print(json.dumps(recovered, ensure_ascii=False))
        raise SystemExit(1 if recovered.get("failures") else 0)
    if args.command == "create":
        backup()
    elif args.command == "managed":
        print(json.dumps(managed_backup(), ensure_ascii=False))
    elif args.directory:
        # The restored copy (all sources + database) is discarded after a passing
        # verification; a failure rolls it back. The report is kept in the manifest.
        restore_verify(args.directory, discard=True)
    else:
        parser.error("verify 需要指定本项目备份目录。")
