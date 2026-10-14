"""Long-term facts, retained ranges and source-free analysis inputs on isolated PG."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from dateutil.relativedelta import relativedelta
from iirp.analysis import calendar
from iirp.db import session
from iirp.jobs.batches import _create, defaults
from iirp.jobs.queue import claim
from iirp.market import long_prices
from iirp.market.cache import ensure_prices, expire_price_cache, price_bars
from iirp.models import (
    Batch,
    CollectionStrategy,
    IndexConstituent,
    IndexConstituentState,
    Job,
    LongPriceBar,
    LongPriceSeries,
    PriceCache,
    RequestScope,
    Security,
    SourceObservation,
)
from sqlalchemy import delete, func, select

from tests.market.test_market_lifecycle import (  # noqa: F401
    bar,
    clean_market,
    market_database,
)
from tests.market.test_market_lifecycle import (
    security_id as security_id,
)

LAST = date(2026, 10, 9)
STAMP = datetime(2026, 10, 10, 22, tzinfo=timezone.utc)
FIRST = date(2026, 8, 3)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(long_prices, "now", lambda: STAMP)
    monkeypatch.setattr(long_prices, "last_completed_session", lambda **_: LAST)
    # Calls without a timestamp read the wall clock; pin them to STAMP so the
    # synthetic series ending on LAST stays the latest completed session.
    completed = calendar.last_completed_session
    monkeypatch.setattr(calendar, "last_completed_session",
                        lambda as_of=None, calendar="XNYS": completed(as_of or STAMP, calendar))


def series(s, security_id):
    row = LongPriceSeries(symbol="SYNTH", security_id=security_id, retain_from=FIRST)
    s.add(row)
    s.flush()
    return row


def target(row, *, full=True, first=FIRST):
    return SimpleNamespace(target={"series_id": row.id, "security_id": row.security_id,
        "generation": row.generation, "start_date": str(first), "end_date": str(LAST), "full": full})


def response(first=FIRST, *, close="100", **fields):
    return {"provider": "synthetic", "records": [bar(str(day), close=close, **fields)
        for day in long_prices.sessions(first, LAST)]}


def test_daily_dividends_ignore_adjusted_close_but_split_correction_replaces_entire_range(security_id):
    with session() as s, s.begin():
        row = series(s, security_id)
        assert long_prices.persist(s, target(row), response())["records"] > 40
        before = row.generation
        assert long_prices.persist(s, target(row, full=False, first=date(2026, 10, 1)),
            response(date(2026, 10, 1), adj_close="80"))["records"] == 7
        assert row.generation == before
        result = long_prices.persist(s, target(row, full=False, first=date(2026, 10, 1)),
            response(date(2026, 10, 1), close="50", splits="2"))
        assert result["correction"] and row.needs_full and row.generation == before + 1
        # Do not publish a mixed adjustment basis during the correction.
        assert set(s.scalars(select(LongPriceBar.close))) == {Decimal("100")}
        batch, _ = _create(s, {"kind": "market_history", "request_id": "correction", "tickers": ["SYNTH"],
            "long_prices": True, "start_date": str(FIRST), "end_date": str(LAST)}, trigger="automatic", policy_key="market")
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id))
        long_prices.plan_scope(s, scope, batch, [1])
        job = s.get(Job, scope.checkpoint["job_id"])
        assert job.target["full"] and job.target["start_date"] == str(FIRST)
        assert long_prices.persist(s, job, response(close="50"))["records"] > 40
        assert not row.needs_full and set(s.scalars(select(LongPriceBar.close))) == {Decimal("50")}


def test_only_completed_sessions_and_tolerance(security_id):
    with session() as s, s.begin():
        row = series(s, security_id)
        data = response()
        data["records"] += [bar("2026-10-10"), bar("2026-10-12")]
        long_prices.persist(s, target(row), data)
        assert s.scalar(select(func.max(LongPriceBar.session_date))) == LAST
        old = s.scalar(select(LongPriceBar).limit(1))
        assert not long_prices.differs(old, dict.fromkeys(("open", "high", "low", "close"), Decimal("100.0005")))
        assert long_prices.differs(old, dict.fromkeys(("open", "high", "low", "close"), Decimal("100.01")))


@pytest.mark.parametrize("action,months", [("all", 0), ("year", 12), ("half_year", 6), ("quarter", 3)])
def test_deletion_advances_frontier_and_stale_download_cannot_restore_it(security_id, action, months):
    with session() as s, s.begin():
        row = series(s, security_id)
        first = date(2024, 1, 2)
        row.retain_from = first
        old_job = target(row, first=first)
        long_prices.persist(s, old_job, response(first))
        long_prices.manage(s, action)
        cutoff = STAMP.date() - relativedelta(months=months)
        assert row.retain_from == cutoff
        assert long_prices.persist(s, old_job, response())["superseded"]
        assert not s.scalar(select(LongPriceBar.session_date).where(LongPriceBar.session_date < cutoff).limit(1))
        assert not row.needs_full
        remaining = s.scalar(select(func.count()).select_from(LongPriceBar))
        assert remaining == len(long_prices.sessions(cutoff, LAST))
        if action == "all":
            assert s.scalar(select(func.count()).select_from(LongPriceBar)) == 0
        long_prices.manage(s, "refill")
        assert row.retain_from == long_prices.history_start() and row.needs_full


def test_new_members_fill_removed_members_stop_and_data_survives(security_id, monkeypatch):
    import iirp.analysis.sectors as sectors
    monkeypatch.setattr(sectors, "sector_etfs", lambda: [])
    monkeypatch.setattr(sectors, "group_etfs", lambda: [])
    with session() as s, s.begin():
        defaults(s)
        row = series(s, security_id)
        long_prices.persist(s, target(row), response())
        for name in ("sp500", "nasdaq100"):
            s.add(IndexConstituentState(index_name=name, updated_at=STAMP))
        s.add(IndexConstituent(index_name="sp500", ticker="SYNTH", name="Synthetic", updated_at=STAMP))
        s.flush()
        long_prices.schedule(s)
        assert row.active
        s.execute(delete(IndexConstituent))
        s.add(IndexConstituent(index_name="nasdaq100", ticker="NEW", name="New", updated_at=STAMP))
        s.flush()
        long_prices.reconcile(s)
        assert not row.active and row.removed_on == STAMP.date()
        assert s.scalar(select(func.count()).select_from(LongPriceBar)) > 0
        added = s.scalar(select(LongPriceSeries).where(LongPriceSeries.symbol == "NEW"))
        assert added.active and added.needs_full
        scope = SimpleNamespace(security_id=security_id, checkpoint={}, status="QUEUED")
        long_prices.plan_scope(s, scope, None, [1])
        assert scope.status == "READY"
        s.get(CollectionStrategy, "market").enabled = False
        before = s.scalar(select(func.count()).select_from(Batch))
        long_prices.schedule(s)
        assert s.scalar(select(func.count()).select_from(Batch)) == before


def test_covered_analysis_requests_use_local_prices_with_zero_source_requests(security_id, monkeypatch):
    import iirp.market.yahoo as yahoo
    calls = []
    monkeypatch.setattr(yahoo, "fetch_market", lambda *args: calls.append(args))
    with session() as s, s.begin():
        row = series(s, security_id)
        long_prices.persist(s, target(row), response())
        batch, _ = _create(s, {"kind": "market_history", "request_id": "local", "tickers": ["SYNTH"],
            "start_date": str(FIRST), "end_date": str(LAST)})
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id))
        security = s.get(Security, security_id)
        for start, end in [(FIRST, LAST), (date(2026, 9, 1), LAST), (date(2026, 10, 1), LAST)]:
            ensured = ensure_prices(s, scope, security, start, end)
            assert ensured["job"] is None
            assert price_bars(s, security_id, ensured["cache"].id)[0]
        assert calls == []
        assert s.scalar(select(func.count()).select_from(SourceObservation)) == 0
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind.in_(("market_history", "market_long_history")))) == 0
        cache = s.scalar(select(PriceCache))
        cache.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    expire_price_cache()
    with session() as s, s.begin():
        assert s.scalar(select(func.count()).select_from(LongPriceBar)) > 0
        security = s.get(Security, security_id)
        scope = s.get(RequestScope, scope.id)
        assert ensure_prices(s, scope, security, FIRST, LAST)["job"] is None
        assert calls == []


def test_immediate_history_claim_precedes_backfill(security_id):
    with session() as s, s.begin():
        row = series(s, security_id)
        batch, _ = _create(s, {"kind": "market_history", "request_id": "background", "tickers": ["SYNTH"],
            "long_prices": True, "start_date": str(FIRST), "end_date": str(LAST)}, trigger="automatic", policy_key="market")
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id))
        long_prices.plan_scope(s, scope, batch, [1])
        assert s.get(Job, scope.checkpoint["job_id"]).priority == 60
        manual, _ = _create(s, {"kind": "market_history", "request_id": "immediate", "tickers": ["SYNTH"],
            "start_date": str(FIRST), "end_date": str(LAST)})
        front = s.scalar(select(RequestScope).where(RequestScope.batch_id == manual.id))
        ensured = ensure_prices(s, front, s.get(Security, row.security_id), FIRST, LAST)
        foreground_id = ensured["job"].id
    assert claim({"market_history", "market_long_history"}).id == foreground_id


def test_new_members_respect_global_deletion(security_id, monkeypatch):
    import iirp.analysis.sectors as sectors
    monkeypatch.setattr(sectors, "sector_etfs", lambda: [])
    monkeypatch.setattr(sectors, "group_etfs", lambda: [])
    with session() as s, s.begin():
        defaults(s)
        row = series(s, security_id)
        long_prices.persist(s, target(row), response())
        long_prices.manage(s, "quarter")
        s.add(IndexConstituent(index_name="sp500", ticker="NEW", name="New", updated_at=STAMP))
        s.flush()
        long_prices.reconcile(s)
        new = s.scalar(select(LongPriceSeries).where(LongPriceSeries.symbol == "NEW"))
        assert new.retain_from == STAMP.date() - relativedelta(months=3)
        long_prices.manage(s, "refill")
        assert new.retain_from == long_prices.history_start()


def test_daily_update_waits_for_1730_eastern(security_id, monkeypatch):
    monkeypatch.setattr(long_prices, "reconcile", lambda s: None)
    stamp = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(long_prices, "now", lambda: stamp)
    with session() as s, s.begin():
        defaults(s)
        row = series(s, security_id)
        long_prices.persist(s, target(row), response())
        row.complete_through = date(2026, 10, 8)
        assert not row.needs_full
        long_prices.schedule(s)
        assert s.scalar(select(func.count()).select_from(Batch)) == 0
        stamp += timedelta(minutes=30)
        long_prices.schedule(s)
        assert s.scalar(select(func.count()).select_from(Batch)) == 1
        batch = s.scalar(select(Batch))
        assert batch.params["tickers"] == ["SYNTH"]


@pytest.mark.parametrize("kind", ["monthly", "interval"])
def test_real_analysis_and_expired_result_recompute_without_yahoo_requests(security_id, monkeypatch, kind):
    import iirp.market.yahoo as yahoo
    from iirp.analysis import requests
    from iirp.jobs import planner
    from iirp.models import AnalysisResult

    from tests.analysis.test_performance_pipeline import finish_compute, params
    from tests.clock import set_clock
    calls = []
    monkeypatch.setattr(yahoo, "fetch_market", lambda *args: calls.append(args))
    # A monthly study runs through the latest close, so the request's clock
    # must agree with the series' last session.
    set_clock(monkeypatch, lambda: STAMP)
    with session() as s, s.begin():
        row = series(s, security_id)
        first = date(2024, 1, 2)
        row.retain_from = first
        long_prices.persist(s, target(row, first=first), response(first))
    created = requests.create_analysis(params(kind=kind, tickers=["SYNTH"], current_year=2026,
        start_mmdd="01-01", end_mmdd="03-31"))
    planner.plan_tick()
    finish_compute()
    assert requests.get_analysis(created["id"])["results"]
    with session() as s, s.begin():
        for result in s.scalars(select(AnalysisResult)):
            assert result.expires_at <= datetime.now(timezone.utc) + timedelta(hours=24)
            result.expires_at = STAMP - timedelta(seconds=1)
        for cache in s.scalars(select(PriceCache)):
            cache.expires_at = STAMP - timedelta(seconds=1)
    expire_price_cache()
    child = requests.refresh_analysis(created["id"])
    planner.plan_tick()
    finish_compute()
    assert requests.get_analysis(child["id"])["results"]
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind.in_(("market_history", "market_long_history")))) == 0
        observations = s.scalars(select(SourceObservation)).all()
        assert observations and all(item.provider == "local" for item in observations)
        assert sum(item.params.get("observation", {}).get("http_requests", 0) or 0 for item in observations) == 0
    assert calls == []


def test_automatic_market_switch_pauses_and_resumes_backfill(security_id):
    from iirp.jobs.preferences import update_strategy
    with session() as s, s.begin():
        batch, _ = _create(s, {"kind": "market_history", "request_id": "backfill", "tickers": ["SYNTH"],
            "long_prices": True, "start_date": str(FIRST), "end_date": str(LAST)}, trigger="automatic", policy_key="market")
        identifier = batch.id
    update_strategy("market", False)
    with session() as s:
        assert s.get(Batch, identifier).status == "PAUSED"
    update_strategy("market", True)
    with session() as s:
        assert s.get(Batch, identifier).status == "QUEUED"


@pytest.mark.parametrize("kind", ["earnings", "custom"])
def test_event_studies_use_retained_history_without_source_requests(security_id, monkeypatch, kind):
    import json

    import iirp.market.yahoo as yahoo
    from iirp.events import service
    from iirp.jobs import planner
    calls = []
    monkeypatch.setattr(yahoo, "fetch_market", lambda *args: calls.append(args))
    with session() as s, s.begin():
        row = series(s, security_id)
        first = date(2024, 1, 2)
        row.retain_from = first
        long_prices.persist(s, target(row, first=first), response(first))
    event = {"ticker": "SYNTH", "date": "2026-09-15", "session": "during", "name": "Synthetic event"}
    if kind == "earnings":
        event.update(fiscal_year=2026, fiscal_quarter=3)
    created = service.create_set({"kind": kind, "text": json.dumps({"events": [event]}), "title": None,
        "request_id": "retained-event", "analyze": True, "n": 3, "benchmark": None})
    planner.plan_tick()
    result = service.get_analysis(created["analysis"]["analysis_id"])
    assert result["status"] == "SUCCEEDED" and result["tickers"][0]["result"]["event_count"] == 1
    with session() as s:
        assert s.scalar(select(func.count()).select_from(SourceObservation)) == 0
        assert s.scalar(select(func.count()).select_from(Job)) == 0
    assert calls == []


def test_insider_windows_and_sector_reads_use_retained_bars(security_id):
    from iirp.analysis.insider_windows import request_trade_prices, trade_windows
    from iirp.analysis.sectors import _etf_views
    with session() as s, s.begin():
        row = series(s, security_id)
        long_prices.persist(s, target(row), response())
        items = [{"id": "transaction", "ticker": "SYNTH", "date": "2026-09-15", "issuer_id": None}]
        windows = trade_windows(s, items, 3)
        assert windows["tickers"]["SYNTH"]["status"] == "ready"
        assert request_trade_prices(s, items, 3)["batch_ids"] == []
        assert windows["tickers"]["SYNTH"]["expires_at"] is None
        security = s.get(Security, security_id)
        security.symbol = row.symbol = "XLK"
        s.flush()
        views = _etf_views(s, ["XLK"], STAMP)
        assert views["XLK"]["periods"]["1m"]["value"] is not None
        assert views["XLK"]["price_expires_at"] is None
        assert s.scalar(select(func.count()).select_from(SourceObservation)) == 0
        assert s.scalar(select(func.count()).select_from(Job)) == 0


def test_inventory_routes_and_management_require_explicit_confirmation(security_id):
    from fastapi.testclient import TestClient
    from iirp.api.app import app
    from iirp.jobs.handlers import execute_business
    with session() as s, s.begin():
        row = series(s, security_id)
        long_prices.persist(s, target(row), response())
    client = TestClient(app, headers={"X-IIRP-Client": "web"})
    result = client.get("/api/v1/long-prices")
    assert result.status_code == 200 and result.json()["bars"] > 40
    assert client.post("/api/v1/long-prices/manage", json={"action": "all", "confirmed": False}).status_code == 422
    result = client.post("/api/v1/long-prices/manage", json={"action": "all", "confirmed": True})
    assert result.status_code == 202
    job = claim({"market_long_manage"})
    assert job.id == result.json()["job_id"]
    execute_business(job)
    assert client.get("/api/v1/long-prices").json()["bars"] == 0


def test_update_after_offline_period_includes_missed_sessions_and_overlap(security_id):
    with session() as s, s.begin():
        row = series(s, security_id)
        long_prices.persist(s, target(row), response())
        row.complete_through = date(2026, 8, 31)
        batch, _ = _create(s, {"kind": "market_history", "request_id": "offline", "tickers": ["SYNTH"],
            "long_prices": True, "start_date": str(FIRST), "end_date": str(LAST)}, trigger="automatic", policy_key="market")
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id))
        long_prices.plan_scope(s, scope, batch, [1])
        job = s.get(Job, scope.checkpoint["job_id"])
        assert not job.target["full"]
        assert date.fromisoformat(job.target["start_date"]) < row.complete_through
        assert job.target["end_date"] == str(LAST)


@pytest.mark.parametrize('bad_kind', ['missing', 'inconsistent', 'absent'])
def test_partial_source_retains_valid_facts_without_claiming_gap_coverage(security_id, bad_kind):
    from iirp.market.cache import covers
    with session() as s, s.begin():
        row = series(s, security_id)
        data = response()
        missing = date(2026, 9, 15)
        bad = next(item for item in data['records'] if item['date'] == str(missing))
        if bad_kind == 'missing':
            bad['open'] = None
        elif bad_kind == 'inconsistent':
            bad['low'] = '101'
        else:
            data['records'].remove(bad)
        result = long_prices.persist(s, target(row), data)
        assert not result['cached'] and result['records'] > 40
        assert row.needs_full
        assert row.details['missing_dates'] == [str(missing)]
        assert s.get(LongPriceBar, (row.id, missing)) is None
        assert not covers(row, FIRST, LAST)
        assert long_prices.local_series(s, security_id, FIRST, LAST) is None
        assert long_prices.local_series(s, security_id, date(2026, 10, 1), LAST) is row
        cache = long_prices.materialize(s, row)
        assert not covers(cache, FIRST, LAST)
        assert covers(cache, date(2026, 10, 1), LAST)
        batch, _ = _create(s, {'kind': 'market_history', 'request_id': 'source-gap',
            'tickers': ['SYNTH'], 'start_date': str(FIRST), 'end_date': str(LAST)}, trigger='manual')
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id))
        ensured = ensure_prices(s, scope, s.get(Security, security_id), FIRST, LAST)
        assert ensured['job'].kind == 'market_history'
        assert long_prices.status(s)['ready_symbols'] == 0


def test_partial_source_retry_keeps_existing_backoff(security_id, monkeypatch):
    monkeypatch.setattr(long_prices, 'reconcile', lambda s: None)
    with session() as s, s.begin():
        s.add(CollectionStrategy(key='market', enabled=True))
        row = series(s, security_id)
        data = response()
        data['records'][10]['open'] = None
        long_prices.persist(s, target(row), data)
        assert row.needs_full and row.fetched_at is not None
        long_prices.schedule(s)
        assert s.scalar(select(func.count()).select_from(Batch)) == 0
    monkeypatch.setattr(long_prices, 'now', lambda: STAMP + timedelta(hours=6, seconds=1))
    with session() as s, s.begin():
        long_prices.schedule(s)
        assert s.scalar(select(func.count()).select_from(Batch)) == 1


@pytest.mark.parametrize('symbol', ['^GSPC', '^IXIC', '^NDX'])
def test_existing_quote_only_index_identity_is_reverified_for_daily_history(security_id, symbol):
    with session() as s, s.begin():
        security = s.get(Security, security_id)
        security.symbol, security.status = symbol, 'VERIFIED_MARKET'
        row = series(s, security_id)
        row.symbol = symbol
        batch, _ = _create(s, {'kind': 'market_history', 'request_id': 'index-daily',
            'tickers': [symbol], 'long_prices': True, 'start_date': str(FIRST), 'end_date': str(LAST)},
            trigger='automatic', policy_key='market')
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id))
        scope.security_id = security_id
        long_prices.plan_scope(s, scope, batch, [1])
        job = s.get(Job, scope.checkpoint['job_id'])
        assert job.kind == 'market_identity'
        assert job.target['symbol'] == symbol
        assert scope.status == 'RUNNING'


def test_repeated_full_gap_is_ready_and_analysis_keeps_missing_day(security_id, monkeypatch):
    from iirp.analysis.research import compute_research
    from iirp.market.cache import coverage_for, covers
    monkeypatch.setattr(long_prices, 'reconcile', lambda s: None)
    with session() as s, s.begin():
        s.add(CollectionStrategy(key='market', enabled=True))
        row = series(s, security_id)
        data = response()
        data['records'][10]['open'] = None
        missing = data['records'][10]['date']
        assert not long_prices.persist(s, target(row), data)['cached']
        assert long_prices.persist(s, target(row), data)['cached']
        assert not row.needs_full
        assert row.details['accepted_missing_dates'] == [missing]
        assert covers(row, FIRST, LAST)
        cache = long_prices.materialize(s, row)
        assert covers(cache, FIRST, LAST)
        coverage = coverage_for(s, s.get(Security, security_id), FIRST, LAST, cache_id=cache.id)
        assert coverage['status'] == 'PARTIAL' and missing in coverage['missing_dates']
        bars, _ = price_bars(s, security_id, cache.id)
        result = compute_research({'kind': 'monthly', 'current_year': 2027, 'historical_years': 1}, bars, today=LAST)
        august = next(period for period in result['periods'] if period['month'] == 8)
        assert august['years'][0]['status'] == 'complete'
        assert missing in august['years'][0]['missing_dates']
        assert august['stats']['n'] == 1
        inventory = long_prices.status(s)
        assert inventory['status'] == 'ready' and inventory['ready_symbols'] == 1
        assert inventory['gap_symbols'] == inventory['gap_days'] == 1
        assert inventory['gaps'] == [{'symbol': 'SYNTH', 'dates': [missing], 'accepted': True}]
        row.attempted_at = STAMP - timedelta(hours=7)
        long_prices.schedule(s)
        assert s.scalar(select(func.count()).select_from(Batch)) == 0
        # The next ordinary daily response must retain the confirmed old gap.
        assert long_prices.persist(s, target(row, full=False, first=date(2026, 10, 1)),
            response(date(2026, 10, 1)))['cached']
        assert row.details['missing_dates'] == [missing] and not row.needs_full


@pytest.mark.parametrize('change', ['new', 'repaired'])
def test_changed_full_gap_requires_recheck_or_clears_gap(security_id, change):
    with session() as s, s.begin():
        row = series(s, security_id)
        data = response()
        data['records'][10]['open'] = None
        long_prices.persist(s, target(row), data)
        long_prices.persist(s, target(row), data)
        updated = response()
        if change == 'new':
            updated['records'][10]['open'] = None
            updated['records'][11]['close'] = None
        result = long_prices.persist(s, target(row), updated)
        assert row.needs_full == (change == 'new')
        assert result['cached'] == (change == 'repaired')
        assert row.details['accepted_missing_dates'] == []
        if change == 'new':
            assert long_prices.persist(s, target(row), updated)['cached']
            assert len(row.details['accepted_missing_dates']) == 2
        else:
            assert long_prices.status(s)['gap_days'] == 0


@pytest.mark.parametrize('change', ['new', 'repaired'])
def test_recent_window_gap_change_requests_full_response(security_id, change):
    with session() as s, s.begin():
        row = series(s, security_id)
        data = response()
        missing = next(item for item in data['records'] if item['date'] == '2026-10-02')
        missing['open'] = None
        long_prices.persist(s, target(row), data)
        long_prices.persist(s, target(row), data)
        update = response(date(2026, 10, 1))
        if change == 'new':
            update['records'][1]['open'] = None
            update['records'][2]['open'] = None
        generation = row.generation
        assert long_prices.persist(s, target(row, full=False, first=date(2026, 10, 1)), update)['correction']
        assert row.needs_full and row.generation == generation + 1


@pytest.mark.parametrize("stamp,start", [
    (STAMP, date(2016, 1, 1)),
    # 23:00 on December 31 in New York is still the old year.
    (datetime(2027, 1, 1, 4, tzinfo=timezone.utc), date(2016, 1, 1)),
    (datetime(2027, 1, 1, 5, tzinfo=timezone.utc), date(2017, 1, 1)),
])
def test_history_starts_on_january_first_of_the_tenth_complete_year(monkeypatch, stamp, start):
    monkeypatch.setattr(long_prices, "now", lambda: stamp)
    assert long_prices.history_start() == start


def reconcile_alone(s, monkeypatch):
    import iirp.analysis.sectors as sectors
    monkeypatch.setattr(sectors, "sector_etfs", lambda: [])
    monkeypatch.setattr(sectors, "group_etfs", lambda: [])
    long_prices.reconcile(s)


def test_series_saved_from_a_later_start_are_fetched_again_whole_once(security_id, monkeypatch):
    with session() as s, s.begin():
        defaults(s)
        row = series(s, security_id)
        long_prices.persist(s, target(row), response())
        row.attempted_at = STAMP
        generation = row.generation
        reconcile_alone(s, monkeypatch)
        assert row.retain_from == date(2016, 1, 1) and row.needs_full and row.attempted_at is None
        assert row.generation == generation + 1
        # Existing bars stay readable until the whole range replaces them.
        assert s.scalar(select(func.count()).select_from(LongPriceBar)) == len(long_prices.sessions(FIRST, LAST))
        reconcile_alone(s, monkeypatch)
        assert row.generation == generation + 1
        long_prices.schedule(s)
        batch = s.scalar(select(Batch).where(Batch.params["long_prices"].astext == "true"))
        assert "SYNTH" in batch.params["tickers"] and batch.params["start_date"] == "2016-01-01"
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id,
            RequestScope.security_id == security_id))
        long_prices.plan_scope(s, scope, batch, [1])
        job = s.get(Job, scope.checkpoint["job_id"])
        assert job.kind == "market_long_history" and job.priority == 60
        assert job.target["full"] and job.target["start_date"] == "2016-01-01"


def test_new_year_moves_the_start_forward_and_drops_older_bars(security_id, monkeypatch):
    with session() as s, s.begin():
        row = series(s, security_id)
        row.retain_from = date(2016, 1, 1)
        job = SimpleNamespace(target={"series_id": row.id, "security_id": row.security_id,
            "generation": row.generation, "start_date": "2016-12-01", "end_date": "2017-01-31", "full": True})
        long_prices.persist(s, job, {"provider": "synthetic", "records": [bar(str(day))
            for day in long_prices.sessions(date(2016, 12, 1), date(2017, 1, 31))]})
        row.details = {**row.details, "missing_dates": ["2016-12-15", "2017-01-10"]}
        generation = row.generation
        monkeypatch.setattr(long_prices, "now", lambda: datetime(2027, 1, 2, 15, tzinfo=timezone.utc))
        reconcile_alone(s, monkeypatch)
        assert row.retain_from == date(2017, 1, 1) and row.start_date == date(2017, 1, 1)
        assert not row.needs_full and row.generation == generation
        assert row.details["missing_dates"] == ["2017-01-10"]
        days = s.scalars(select(LongPriceBar.session_date).where(LongPriceBar.cache_id == row.id)).all()
        assert days and min(days) >= date(2017, 1, 1)


@pytest.mark.parametrize("action,months", [("year", 12), ("quarter", 3)])
def test_deleted_history_is_not_downloaded_again_across_years(security_id, monkeypatch, action, months):
    with session() as s, s.begin():
        row = series(s, security_id)
        row.retain_from = long_prices.history_start()
        long_prices.persist(s, target(row), response())
        long_prices.manage(s, action)
        cutoff = STAMP.date() - relativedelta(months=months)
        generation = row.generation
        reconcile_alone(s, monkeypatch)
        monkeypatch.setattr(long_prices, "now", lambda: datetime(2027, 1, 2, 15, tzinfo=timezone.utc))
        reconcile_alone(s, monkeypatch)
        assert row.retain_from == cutoff and not row.needs_full and row.generation == generation
        # Refilling returns to the rolling start rather than pinning today's date.
        long_prices.manage(s, "refill")
        assert row.retain_from == date(2017, 1, 1) and row.needs_full
        assert "long_prices" not in s.get(long_prices.Preferences, 1).values
