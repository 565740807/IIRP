"""Supplement fixed B/C with real HTTP and a separate client outside its cgroup."""

import gc
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import Request
from iirp.business_models import AnalysisResult
from iirp.config import ROOT, settings
from iirp.db import session
from sqlalchemy import select

from scripts.validation.identity import add_test_route, disable_sources, verified_identity


def serve_fixed(app, out, port, fixed, queue_fixed, stages):
    if not 1024 <= port <= 65535 or port == 18081:
        raise ValueError("Invalid synthetic HTTP port")
    identity = verified_identity()
    disable_sources()
    worker = None
    queued = []
    finished = threading.Event()
    record_lock = threading.Lock()
    raw_encode = json.JSONEncoder(separators=(",", ":"), default=str).encode
    with (out / "network-server.jsonl").open("x") as log:

        def record(value):
            with record_lock:
                log.write(raw_encode({"unix": time.time(), **value}) + "\n")
                log.flush()

        gc_starts = {}

        def on_gc(phase, info):
            generation = info["generation"]
            if phase == "start":
                gc_starts[generation] = time.perf_counter()
            elif generation in gc_starts:
                record(
                    {
                        "stage": "gc",
                        "generation": generation,
                        "ms": (time.perf_counter() - gc_starts.pop(generation)) * 1000,
                        "collected": info["collected"],
                        "uncollectable": info["uncollectable"],
                    }
                )

        import fastapi.routing

        original_serialize = fastapi.routing.serialize_response

        async def serialize(*args, **kwargs):
            start, cpu = time.perf_counter(), time.process_time()
            try:
                return await original_serialize(*args, **kwargs)
            finally:
                record(
                    {
                        "stage": "serialize_response",
                        "ms": (time.perf_counter() - start) * 1000,
                        "cpu_ms": (time.process_time() - cpu) * 1000,
                    }
                )

        async def workload(request: Request):
            nonlocal worker
            if request.headers.get("X-IIRP-Test-Identity") != identity["validation_id"]:
                from fastapi import HTTPException

                raise HTTPException(403, "Synthetic identity required")
            if worker is not None:
                raise RuntimeError("Background scenario can run once only")
            worker = subprocess.Popen(
                [sys.executable, "-m", "scripts.validation.worker"],
                cwd=ROOT,
                env={**os.environ, "IIRP_RUNTIME_DIR": str(settings().runtime_dir)},
                stdout=worker_log,
                stderr=worker_log,
            )
            record(
                {"stage": "network_workload_started", "queued": queued, "worker_pid": worker.pid}
            )
            return {"queued_analyses": queued, "worker_pid": worker.pid}

        def status():
            with session() as s:
                ids = list(
                    s.scalars(
                        select(AnalysisResult.analysis_id).where(
                            AnalysisResult.analysis_id.in_(queued)
                        )
                    )
                )
            return {
                "queued": queued,
                "published": sorted(set(ids)),
                "worker_started": worker is not None,
                "worker_exit": worker.poll() if worker else None,
            }

        def finish():
            finished.set()
            return {"stopping": True}

        # Creating ten distinct 1500-event versions and planning their work is
        # fixture setup. Finish it before serving any measured HTTP requests;
        # the read-only scenario runs with no worker, and POST only starts it.
        setup_start = time.perf_counter()
        setup = {"stage": "network_workload_setup", "expected_count": 10, "complete": False}
        try:
            queued = list(queue_fixed(10))
            if len(queued) != 10 or len(set(queued)) != 10:
                raise RuntimeError("Network workload requires ten distinct queued analyses")
            prepared = status()
            setup.update(prepared)
            if prepared["published"]:
                raise RuntimeError("Network workload published before its worker was started")
            setup["complete"] = True
        except Exception as exc:
            setup["error"] = type(exc).__name__
            raise
        finally:
            setup["seconds"] = time.perf_counter() - setup_start
            record(setup)
            (out / "network-workload-setup.json").write_text(json.dumps(setup, indent=2))

        add_test_route(app, "/__iirp_test_identity__", verified_identity)
        add_test_route(app, "/__iirp_test_workload__", workload, methods=["POST"])
        add_test_route(app, "/__iirp_test_workload_status__", status)
        add_test_route(app, "/__iirp_test_stop__", finish, methods=["POST"])

        class Observed:
            async def __call__(self, scope, receive, send):
                start, cpu = time.perf_counter(), time.process_time()
                try:
                    return await app(scope, receive, send)
                finally:
                    record(
                        {
                            "stage": "http",
                            "path": scope.get("path"),
                            "ms": (time.perf_counter() - start) * 1000,
                            "cpu_ms": (time.process_time() - cpu) * 1000,
                        }
                    )

        with session() as s:
            data = s.get(AnalysisResult, fixed[1]).data
            fingerprint = hashlib.sha256(
                json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            ).hexdigest()
            del data
        server = uvicorn.Server(
            uvicorn.Config(
                Observed(), host="0.0.0.0", port=port, log_level="warning", lifespan="off"
            )
        )
        thread = threading.Thread(target=server.run)
        gc.callbacks.append(on_gc)
        fastapi.routing.serialize_response = serialize
        with (out / "network-worker.log").open("x") as worker_log:
            thread.start()
            try:
                deadline = time.monotonic() + 30
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        raise RuntimeError("Network capacity server startup failed")
                    time.sleep(0.1)
                (out / "network-fixture.json").write_text(
                    json.dumps(
                        {
                            **identity,
                            "base": f"http://127.0.0.1:{port}",
                            "analysis_id": fixed[0],
                            "result_id": fixed[1],
                            "data_sha256": fingerprint,
                            "events": 1500,
                            "background_prepared": prepared,
                            "application_pid": os.getpid(),
                            "application_cgroup": Path("/proc/self/cgroup").read_text().strip(),
                            "application_memory_max": Path("/sys/fs/cgroup/memory.max")
                            .read_text()
                            .strip(),
                        },
                        indent=2,
                    )
                )
                deadline = time.monotonic() + 1800
                while not finished.wait(0.25) and not (out / "stop").exists():
                    if time.monotonic() > deadline:
                        raise TimeoutError("External network client did not finish")
                (out / "network-publication.json").write_text(json.dumps(status(), indent=2))
            finally:
                if worker and worker.poll() is None:
                    worker.send_signal(signal.SIGTERM)
                    try:
                        worker.wait(40)
                    except subprocess.TimeoutExpired:
                        worker.kill()
                        worker.wait(5)
                        record({"stage": "worker_cleanup", "forced": True})
                server.should_exit = True
                thread.join(30)
                fastapi.routing.serialize_response = original_serialize
                gc.callbacks.remove(on_gc)
                (out / "network-sql-and-codec.json").write_text(
                    json.dumps(stages, indent=2, default=str)
                )
                if thread.is_alive():
                    raise RuntimeError("Network server still running")
