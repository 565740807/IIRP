"""24-hour price cache: reopening reuses results; lapsed results fetch again (D14)."""
from datetime import date, timedelta

from iirp.db import session
from iirp.jobs import lifecycle
from iirp.market.cache import expire_price_cache
from iirp.models import AnalysisRequest, AnalysisResult, Job, PriceCache, now
from sqlalchemy import func, select

from tests.analysis.test_performance_pipeline import finish_compute, params
from tests.jobs.test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    seed_prices,
    seed_security,
)


def _jobs(kind=None):
    with session() as s:
        query = select(func.count()).select_from(Job)
        if kind:
            query = query.where(Job.kind == kind)
        return s.scalar(query)


def test_reopening_within_24_hours_requests_nothing_and_expiry_fetches_again():
    security = seed_security()
    seed_prices(security, date(2022, 12, 1), date(2024, 12, 31), wide=True)
    created = lifecycle.create_analysis(params())
    lifecycle.plan_tick()
    finish_compute()
    jobs = _jobs()
    first = lifecycle.get_analysis(created["id"])
    assert first["results"] and first["freshness"]["expired"] is False
    assert first["freshness"]["price_fetched_at"] and first["freshness"]["price_expires_at"]
    # Reopening, automatic checks and an identical new request reuse the cache.
    for _ in range(3):
        assert lifecycle.get_analysis(created["id"])["results"][0]["result_id"] == first["results"][0]["result_id"]
        assert lifecycle.refresh_analysis(created["id"])["id"] == created["id"]
    assert lifecycle.create_analysis(params())["results"]
    lifecycle.plan_tick()
    assert _jobs() == jobs and _jobs("market_history") == 0
    with session() as s, s.begin():
        stamp = now() - timedelta(seconds=1)
        for cache in s.scalars(select(PriceCache)):
            cache.expires_at = stamp
        for result in s.scalars(select(AnalysisResult)):
            result.expires_at = stamp
    assert expire_price_cache()["price_caches"] == 1
    lapsed = lifecycle.get_analysis(created["id"])
    assert lapsed["results"] == [] and lapsed["freshness"]["expired"] is True
    child = lifecycle.refresh_analysis(created["id"])
    assert child["id"] != created["id"]
    assert child["freshness"]["origin_id"] == created["id"]
    assert lifecycle.refresh_analysis(created["id"])["id"] == child["id"]
    lifecycle.plan_tick()
    assert _jobs("market_history") == 1
    with session() as s:
        assert len(s.scalars(select(AnalysisRequest)).all()) == 3


def test_manual_refetch_creates_one_new_research_while_it_runs():
    security = seed_security()
    seed_prices(security, date(2022, 12, 1), date(2024, 12, 31), wide=True)
    created = lifecycle.create_analysis(params())
    lifecycle.plan_tick()
    finish_compute()
    refetched = lifecycle.refresh_analysis(created["id"], force=True)
    assert refetched["id"] != created["id"]
    assert lifecycle.refresh_analysis(created["id"], force=True)["id"] == refetched["id"]


def test_acceptance_label_is_not_a_financial_condition():
    seed_security()
    values = params()
    values["research_label"] = "验收20260922-隔离测试"
    view = lifecycle.create_analysis(values)
    assert view["batch"]["title"].startswith("验收20260922")
    assert "research_label" not in view["params"]
