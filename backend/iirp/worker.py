import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from logging.handlers import RotatingFileHandler

from sqlalchemy.dialects.postgresql import insert

from iirp.config import ROOT, settings
from iirp.db import session
from iirp.models import SourceBudget, now
from iirp.profiles import development_budget
from iirp.queue import claim, ensure_defaults, fenced, heartbeat, tick
from iirp.storage import save_object
from iirp.worker_ownership import OwnershipLost, WorkerOwnership, previous_leases_drained

STOP = False
WORKER_ID = str(uuid.uuid4())


def stop(*_):
    global STOP
    STOP = True


def provider_budget(provider):
    with session() as s, s.begin():
        s.execute(insert(SourceBudget).values(provider=provider).on_conflict_do_nothing())
        budget = s.get(SourceBudget, provider, with_for_update=True)
        if budget.next_allowed_at > now():
            return (budget.next_allowed_at - now()).total_seconds()
        # This development probe has a 60-second shared request cooldown.
        budget.next_allowed_at = now() + timedelta(
            seconds=development_budget().source_cooldown_seconds
        )
        return 0


def run_probe(job, stopping=lambda: STOP):
    if stopping():
        return
    provider = "sec" if job.kind == "sec_probe" else "yfinance"
    delay = provider_budget(provider)
    if delay:
        fenced(
            job,
            status="RETRY_WAIT",
            error="来源处于冷却期，任务等待后再验证。",
            retry_seconds=delay,
        )
        return
    args = [sys.executable, "-m", "iirp.providers", job.kind, job.target.get("ticker", "AAPL")]
    # A child process bounds libraries whose internal network calls cannot be interrupted.
    # Files avoid pipe deadlocks. The child can never write business results itself.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(
            args,
            stdout=output,
            stderr=errors,
            start_new_session=True,
            env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
        )
        started = time.monotonic()
        try:
            while proc.poll() is None:
                if stopping() or not fenced(job, acknowledge_control=False):
                    return
                heartbeat(WORKER_ID)
                if time.monotonic() - started > development_budget().provider_deadline_seconds:
                    raise TimeoutError("来源验证超过 25 秒操作期限。")
                time.sleep(0.5)
            if proc.returncode != 0:
                raise RuntimeError(f"来源子进程异常退出（{proc.returncode}）。")
            output.seek(0)
            response = json.loads(output.read(7 * 1024**2))
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=2)
            # Only acknowledge pause/cancel after the provider has really stopped.
            fenced(job)
    if stopping():
        return
    if response.get("ok"):
        source = save_object(response.pop("payload").encode(), response.pop("media_type"))
        response["source_hash"] = source["sha256"]
        coverage = {
            "provider": provider,
            "target": job.target.get("ticker", "ownership_feed"),
            "status": "SAMPLE_ONLY",
            "message": response["message"],
            "source_hash": source["sha256"],
            "updated_at": now(),
        }
        fenced(job, done=1, result=response, source=source, coverage=coverage, status="SUCCEEDED")
    else:
        with session() as s, s.begin():
            budget = s.get(SourceBudget, provider, with_for_update=True)
            budget.failures += 1
            budget.next_allowed_at = now() + timedelta(
                seconds=response.get("cooldown", development_budget().failure_cooldown_seconds)
            )
        coverage = {
            "provider": provider,
            "target": job.target.get("ticker", "ownership_feed"),
            "status": "FAILED",
            "message": response["message"],
            "updated_at": now(),
        }
        fenced(job, coverage=coverage, status="FAILED", error=response["message"])


def execute(job, stopping=lambda: STOP):
    try:
        if job.kind == "fixture_check":
            for i in range(job.progress_done, job.progress_total):
                for _ in range(4):
                    if stopping() or not fenced(job):
                        return
                    heartbeat(WORKER_ID)
                    time.sleep(0.25)
                source = save_object(
                    json.dumps({"provenance": "synthetic-test-fixture", "step": i + 1}).encode()
                )
                if not fenced(
                    job, done=i + 1, checkpoint={"last_verified_step": i + 1}, source=source
                ):
                    return
            fenced(
                job,
                status="SUCCEEDED",
                result={"message": "8 个合成测试检查点已提交；不是市场或 Insider 数据。"},
            )
        else:
            run_probe(job, stopping)
    except Exception as exc:
        logging.exception("job failed id=%s type=%s", job.id, type(exc).__name__)
        fenced(
            job, status="FAILED", error=f"任务中断（{type(exc).__name__}），请检查本地运行日志。"
        )


def collect_finished(futures):
    """Release completed lanes even when a job failed outside its fenced handler."""
    for future in list(futures):
        if future.done():
            lane = futures.pop(future)
            try:
                future.result()
            except Exception as exc:
                # Its lease remains durable; recover() handles unfinished writes.
                logging.exception("completed lane failed lane=%s type=%s", lane, type(exc).__name__)



def housekeeping():
    """Expired sources, price caches and results, and the daily exact storage check."""
    from iirp.price_cache import expire_price_cache
    from iirp.storage import expire_sources
    from iirp.storage_inventory import exact_tick

    try:
        removed = expire_sources()
        if removed.get("objects"):
            logging.info("expired sources removed objects=%s bytes=%s", removed["objects"], removed["bytes"])
    except Exception as exc:
        logging.exception("source expiry failed type=%s", type(exc).__name__)
    try:
        removed = expire_price_cache()
        if removed.get("price_caches") or removed.get("analysis_results"):
            logging.info("expired price caches=%s results=%s", removed["price_caches"], removed["analysis_results"])
    except Exception as exc:
        logging.exception("price cache expiry failed type=%s", type(exc).__name__)
    try:
        exact_tick()
    except Exception as exc:
        logging.exception("exact storage check failed type=%s", type(exc).__name__)


def claim_lane(lane, kinds):
    """A failed source lane must not prevent independent market/compute work."""
    try:
        return claim(kinds, prefer_latest=lane == "sec")
    except Exception as exc:
        logging.exception("task claim failed lane=%s type=%s", lane, type(exc).__name__)
        return None


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logdir = settings().runtime_dir / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(logdir / "worker-events.log", maxBytes=5 * 1024**2, backupCount=4)
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    ensure_defaults()
    from iirp.feed_index import ensure_cluster, reconcile

    with session() as s, s.begin():
        ensure_cluster(s)
        # Revisions published by an older worker during an upgrade get pointers.
        reconcile(s)
    from iirp.freshness import ensure_fresh

    ensure_fresh({"reason": "startup", "sources": ["sec", "market"]})
    # One coordinator owns bounded provider lanes; other coordinators exit visibly.
    with WorkerOwnership() as owner:
        from iirp.business_worker import execute_business
        from iirp.lifecycle import plan_tick
        from iirp.maintenance import operational_log_tick, schedule_tick

        lanes = {
            "sec": (2, {"sec_discover", "sec_document", "sec_identity", "earnings_evidence"}),
            "market": (
                1,
                {"market_identity", "market_history", "market_quote", "earnings_candidates"},
            ),
            "compute": (1, {"research_compute", "event_compute", "local_import"}),
            "diagnostic": (1, {"fixture_check", "sec_probe", "market_probe"}),
            "maintenance": (1, {"maintenance_backup", "maintenance_clean"}),
        }
        from iirp.sec_poll import poll_due, poll_once, release_stale_lease

        release_stale_lease()
        futures = {}
        last_plan = 0.0
        poll_future = None
        chores, last_chores = None, 0.0
        from iirp.operation_pool import OperationPool
        operations = OperationPool()
        def stopping():
            return STOP or owner.stopping()
        drained = False
        def business(job):
            try:
                return execute_business(job, stopping, runner=operations if job.kind.startswith("market_") or job.kind in {"research_compute", "event_compute", "earnings_candidates"} else None)
            finally:
                from iirp.lifecycle import plan_job_scopes
                if not stopping():
                    plan_job_scopes(job.id)
        try:
            with ThreadPoolExecutor(max_workers=5, thread_name_prefix="iirp") as pool:
                while not stopping():
                    try:
                        owner.require()
                        heartbeat(WORKER_ID)
                        collect_finished(futures)
                        if time.monotonic() - last_plan >= 2:
                            last_plan = time.monotonic()
                            tick()
                            plan_tick(time_budget_seconds=1.0)
                            schedule_tick()
                            operational_log_tick()
                            if (drained and (poll_future is None or poll_future.done())
                                    and len(futures) < 5
                                    and sum(value == "sec" for value in futures.values()) < lanes["sec"][0]
                                    and poll_due()):
                                # The SEC latest poll uses an SEC lane slot, not a job.
                                poll_future = pool.submit(poll_once, stopping)
                                futures[poll_future] = "sec"
                            if (chores is None or not chores.is_alive()) and time.monotonic() - last_chores >= 600:
                                last_chores = time.monotonic()
                                chores = threading.Thread(target=housekeeping, name="housekeeping", daemon=True)
                                chores.start()
                        if not drained:
                            drained = previous_leases_drained()
                            if not drained:
                                time.sleep(0.25)
                                continue
                        for lane, (limit, kinds) in lanes.items():
                            owner.require()
                            if (
                                len(futures) >= 5
                                or sum(value == lane for value in futures.values()) >= limit
                            ):
                                continue
                            job = claim_lane(lane, kinds)
                            if job:
                                owner.require()
                                future = (
                                    pool.submit(execute, job, stopping)
                                    if lane == "diagnostic"
                                    else pool.submit(business, job)
                                )
                                futures[future] = lane
                        time.sleep(0.5)
                    except OwnershipLost:
                        break
                    except Exception as exc:
                        logging.exception("worker loop failed type=%s", type(exc).__name__)
                        time.sleep(2)
        finally:
            operations.close()
        owner.require()



if __name__ == "__main__":
    main()
