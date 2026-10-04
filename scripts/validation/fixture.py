"""Portable G browser fixture: real API/PG/computation, fixed synthetic sources."""

import argparse
import json
import os
import signal
import sys
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))


def main():
    from scripts.validation.identity import (
        add_test_route,
        configured_identity,
        disable_sources,
        verified_identity,
    )

    configured_identity()  # Reject main DB before creating a connection.
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.port == 18081:
        raise ValueError("Synthetic fixture refuses original service port")
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / "fixture.json"
    if manifest.exists():
        raise FileExistsError("Fixture evidence already exists")
    from iirp import event_service, lifecycle
    from iirp.business_models import Security
    from iirp.business_worker import execute_business
    from iirp.db import session
    from iirp.operation_pool import OperationPool
    from iirp.queue import claim
    from sqlalchemy import select
    from test_earnings_lifecycle import event
    from test_lifecycle import clean_lifecycle, lifecycle_database
    from test_performance_pipeline import params
    from test_shared_compute import RealRunner, plan, setup

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    db = lifecycle_database.__wrapped__()
    next(db)
    try:
        with tempfile.TemporaryDirectory(prefix="iirp-browser-") as tmp:
            clean = clean_lifecycle.__wrapped__(Path(tmp))
            next(clean)
            try:
                disable_sources()
                create = setup("event_dates")
                events = [create(historical_years=8), create(historical_years=2)]
                with session() as s, s.begin():
                    security = s.scalar(select(Security).where(Security.symbol == "AAPL"))
                    event(s, security, day="2024-06-10", year=2024, quarter=2)
                native = {}
                for kind in ["monthly", "interval", "earnings"]:
                    view = lifecycle.create_analysis(
                        params(
                            kind=kind,
                            **(
                                {"start_mmdd": "01-03", "end_mmdd": "01-20"}
                                if kind == "interval"
                                else {}
                            ),
                            **(
                                {"current_fiscal_year": 2025, "years": [2024]}
                                if kind == "earnings"
                                else {}
                            ),
                        )
                    )
                    native[kind] = view["id"]
                for aid in [e[0] for e in events] + list(native.values()):
                    plan(aid)
                with closing(OperationPool()) as pool:
                    while job := claim({"event_compute", "research_compute"}):
                        execute_business(job, runner=RealRunner(pool))
                views = [event_service.get_analysis(e[0]) for e in events]
                natives = {kind: lifecycle.get_analysis(aid) for kind, aid in native.items()}
                assert all(v["result_id"] for v in views)
                assert all(v["results"] for v in natives.values())
                # Two same-input subscribers remain pending for browser control tests.
                shared = [create(date_window="before5"), create(date_window="before5")]
                for aid, _batch in shared:
                    plan(aid)
                import uvicorn

                from scripts.validation.app import app

                def execute_pending():
                    lifecycle.plan_tick()
                    with closing(OperationPool()) as pool:
                        completed = []
                        while job := claim({"event_compute", "research_compute"}):
                            execute_business(job, runner=RealRunner(pool))
                            completed.append(job.id)
                    return {"executed": completed}

                add_test_route(
                    app,
                    "/__iirp_test_compute__",
                    execute_pending,
                    methods=["POST"],
                )
                server = uvicorn.Server(
                    uvicorn.Config(
                        app, host="0.0.0.0", port=args.port, log_level="warning", lifespan="off"
                    )
                )
                thread = threading.Thread(target=server.run)
                thread.start()
                try:
                    deadline = time.monotonic() + 30
                    while not server.started:
                        if not thread.is_alive() or time.monotonic() > deadline:
                            raise RuntimeError("Synthetic HTTP startup failed")
                        time.sleep(0.1)
                    info = {
                        **verified_identity(),
                        "base": f"http://127.0.0.1:{args.port}",
                        "events": views,
                        "native": natives,
                        "analyses": native,
                        "shared": [{"analysis_id": a, "batch_id": b} for a, b in shared],
                        "pid": os.getpid(),
                    }
                    manifest.write_text(json.dumps(info, indent=2, default=str) + "\n")
                    deadline = time.monotonic() + 3600
                    while not stop.wait(0.25) and not (out / "stop").exists():
                        if time.monotonic() > deadline:
                            raise TimeoutError("Synthetic fixture lifetime exceeded")
                finally:
                    server.should_exit = True
                    thread.join(30)
                    if thread.is_alive():
                        raise RuntimeError("Synthetic HTTP server did not stop")
            finally:
                clean.close()
    finally:
        db.close()


if __name__ == "__main__":
    main()
