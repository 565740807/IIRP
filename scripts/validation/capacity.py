"""Fixed real-channel scenarios. No process restart/GC/database clearing in B/C."""

import copy
import hashlib
import json
import os
import platform
import sys
import tempfile
import threading
import time
import traceback
import zlib
from datetime import date
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from fastapi.testclient import TestClient
from iirp import business_worker, result_storage, storage
from iirp import event_service as svc
from iirp.analytics.calendar import sessions
from iirp.api import app
from iirp.business_models import AnalysisResult
from iirp.business_worker import execute_business
from iirp.config import settings
from iirp.db import engine, session
from iirp.event_models import EventSet, EventSetVersion
from iirp.models import Job
from iirp.operation_pool import OperationPool
from iirp.queue import claim, ensure_defaults
from sqlalchemy import event, select, text
from test_event_pipeline import enqueue
from test_event_service import saved
from test_lifecycle import lifecycle_database, seed_prices

from scripts.validation.identity import configured_identity

configured_identity()
phase = sys.argv[1]
mode = sys.argv[2] if len(sys.argv) > 2 else "sequence"
out = Path(sys.argv[3])
out.mkdir(parents=True, exist_ok=True)
output = out / (phase + "-samples.json")
assert not output.exists()
samples = []
env = {}
current = {}
plans = {}
db = lifecycle_database.__wrapped__()
next(db)


def rss(pid):
    try:
        return (
            int(
                next(
                    x.split()[1]
                    for x in Path(f"/proc/{pid}/status").read_text().splitlines()
                    if x.startswith("VmRSS:")
                )
            )
            * 1024
        )
    except (OSError, StopIteration):
        return 0


def save():
    output.write_text(json.dumps({"environment": env, "samples": samples}, indent=2, default=str))


def timed(name, fn):
    def wrapped(*a, **kw):
        memory_before = rss(os.getpid()) if name.startswith(("payload_", "zlib_")) else None
        start = time.perf_counter()
        cpu_start = time.process_time()
        try:
            return fn(*a, **kw)
        finally:
            ms = (time.perf_counter() - start) * 1000
            if ms > 1:
                current.setdefault("stages", []).append(
                    {
                        "stage": name,
                        "ms": ms,
                        "cpu_ms": (time.process_time() - cpu_start) * 1000,
                        **(
                            {
                                "parent_rss_before": memory_before,
                                "parent_rss_after": rss(os.getpid()),
                            }
                            if memory_before
                            else {}
                        ),
                    }
                )

    return wrapped


zlib.compress = timed("zlib_compress", zlib.compress)
original_decompressobj = zlib.decompressobj


class MeasuredDecoder:
    def __init__(self, *a, **kw):
        self.decoder = original_decompressobj(*a, **kw)

    def decompress(self, *a, **kw):
        return timed("zlib_decompress", self.decoder.decompress)(*a, **kw)

    def __getattr__(self, name):
        return getattr(self.decoder, name)


zlib.decompressobj = MeasuredDecoder
result_storage.encode_payload = timed("payload_encode_compress", result_storage.encode_payload)
result_storage.decode_payload = timed("payload_decompress_decode", result_storage.decode_payload)
storage.response_evidence = timed("response_evidence_copy_and_encode", storage.response_evidence)
business_worker.prepare_target = timed("prepare_target", business_worker.prepare_target)
business_worker._persist = timed("persist_inside_fence", business_worker._persist)
original_fenced = business_worker.fenced


def timed_fenced(*args, **kwargs):
    name = (
        "publication_transaction"
        if getattr(kwargs.get("business_write"), "__name__", "") == "commit_response"
        else "other_fence_transaction"
    )
    return timed(name, original_fenced)(*args, **kwargs)


business_worker.fenced = timed_fenced
# Codec and deepcopy instrumentation measures actual product calls, retains no objects.
json.dumps = timed("json_encode", json.dumps)
json.loads = timed("json_decode", json.loads)
orig_copy = copy.deepcopy
local = threading.local()


def deep(*a, **kw):
    if getattr(local, "copying", False):
        return orig_copy(*a, **kw)
    local.copying = True
    start = time.perf_counter()
    try:
        return orig_copy(*a, **kw)
    finally:
        local.copying = False
        ms = (time.perf_counter() - start) * 1000
        if ms > 1:
            current.setdefault("stages", []).append({"stage": "deepcopy", "ms": ms})


copy.deepcopy = deep


def before(conn, cursor, statement, parameters, context, many):
    context.e21_start = time.perf_counter()
    if (
        "analysis_result" in statement
        and statement.lstrip().startswith(("SELECT", "WITH"))
        and len(str(parameters)) < 2000
    ):
        plans.setdefault(statement, (statement, parameters))


def after(conn, cursor, statement, parameters, context, many):
    ms = (time.perf_counter() - context.e21_start) * 1000
    if ms > 5 or "analysis_result" in statement:
        current.setdefault("sql", []).append({"ms": ms, "statement": statement, "status": "ok"})


def error(ctx):
    current.setdefault("sql", []).append(
        {
            "ms": (time.perf_counter() - ctx.execution_context.e21_start) * 1000
            if ctx.execution_context
            else None,
            "statement": ctx.statement,
            "status": "error",
            "sqlstate": getattr(ctx.original_exception, "sqlstate", None),
        }
    )


for name, fn in [
    ("before_cursor_execute", before),
    ("after_cursor_execute", after),
    ("handle_error", error),
]:
    event.listen(engine(), name, fn)

dataset_id = None


def prepare(n, shape):
    global dataset_id
    collection = saved()
    security = svc.get_set(collection["set_id"])["security"]["id"]
    dates = sessions(date(1950, 1, 1), date(2079, 1, 1))
    chosen = (
        [dates[100 + i * 15] for i in range(n)] if shape == "sparse" else [date(2024, 6, 10)] * n
    )
    dataset_id = seed_prices(
        security,
        min(chosen).replace(month=1, day=1),
        max(chosen).replace(month=12, day=31),
        dataset_id=dataset_id,
    )
    with session() as s, s.begin():
        rev = s.get(EventSetVersion, collection["version_id"])
        template = rev.events[0]
        rev.events = [
            {
                **copy.deepcopy(template),
                "client_event_id": f"event-{i:05}",
                "event_date": str(d),
                "event_year": d.year,
                "event_name": f"Synthetic event {i}",
            }
            for i, d in enumerate(chosen)
        ]
        rev.document = {
            **rev.document,
            "scope": {**rev.document["scope"], "year_start": 1950, "year_end": 2025},
        }
    return collection


def new_version(collection):
    with session() as s, s.begin():
        old = s.get(EventSetVersion, collection["version_id"])
        v = s.get(EventSet, collection["set_id"])
        v.version += 1
        newer = EventSetVersion(
            set_id=old.set_id,
            version=v.version,
            document=old.document,
            events=old.events,
            content_hash=str(uuid4()),
            preview_id=old.preview_id,
            reviews=old.reviews,
            revision_note="E2.1 synthetic unique input",
        )
        s.add(newer)
        s.flush()
        return newer.version


def read(client, aid, rid, n, shape, all_pages=True):
    result = {}
    try:
        return read_into(result, client, aid, rid, n, shape, all_pages)
    except Exception as exc:
        result["error"] = type(exc).__name__
        exc.measurements = result
        raise


def read_into(result, client, aid, rid, n, shape, all_pages):
    start = time.perf_counter()
    r = client.get(
        f"/api/v1/events/analyses/{aid}", params={"result_id": rid, "include_freshness": False}
    )
    result.update(
        {
            "result_ms": (time.perf_counter() - start) * 1000,
            "result_status": r.status_code,
            "result_bytes": len(r.content),
        }
    )
    assert r.status_code == 200
    view = r.json()
    assert len(view["data"]["rows"]) == n and all(x["points"] for x in view["data"]["rows"])
    summary = view["data"]["rows"][0]["overlap"]
    del view, r
    cursor = None
    keys = []
    pages = []
    result["pages"] = pages
    while True:
        start = time.perf_counter()
        r = client.get(
            f"/api/v1/analyses/{aid}/results/{rid}/event-overlaps",
            params={
                "event_key": "event-00000",
                "limit": 100,
                **({"cursor": cursor} if cursor else {}),
            },
        )
        pages.append(
            {
                "ms": (time.perf_counter() - start) * 1000,
                "status": r.status_code,
                "bytes": len(r.content),
            }
        )
        assert r.status_code == 200
        page = r.json()
        assert page["summary"] == summary
        keys.extend(x["event_key"] for x in page["items"])
        cursor = page["next_cursor"]
        if not cursor or not all_pages:
            break
    if all_pages:
        assert keys == ([f"event-{i:05}" for i in range(1, n)] if shape == "dense" else [])
        assert summary["preview_event_ids"] == keys[:10]
        result["keys_sha256"] = hashlib.sha256(
            json.dumps(keys, separators=(",", ":")).encode()
        ).hexdigest()
    result.update(
        pages=pages, total=page["total"], keys_count=len(keys), preview=summary["preview_event_ids"]
    )
    return result


try:
    with tempfile.TemporaryDirectory() as runtime:
        settings().runtime_dir = Path(runtime)
        ensure_defaults()
        with session() as s:
            env = {
                "database": s.scalar(text("select current_database()")),
                "postgres": s.scalar(text("select version()")),
                "statement_timeout": s.scalar(text("show statement_timeout")),
                "lock_timeout": s.scalar(text("show lock_timeout")),
                "work_mem": s.scalar(text("show work_mem")),
                "memory_max": Path("/sys/fs/cgroup/memory.max").read_text().strip(),
                "python": platform.python_version(),
                "pid": os.getpid(),
                "loadavg": os.getloadavg(),
                "mode": mode,
            }
            assert env["database"].startswith("iirp_v1_test_")
            assert env["memory_max"] == "805306368", "Preserve the 768 MiB application budget"
            assert env["statement_timeout"] == "5s" and env["lock_timeout"] == "500ms"
            save()
        stop = threading.Event()
        pool = None
        monitor_failures = []
        monitor_count = [0]

        def monitor_loop():
            with (out / (phase + "-resources.jsonl")).open("w") as f, engine().connect() as conn:
                conn = conn.execution_options(isolation_level="AUTOCOMMIT")
                while not stop.is_set():
                    # close() clears child.proc; hold each Popen reference once
                    # so a phase handoff cannot race the PID/RSS observation.
                    processes = [child.proc for child in list(pool.children.values())] if pool else []
                    item = {
                        "unix": time.time(),
                        "sample": current.get("index"),
                        "parent_rss": rss(os.getpid()),
                        "children": {
                            str(proc.pid): rss(proc.pid) for proc in processes if proc is not None
                        },
                    }
                    for k in [
                        "memory.current",
                        "memory.swap.current",
                        "memory.events",
                        "memory.pressure",
                        "cpu.stat",
                    ]:
                        item[k] = Path("/sys/fs/cgroup/" + k).read_text().strip()
                    try:
                        item["pg_activity"] = [
                            dict(r)
                            for r in conn.execute(
                                text(
                                    "select pid,state,wait_event_type,wait_event,extract(epoch from clock_timestamp()-query_start) as elapsed from pg_stat_activity where datname=current_database() and pid<>pg_backend_pid() and state<>'idle'"
                                )
                            ).mappings()
                        ]
                    except Exception as e:
                        item["monitor_error"] = type(e).__name__
                        monitor_failures.append(item["monitor_error"])
                    f.write(json.dumps(item, default=str) + "\n")
                    f.flush()
                    monitor_count[0] += 1
                    stop.wait(0.1)

        def monitor():
            try:
                monitor_loop()
            except Exception as exc:
                monitor_failures.append(type(exc).__name__)
                traceback.print_exc()

        thread = threading.Thread(target=monitor)
        thread.start()
        scenarios = (
            [(n, shape, 2) for n in (100, 500, 1000) for shape in ("sparse", "dense")]
            + [(1500, "dense", 2)]
            if mode == "sequence"
            else [(1500, "dense", 10 if mode == "repeat" else 2)]
        )
        fixed = None
        try:
            with TestClient(app) as client:
                for n, shape, trials in scenarios:
                    collection = prepare(n, shape)
                    pool = OperationPool()
                    for trial in range(trials):
                        current = {
                            "index": len(samples),
                            "n": n,
                            "shape": shape,
                            "trial": trial,
                            "startup": "cold" if trial == 0 else "warm",
                            "unix": time.time(),
                        }
                        samples.append(current)
                        version = new_version(collection)
                        created = svc.create_analysis(
                            collection["set_id"],
                            {
                                "request_id": str(uuid4()),
                                "version": version,
                                "cutoff_date": "2080-01-01",
                                "historical_years": 150,
                            },
                        )
                        enqueue(created["analysis_id"])
                        job = claim({"event_compute"})
                        assert job and job.target["analysis_id"] == created["analysis_id"]
                        current.update(
                            job_id=job.id,
                            input_key=job.target["input_key"],
                            analysis_id=created["analysis_id"],
                        )
                        start = time.perf_counter()
                        try:
                            execute_business(job, runner=pool)
                            current["execute_ms"] = (time.perf_counter() - start) * 1000
                            with session() as s:
                                j = s.get(Job, job.id)
                                current.update(
                                    status=j.status,
                                    error=j.error,
                                    attempts=j.attempts,
                                    timing=j.result,
                                )
                            child = pool.children.get("compute")
                            current["child_pid"] = child.proc.pid if child and child.proc else None
                            if child and child.directory:
                                for f in ("request", "response"):
                                    current[f + "_bytes"] = (
                                        (Path(child.directory.name) / (f + ".json")).stat().st_size
                                    )
                            if current["status"] == "SUCCEEDED":
                                with session() as s:
                                    rid = s.scalar(
                                        select(AnalysisResult.id).where(
                                            AnalysisResult.analysis_id == created["analysis_id"]
                                        )
                                    )
                                current["result_id"] = rid
                                current["reads"] = read(
                                    client, created["analysis_id"], rid, n, shape
                                )
                                with session() as s:
                                    current["storage"] = dict(
                                        s.execute(
                                            text(
                                                "select pg_column_size(data)+coalesce(pg_column_size(payload),0)+coalesce(pg_column_size(overlap_projection),0) as physical_bytes,octet_length(payload) as payload_bytes,pg_column_size(overlap_projection) as projection_bytes,octet_length(overlap_projection::text) as projection_json_bytes from analysis_result where id=:id"
                                            ),
                                            {"id": rid},
                                        )
                                        .mappings()
                                        .one()
                                    )
                                    value = s.get(AnalysisResult, rid).data
                                    current["storage"]["json_bytes"] = len(
                                        json.dumps(
                                            value,
                                            ensure_ascii=False,
                                            separators=(",", ":"),
                                            allow_nan=False,
                                        ).encode("utf-8")
                                    )
                                    del value
                                current["complete"] = True
                                if n == 1500:
                                    fixed = (created["analysis_id"], rid)
                            else:
                                current["complete"] = False
                        except Exception as e:
                            current["reads_at_failure"] = getattr(e, "measurements", {})
                            current["complete"] = False
                            current["exception"] = type(e).__name__ + ": " + str(e)[:160]
                            traceback.print_exc()
                        finally:
                            # Quarantine failed test jobs, never retry them as a later sample.
                            with session() as s, s.begin():
                                j = s.get(Job, job.id)
                                if j.status != "SUCCEEDED":
                                    j.status = "CANCELLED"
                            current["wall_ms"] = (time.perf_counter() - start) * 1000
                            save()
                            print(
                                current["index"],
                                n,
                                shape,
                                trial,
                                current.get("status"),
                                current.get("complete"),
                                flush=True,
                            )
                    if mode != "repeat":
                        pool.close()
                if mode == "repeat" and fixed:
                    reads = []
                    for i in range(100):
                        current = {"index": i, "phase": "C100", "unix": time.time()}
                        record = current
                        try:
                            record.update(read(client, *fixed, 1500, "dense", False))
                        except Exception as e:
                            record.update(getattr(e, "measurements", {}))
                            record["error"] = type(e).__name__
                            traceback.print_exc()
                        reads.append(record)
                        (out / (phase + "-reads100.json")).write_text(json.dumps(reads, indent=2))
                    current = {"index": "all-pages", "phase": "C100", "unix": time.time()}
                    (out / (phase + "-all-pages.json")).write_text(
                        json.dumps(read(client, *fixed, 1500, "dense"), indent=2)
                    )
                    (out / (phase + "-all-pages-observations.json")).write_text(
                        json.dumps(current, indent=2)
                    )
                if mode == "repeat" and fixed and len(sys.argv) > 4:
                    from scripts.validation.network_server import serve_fixed

                    # B10 and C100 (including all pages) are now complete in
                    # their unchanged parent/pool. The separate HTTP scenario
                    # starts its own real worker; do not retain the idle old
                    # computation child alongside that worker in the same cap.
                    current = {"index": "network-handoff"}
                    previous = [
                        (lane, child, proc)
                        for lane, child in pool.children.items()
                        if (proc := child.proc) is not None
                    ]
                    handoff = {
                        "unix": time.time(),
                        "parent_pid": os.getpid(),
                        "from": "B10-C100-and-all-pages",
                        "to": "independent-real-HTTP",
                        "complete": False,
                        "children_before": [
                            {"lane": lane, "pid": proc.pid, "rss": rss(proc.pid)}
                            for lane, _child, proc in previous
                        ],
                        "memory_current_before": Path("/sys/fs/cgroup/memory.current")
                        .read_text()
                        .strip(),
                    }
                    try:
                        pool.close()
                        handoff["children_after"] = [
                            {
                                "lane": lane,
                                "pid": proc.pid,
                                "returncode": proc.poll(),
                                "pool_reference_released": child.proc is None,
                                "proc_path_exists": Path(f"/proc/{proc.pid}").exists(),
                            }
                            for lane, child, proc in previous
                        ]
                        if any(
                            row["returncode"] is None or not row["pool_reference_released"]
                            for row in handoff["children_after"]
                        ):
                            raise RuntimeError("Previous capacity pool did not close before HTTP")
                        handoff["complete"] = True
                    except Exception as exc:
                        handoff["error"] = type(exc).__name__
                        raise
                    finally:
                        handoff["finished_unix"] = time.time()
                        handoff["memory_current_after"] = (
                            Path("/sys/fs/cgroup/memory.current").read_text().strip()
                        )
                        (out / "network-phase-handoff.json").write_text(
                            json.dumps(handoff, indent=2)
                        )

                    current = {"index": "network"}

                    def queue_fixed(count):
                        identifiers = []
                        for _ in range(count):
                            version = new_version(collection)
                            created = svc.create_analysis(
                                collection["set_id"],
                                {
                                    "request_id": str(uuid4()),
                                    "version": version,
                                    "cutoff_date": "2080-01-01",
                                    "historical_years": 150,
                                },
                            )
                            enqueue(created["analysis_id"])
                            identifiers.append(created["analysis_id"])
                        return identifiers

                    serve_fixed(app, out, int(sys.argv[4]), fixed, queue_fixed, current)
                current = {"index": "plans"}
                # Retain execution plans for last result only; diagnostic, outside timed samples.
                if fixed:
                    for i, (stmt, params) in enumerate(list(plans.values())):
                        if not isinstance(params, dict) or not stmt.lstrip().startswith(
                            ("SELECT", "WITH")
                        ):
                            continue
                        params = {**params}
                        for k in params:
                            if k == "result_id":
                                params[k] = fixed[1]
                            if k == "analysis_id":
                                params[k] = fixed[0]
                        try:
                            with engine().connect() as conn:
                                plan = conn.exec_driver_sql(
                                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + stmt, params
                                ).scalar()
                            (out / f"{phase}-plan-{i}.json").write_text(
                                json.dumps({"sql": stmt, "plan": plan}, indent=2, default=str)
                            )
                        except Exception as e:
                            (out / f"{phase}-plan-{i}-error.txt").write_text(type(e).__name__)
                with session() as s:
                    env["db_stats"] = dict(
                        s.execute(
                            text(
                                "select temp_files,temp_bytes,deadlocks from pg_stat_database where datname=current_database()"
                            )
                        )
                        .mappings()
                        .one()
                    )
                save()
        finally:
            stop.set()
            thread.join()
            if pool:
                pool.close()
            (out / (phase + "-monitor-result.json")).write_text(
                json.dumps({"samples": monitor_count[0], "errors": monitor_failures}, indent=2)
            )
finally:
    try:
        next(db)
    except StopIteration:
        pass

assert all(row.get("complete") for row in samples), "Capacity calculation/publication failed"
assert monitor_count[0] > 0 and not monitor_failures, "Capacity resource monitoring incomplete"
assert len(samples) == (10 if mode == "repeat" else 14 if mode == "sequence" else 2)
assert all(row.get("attempts") == 1 for row in samples), "Capacity samples must succeed first try"
if mode == "repeat":
    assert len({row["input_key"] for row in samples}) == 10
    assert len({row["child_pid"] for row in samples}) == 1
    assert all(row["child_pid"] is not None for row in samples), "Real compute child required"
    assert len(reads) == 100
    assert all(not row.get("error") for row in reads), "C100 read failed"
