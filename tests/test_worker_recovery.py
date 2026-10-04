"""Coordinator failure must release capacity while durable leases recover."""
from concurrent.futures import Future

from iirp.worker import collect_finished


def test_failed_future_is_removed_and_other_lanes_continue(caplog):
    failed, success, running = Future(), Future(), Future()
    failed.set_exception(RuntimeError("database completion lock timed out"))
    success.set_result(None)
    futures = {failed: "sec", success: "market", running: "sec"}
    collect_finished(futures)
    assert futures == {running: "sec"}
    assert "completed lane failed" in caplog.text
    # Repeated ticks must not re-raise or grow the failed future traceback.
    collect_finished(futures)
    running.set_result(None)
    collect_finished(futures)
    assert futures == {}


def test_database_commit_failure_retries_without_consuming_source_attempts(monkeypatch):
    from types import SimpleNamespace

    from iirp import business_worker
    from sqlalchemy.exc import OperationalError
    calls = []
    def fence(job, **kwargs):
        if not calls:
            calls.append(kwargs)
            raise OperationalError("completion", {}, RuntimeError("lock timeout"))
        calls.append(kwargs)
        return True
    monkeypatch.setattr(business_worker, "fenced", fence)
    job = SimpleNamespace(id="fenced-test", kind="local_import", attempts=2)
    business_worker.execute_business(job)
    assert calls[-1]["status"] == "RETRY_WAIT"
    assert calls[-1]["retry_seconds"] == 5
    calls[-1]["business_write"](None, job)
    assert job.attempts == 1


def test_failed_sec_claim_does_not_abort_market_or_compute(monkeypatch, caplog):
    from iirp import worker
    calls = []
    def claiming(kinds, **kwargs):
        calls.append(kinds)
        if kinds == {"sec_document"}:
            raise TimeoutError("claim budget exceeded")
        return next(iter(kinds))
    monkeypatch.setattr(worker, "claim", claiming)
    assert worker.claim_lane("sec", {"sec_document"}) is None
    assert worker.claim_lane("market", {"market_identity"}) == "market_identity"
    assert worker.claim_lane("compute", {"research_compute"}) == "research_compute"
    assert len(calls) == 3
    assert "lane=sec" in caplog.text
