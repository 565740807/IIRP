"""Sector references use per-stock classifications and local prices on isolated PG."""

import csv
import io
import json
from datetime import date
from decimal import Decimal

import pytest
from iirp.analysis import requests
from iirp.analysis.sector_benchmarks import sector_snapshot, validate_sector_benchmark
from iirp.api.events import EventAnalysisOutput
from iirp.api.schemas import AnalysisOutput
from iirp.db import session
from iirp.events import service
from iirp.jobs import planner
from iirp.jobs.queue import claim, fenced
from iirp.market.yahoo import resolve_metadata
from iirp.messages import UserError, decode
from iirp.models import (
    AnalysisRequest,
    AnalysisResult,
    Batch,
    IndexConstituent,
    Job,
    Security,
    SourceObservation,
    now,
)
from sqlalchemy import func, select

from tests.analysis.test_performance_pipeline import finish_compute, params
from tests.jobs.test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    seed_prices,
    seed_security,
)
from tests.market.test_classification import SP500


def classification(symbol="NVDA"):
    with session() as s, s.begin():
        s.add(IndexConstituent(**next(row for row in SP500 if row["ticker"] == symbol), updated_at=now()))


def prices(symbol, instrument="EQUITY"):
    identifier = seed_security(symbol)
    with session() as s, s.begin():
        s.get(Security, identifier).instrument = instrument
    seed_prices(identifier, date(2023, 1, 3), date(2024, 1, 31), wide=True)
    return identifier


def create(symbol="NVDA", sector="auto", **kwargs):
    return requests.create_analysis(params(tickers=[symbol], kind="interval", start_mmdd="01-03",
        end_mmdd="01-05", sector_benchmark=sector, **kwargs))


def sector_for(result):
    return result["results"][0]["data"]["metadata"]["sector"]


def source_requests():
    with session() as s:
        return s.scalar(select(func.count()).select_from(SourceObservation).where(
            SourceObservation.provider == "yfinance"))


def test_automatic_mapping_gics_yahoo_and_none():
    for symbol in ("NVDA", "ARM", "ZZTEST"):
        prices(symbol)
    prices("XLK", "ETF")
    classification()
    with session() as s, s.begin():
        arm = s.scalar(select(Security).where(Security.symbol == "ARM"))
        arm.metadata_json = {"sector": "Technology"}
    for symbol, expected in (("NVDA", "sp500_gics"), ("ARM", "yahoo"), ("ZZTEST", "none")):
        created = create(symbol)
        planner.plan_tick()
        finish_compute()
        info = sector_for(requests.get_analysis(created["id"]))
        assert info["source"] == expected
        assert info["symbol"] == (None if expected == "none" else "XLK")
        if expected == "none":
            assert decode(info["reason"])["code"] == "sector.unavailable"
    assert source_requests() == 0


def test_industry_group_selects_primary_and_falls_back_to_sector():
    prices("NVDA")
    prices("ARM")
    prices("XLK", "ETF")
    prices("SOXX", "ETF")
    classification()
    with session() as s, s.begin():
        s.scalar(select(Security).where(Security.symbol == "ARM")).metadata_json = {"sector": "Technology"}
    for symbol, etf, fallback in (("NVDA", "SOXX", False), ("ARM", "XLK", True)):
        created = create(symbol, sector="industry_group")
        planner.plan_tick()
        finish_compute()
        info = sector_for(requests.get_analysis(created["id"]))
        assert (info["symbol"], info["fallback"]) == (etf, fallback)
        if fallback:
            assert decode(info["reason"])["code"] == "sector.industry_group_fallback"
    assert source_requests() == 0


@pytest.mark.parametrize("kind", ["monthly", "earnings", "custom"])
def test_classification_refresh_is_waited_for_before_sector_calculation(kind):
    stock = prices("ARM")
    prices("XLK", "ETF")
    with session() as s, s.begin():
        s.get(Security, stock).metadata_json = {}
    if kind == "monthly":
        created = create("ARM")
        identifier = created["id"]
    else:
        event = {"ticker": "ARM", "date": "2023-01-05", "session": "after_close", "name": "Sample"}
        if kind == "earnings":
            event.update(fiscal_year=2023, fiscal_quarter=1)
        saved = service.create_set({"kind": kind, "text": json.dumps({"events": [event]}),
            "request_id": "refresh-event", "analyze": True, "n": 1, "benchmark": None})
        identifier = saved["analysis"]["analysis_id"]
    planner.plan_tick()
    planner.plan_tick()
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "market_identity")) == 1
        assert not s.scalar(select(AnalysisResult.id).where(AnalysisResult.analysis_id == identifier))
        assert not s.scalar(select(Job.id).where(Job.kind == "research_compute"))
    refresh = claim({"market_identity"})
    assert refresh and refresh.target["security_id"] == stock
    assert fenced(refresh, status="SUCCEEDED", business_write=lambda s, j: resolve_metadata(s,
        s.get(Security, stock), {"metadata": {"symbol": "ARM", "quoteType": "EQUITY",
            "exchange": "NMS", "currency": "USD", "sector": "Technology"}}))
    planner.plan_tick()
    if kind == "monthly":
        finish_compute()
        info = sector_for(requests.get_analysis(identifier))
    else:
        info = service.get_analysis(identifier)["tickers"][0]["result"]["sector"]
    assert (info["symbol"], info["source"], info["status"]) == ("XLK", "yahoo", "available")
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "market_identity")) == 1


@pytest.mark.parametrize("kind", ["monthly", "earnings", "custom"])
def test_failed_classification_refresh_publishes_reason_without_retry_loop(kind):
    stock = prices("ARM")
    with session() as s, s.begin():
        s.get(Security, stock).metadata_json = {}
    if kind == "monthly":
        created = create("ARM")
        identifier = created["id"]
    else:
        event = {"ticker": "ARM", "date": "2023-01-05", "session": "after_close", "name": "Sample"}
        if kind == "earnings":
            event.update(fiscal_year=2023, fiscal_quarter=1)
        saved = service.create_set({"kind": kind, "text": json.dumps({"events": [event]}),
            "request_id": "failed-refresh", "analyze": True, "n": 1, "benchmark": None})
        identifier = saved["analysis"]["analysis_id"]
    planner.plan_tick()
    refresh = claim({"market_identity"})
    assert fenced(refresh, status="FAILED", error="synthetic identity failure")
    planner.plan_tick()
    if kind == "monthly":
        finish_compute()
    for _ in range(3):
        planner.plan_tick()
    view = requests.get_analysis(identifier) if kind == "monthly" else service.get_analysis(identifier)
    info = sector_for(view) if kind == "monthly" else view["tickers"][0]["result"]["sector"]
    assert info["status"] == "sector_identity_failed"
    assert decode(info["reason"])["code"] == "sector.identity_failed"
    assert view["status"] == "PARTIAL"
    assert claim({"market_identity"}) is None
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "market_identity")) == 1


def test_sector_switches_custom_etf_and_none_use_zero_source_requests():
    prices("NVDA")
    prices("XLK", "ETF")
    prices("SOXX", "ETF")
    classification()
    before = source_requests()
    for choice in ("auto", "industry_group", "XLK", None, "UNSAVED"):
        created = create(sector=choice)
        planner.plan_tick()
        finish_compute()
        view = requests.get_analysis(created["id"])
        info = sector_for(view)
        if choice == "UNSAVED":
            assert info["status"] == "sector_reference_unavailable"
        if choice == "XLK":
            assert info["source"] == "custom"
        if choice is None:
            assert info is None
    assert source_requests() == before == 0
    with session() as s:
        assert not s.scalar(select(Job.id).where(Job.kind.in_(("market_identity", "market_history"))))


def test_research_sector_inputs_fence_publication_and_csv_metadata():
    from tests.analysis.test_benchmarks import publish
    from tests.jobs.test_lifecycle import refetch_prices
    prices("NVDA")
    sector_id = prices("XLK", "ETF")
    classification()
    created = create()
    planner.plan_tick()
    original = claim({"research_compute"})
    assert original.target["sector"]["symbol"] == "XLK"
    from iirp.jobs.handlers import _persist, prepare_target
    from iirp.jobs.operations import operation
    from iirp.storage.objects import save_object
    response = operation("research_compute", prepare_target(original))
    refetch_prices(sector_id, date(2023, 1, 3), date(2023, 1, 5), close=Decimal("102"))
    source = save_object(b"synthetic sector fence")
    assert fenced(original, source=source, business_write=lambda s, j: _persist(s, j, response, {}, source))
    with session() as s:
        assert not s.scalar(select(AnalysisResult.id).where(AnalysisResult.analysis_id == created["id"]))
    planner.plan_tick()
    publish(claim({"research_compute"}))
    for table in ("stats", "detail"):
        rows = list(csv.DictReader(io.StringIO(requests.export_analysis(created["id"], table).lstrip("\ufeff"))))
        assert rows and {row["sector_etf"] for row in rows} == {"XLK"}
        assert {row["sector_source"] for row in rows} == {"sp500_gics"}
        assert "sector_median_excess" in rows[0] if table == "stats" else "sector_excess" in rows[0]


def test_legacy_research_is_readable_and_refreshes_with_current_version():
    prices("NVDA")
    prices("XLK", "ETF")
    classification()
    created = create()
    planner.plan_tick()
    finish_compute()
    with session() as s, s.begin():
        row = s.scalar(select(AnalysisResult).where(AnalysisResult.analysis_id == created["id"]))
        data = {**row.data, "metadata": {k: v for k, v in row.data["metadata"].items()
            if k not in {"sector", "sector_comparison_included"}}}
        row.data = data
        row.inputs = {**row.inputs, "calculation_version": "research-v11-open-close"}
        request = s.get(AnalysisRequest, created["id"])
        request.params = {key: value for key, value in request.params.items() if key != "sector_benchmark"}
        batch = s.get(Batch, request.batch_id)
        submitted = {key: value for key, value in batch.params["analysis_input"].items() if key != "sector_benchmark"}
        batch.params = {**batch.params, "analysis_input": submitted}
    legacy = AnalysisOutput.model_validate(requests.get_analysis(created["id"]))
    assert legacy.results[0].data.metadata.sector_comparison_included is False
    refreshed = requests.refresh_analysis(created["id"], force=False)
    assert refreshed["id"] != created["id"]
    planner.plan_tick()
    if not refreshed["results"]:
        finish_compute()
    current = requests.get_analysis(refreshed["id"])
    assert current["results"][0]["data"]["metadata"]["sector_comparison_included"] is True
    assert current["params"]["sector_benchmark"] == "auto"


def test_event_variants_csv_and_legacy_results_remain_readable():
    prices("NVDA")
    prices("XLK", "ETF")
    prices("SOXX", "ETF")
    classification()
    event = {"ticker": "NVDA", "date": "2023-01-05", "session": "after_close", "name": "Sample"}
    saved = service.create_set({"kind": "custom", "text": json.dumps({"events": [event]}),
        "request_id": "local-event", "analyze": True, "n": 1, "benchmark": None})
    identifier = saved["analysis"]["analysis_id"]
    planner.plan_tick()
    before = source_requests()
    variant = service.analysis_variant(identifier, {"request_id": "group-event", "sector_benchmark": "industry_group"})
    planner.plan_tick()
    view = service.get_analysis(variant["analysis_id"])
    result = view["tickers"][0]["result"]
    assert result["sector"]["symbol"] == "SOXX"
    for window in ("before", "gap", "reaction", "after"):
        assert result["summary"][window]["sector_paired_n"] == 1
    assert source_requests() == before == 0
    for table in ("stats", "detail"):
        rows = list(csv.DictReader(io.StringIO(service.export_analysis(view["id"], table).lstrip("\ufeff"))))
        assert rows and {row["sector_etf"] for row in rows} == {"SOXX"}
        assert {row["sector_source"] for row in rows} == {"sp500_gics"}
        assert "sector_median_excess" in rows[0] if table == "stats" else "reaction_sector_excess" in rows[0]
    with session() as s, s.begin():
        row = s.scalar(select(AnalysisResult).where(AnalysisResult.analysis_id == identifier))
        row.data = {key: value for key, value in row.data.items() if key not in {"sector", "sector_comparison_included"}}
        row.inputs = {**row.inputs, "calculation_version": "event-windows-v2"}
        request = s.get(AnalysisRequest, identifier)
        request.params = {key: value for key, value in request.params.items() if key != "sector_benchmark"}
    legacy = EventAnalysisOutput.model_validate(service.get_analysis(identifier))
    assert legacy.tickers[0].result.sector_comparison_included is False
    replacement = service.refresh_analysis(identifier, force=False)
    assert replacement["id"] != identifier
    planner.plan_tick()
    refreshed = service.get_analysis(replacement["id"])
    assert refreshed["tickers"][0]["result"]["sector_comparison_included"] is True
    assert refreshed["sector_benchmark"] == "auto"


def test_sector_selection_rejects_indexes_and_accepts_etfs():
    assert validate_sector_benchmark(" xlk ") == "XLK"
    assert validate_sector_benchmark("industry_group") == "industry_group"
    assert validate_sector_benchmark(None) is None
    with pytest.raises(UserError):
        validate_sector_benchmark("^GSPC")
    prices("NVDA")
    bad = prices("NOTETF")
    with session() as s:
        stock = s.scalar(select(Security).where(Security.symbol == "NVDA"))
        assert sector_snapshot({"sector_benchmark": "NOTETF"}, stock, s)["status"] == "sector_reference_unavailable"
        assert s.get(Security, bad).instrument == "EQUITY"


def test_analysis_attaches_existing_manual_classification_refresh_once():
    from iirp.market.classification import request_refresh
    stock = prices("ARM")
    prices("XLK", "ETF")
    with session() as s, s.begin():
        s.get(Security, stock).metadata_json = {}
        assert request_refresh(s, "ARM")["refresh"] == "pending"
        identifier = s.scalar(select(Job.id).where(Job.kind == "market_identity"))
    created = create("ARM")
    planner.plan_tick()
    planner.plan_tick()
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "market_identity")) == 1
        from iirp.models import BatchJob, RequestScope
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == created["batch_id"]))
        assert s.get(BatchJob, (scope.id, identifier)).active
    refresh = claim({"market_identity"})
    assert refresh.id == identifier
    assert fenced(refresh, status="SUCCEEDED", business_write=lambda s, j: resolve_metadata(s,
        s.get(Security, stock), {"metadata": {"symbol": "ARM", "quoteType": "EQUITY",
            "exchange": "NMS", "currency": "USD", "sector": "Technology"}}))
    planner.plan_tick()
    finish_compute()
    assert sector_for(requests.get_analysis(created["id"]))["symbol"] == "XLK"


def test_each_stock_uses_its_own_sector_etf():
    for symbol in ("NVDA", "JPM", "XOM"):
        prices(symbol)
    for etf in ("XLK", "XLF", "XLE"):
        prices(etf, "ETF")
    classification("NVDA")
    classification("XOM")
    with session() as s, s.begin():
        s.add(IndexConstituent(index_name="sp500", ticker="JPM", name="JPMorgan",
            industry="Financials", sub_industry="Diversified Banks", classification="GICS", updated_at=now()))
    created = requests.create_analysis(params(tickers=["NVDA", "JPM", "XOM"], kind="interval",
        start_mmdd="01-03", end_mmdd="01-05"))
    planner.plan_tick()
    for _ in range(3):
        finish_compute()
    result = requests.get_analysis(created["id"])
    assert {item["symbol"]: item["data"]["metadata"]["sector"]["symbol"] for item in result["results"]} == {
        "NVDA": "XLK", "JPM": "XLF", "XOM": "XLE"}
    assert source_requests() == 0


def test_retained_sector_prices_are_materialized_without_external_requests():
    from iirp.models import LongPriceBar, LongPriceSeries, PriceCache, PriceCacheBar
    prices("NVDA")
    etf = prices("XLK", "ETF")
    classification()
    with session() as s, s.begin():
        cache = s.scalar(select(PriceCache).where(PriceCache.security_id == etf))
        series = LongPriceSeries(symbol="XLK", security_id=etf, retain_from=date(2023, 1, 3),
            start_date=date(2023, 1, 3), end_date=date(2024, 1, 31), complete_through=date(2024, 1, 31),
            fetched_at=now(), needs_full=False, details={})
        s.add(series)
        s.flush()
        for row in s.scalars(select(PriceCacheBar).where(PriceCacheBar.cache_id == cache.id)):
            s.add(LongPriceBar(cache_id=series.id, session_date=row.session_date, dividends=0, splits=0,
                **{field: getattr(row, field) for field in ("open", "high", "low", "close", "volume", "status", "reason")}))
        s.flush()
        s.delete(cache)
    created = create()
    planner.plan_tick()
    finish_compute()
    view = requests.get_analysis(created["id"])
    assert sector_for(view)["status"] == "available"
    assert view["results"][0]["data"]["periods"][0]["stats"]["sector_paired_n"] == 1
    with session() as s:
        assert s.scalar(select(PriceCache.provider).where(PriceCache.security_id == etf)) == "local_long_prices"
        assert not s.scalar(select(Job.id).where(Job.kind == "market_history"))
    assert source_requests() == 0


def test_custom_etf_without_local_prices_is_explicit_and_never_downloaded():
    prices("NVDA")
    reference = seed_security("UNSAVED")
    with session() as s, s.begin():
        s.get(Security, reference).instrument = "ETF"
    created = create(sector="UNSAVED")
    planner.plan_tick()
    finish_compute()
    info = sector_for(requests.get_analysis(created["id"]))
    assert info["status"] == "sector_prices_unavailable"
    assert decode(info["reason"])["code"] == "sector.prices_unavailable"
    assert source_requests() == 0
    assert claim({"market_history", "market_identity"}) is None


def test_existing_active_refresh_is_waited_for_after_prior_finished_refresh():
    from iirp.market.classification import request_refresh
    stock = prices("ARM")
    prices("XLK", "ETF")
    with session() as s, s.begin():
        s.get(Security, stock).metadata_json = {}
    created = create("ARM")
    planner.plan_tick()
    first = claim({"market_identity"})
    # Represent a completed older adapter that did not retain classification.
    assert fenced(first, status="SUCCEEDED")
    with session() as s, s.begin():
        assert request_refresh(s, "ARM")["refresh"] == "pending"
    planner.plan_tick()
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "market_identity")) == 2
        assert not s.scalar(select(AnalysisResult.id).where(AnalysisResult.analysis_id == created["id"]))
    latest = claim({"market_identity"})
    assert latest.id != first.id
    assert fenced(latest, status="SUCCEEDED", business_write=lambda s, j: resolve_metadata(s,
        s.get(Security, stock), {"metadata": {"symbol": "ARM", "quoteType": "EQUITY",
            "exchange": "NMS", "currency": "USD", "sector": "Technology"}}))
    planner.plan_tick()
    finish_compute()
    assert sector_for(requests.get_analysis(created["id"]))["status"] == "available"


def test_narrow_local_sector_cache_keeps_pairs_and_explains_missing_periods():
    prices("NVDA")
    etf = seed_security("XLK")
    with session() as s, s.begin():
        s.get(Security, etf).instrument = "ETF"
    # Only the past interval is local; the current year's interval is absent.
    seed_prices(etf, date(2023, 1, 3), date(2023, 1, 5), wide=False)
    classification()
    before = source_requests()
    created = create()
    planner.plan_tick()
    finish_compute()
    view = requests.get_analysis(created["id"])
    info = sector_for(view)
    assert info["status"] == "available"
    assert decode(info["reason"]) == {"code": "sector.prices_incomplete", "params": {"symbol": "XLK"}}
    period = view["results"][0]["data"]["periods"][0]
    assert period["stats"]["sector_paired_n"] == 1
    assert Decimal(period["years"][0]["sector_change"]) == 0
    assert period["years"][1]["sector_change"] is None
    assert source_requests() == before == 0
    assert claim({"market_history", "market_identity"}) is None
