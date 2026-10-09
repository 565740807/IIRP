"""24-hour price cache: reopening reuses results; lapsed results fetch again."""
from datetime import date, datetime, timedelta, timezone

from iirp.analysis import requests
from iirp.db import session
from iirp.jobs import planner
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
    created = requests.create_analysis(params())
    planner.plan_tick()
    finish_compute()
    jobs = _jobs()
    first = requests.get_analysis(created["id"])
    assert first["results"] and first["freshness"]["expired"] is False
    assert first["freshness"]["price_fetched_at"] and first["freshness"]["price_expires_at"]
    # Reopening, automatic checks and an identical new request reuse the cache.
    for _ in range(3):
        assert requests.get_analysis(created["id"])["results"][0]["result_id"] == first["results"][0]["result_id"]
        assert requests.refresh_analysis(created["id"])["id"] == created["id"]
    assert requests.create_analysis(params())["results"]
    planner.plan_tick()
    assert _jobs() == jobs and _jobs("market_history") == 0
    with session() as s, s.begin():
        stamp = now() - timedelta(seconds=1)
        for cache in s.scalars(select(PriceCache)):
            cache.expires_at = stamp
        for result in s.scalars(select(AnalysisResult)):
            result.expires_at = stamp
    assert expire_price_cache()["price_caches"] == 1
    lapsed = requests.get_analysis(created["id"])
    assert lapsed["results"] == [] and lapsed["freshness"]["expired"] is True
    child = requests.refresh_analysis(created["id"])
    assert child["id"] != created["id"]
    assert child["freshness"]["origin_id"] == created["id"]
    assert requests.refresh_analysis(created["id"])["id"] == child["id"]
    planner.plan_tick()
    assert _jobs("market_history") == 1
    with session() as s:
        assert len(s.scalars(select(AnalysisRequest)).all()) == 3


def test_manual_refetch_creates_one_new_research_while_it_runs():
    security = seed_security()
    seed_prices(security, date(2022, 12, 1), date(2024, 12, 31), wide=True)
    created = requests.create_analysis(params())
    planner.plan_tick()
    finish_compute()
    refetched = requests.refresh_analysis(created["id"], force=True)
    assert refetched["id"] != created["id"]
    assert requests.refresh_analysis(created["id"], force=True)["id"] == refetched["id"]


def test_after_close_analysis_refetches_pre_close_cache_once(monkeypatch):
    from iirp.analysis import calendar
    from iirp.jobs import batches
    from iirp.jobs.handlers import execute_business
    from iirp.jobs.queue import claim
    from iirp.market import cache
    from iirp.models import PriceCacheBar

    from tests.analysis.test_performance_pipeline import CountingYahoo
    from tests.clock import set_clock

    stamp = datetime(2026, 10, 8, 19, 35, tzinfo=timezone.utc)
    def clock():
        return stamp
    set_clock(monkeypatch, clock)
    monkeypatch.setattr(cache, "now", clock)
    original = calendar.last_completed_session
    monkeypatch.setattr(calendar, "last_completed_session", lambda as_of=None, calendar="XNYS":
                        original(as_of=as_of or clock(), calendar=calendar))
    security = seed_security()
    batches.create_collection({"request_id": "before-close", "kind": "market_history",
                               "tickers": ["AAPL"], "start_date": "2025-09-20",
                               "end_date": "2026-10-08"})
    planner.plan_tick()
    class IntradayYahoo(CountingYahoo):
        def run(self, kind, target, checkpoint):
            result = super().run(kind, target, checkpoint)
            if clock().hour < 20:
                result["data"]["records"].append({"date": "2026-10-08",
                    **dict.fromkeys(("open", "high", "low", "close", "adj_close"), "100"),
                    "volume": "10", "splits": "0", "dividends": "0"})
            return result

    yahoo = IntradayYahoo()
    job = claim({"market_history"})
    assert job
    execute_business(job, runner=yahoo)
    planner.plan_tick()
    with session() as s:
        cached = s.scalar(select(PriceCache).where(PriceCache.security_id == security))
        assert cached.complete_through == date(2026, 10, 7)
        assert s.scalar(select(PriceCacheBar.status).where(
            PriceCacheBar.cache_id == cached.id,
            PriceCacheBar.session_date == date(2026, 10, 8))) == "UNCONFIRMED"

    stamp = datetime(2026, 10, 8, 20, 29, tzinfo=timezone.utc)
    created = requests.create_analysis(params(kind="interval", current_year=2026,
                                              start_mmdd="09-20", end_mmdd="10-15"))
    for _ in range(3):
        planner.plan_tick()
    refresh = claim({"market_history"})
    assert refresh is not None, "the pre-close fetch must not count as the post-close fetch"
    execute_business(refresh, runner=yahoo)
    planner.plan_tick()
    finish_compute()
    view = requests.get_analysis(created["id"])
    assert view["progress"] == [{"symbol": "AAPL", "step": "done", "reason": None}]
    assert view["results"]
    assert len(yahoo.calls) == 2 and _jobs("market_history") == 2
