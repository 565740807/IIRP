"""Synthetic 24-hour price cache tests on their own PostgreSQL database."""

import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from iirp.analysis.research import compute_research
from iirp.config import ROOT, settings
from iirp.db import engine, session
from iirp.jobs.queue import claim, fenced
from iirp.market.cache import coverage_for, current_cache, persist_prices, price_bars
from iirp.market.yahoo import quote_from_history, source_contract
from iirp.models import (
    Base,
    BatchJob,
    Job,
    PriceCache,
    PriceCacheBar,
    RequestScope,
    Security,
    SourceObject,
)
from iirp.storage.objects import save_object
from psycopg import sql
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from tests.zh import zh


@pytest.fixture(scope="module", autouse=True)
def market_database():
    url = make_url(settings().database_url)
    name = "iirp_v1_test_market_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname="postgres",
        autocommit=True,
    )
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    previous = os.environ.get("IIRP_DATABASE_URL")
    os.environ["IIRP_DATABASE_URL"] = url.set(database=name).render_as_string(hide_password=False)
    engine.cache_clear()
    settings.cache_clear()
    try:
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        yield
    finally:
        engine().dispose()
        engine.cache_clear()
        if previous is None:
            os.environ.pop("IIRP_DATABASE_URL", None)
        else:
            os.environ["IIRP_DATABASE_URL"] = previous
        settings.cache_clear()
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture(autouse=True)
def clean_market(tmp_path):
    with engine().begin() as connection:
        connection.execute(
            text(
                "TRUNCATE "
                + ",".join('"' + t.name + '"' for t in Base.metadata.sorted_tables)
                + " CASCADE"
            )
        )
    previous = settings().runtime_dir
    settings().runtime_dir = tmp_path
    yield
    settings().runtime_dir = previous


@pytest.fixture
def security_id():
    with session() as s, s.begin():
        security = Security(
            symbol="SYNTH",
            name="Synthetic fixture security",
            instrument="EQUITY",
            currency="USD",
            exchange="NYQ",
            calendar="XNYS",
            status="VERIFIED",
        )
        s.add(security)
        s.flush()
        return security.id


def bar(day, close="100", **overrides):
    return {
        "date": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "adj_close": close,
        "volume": "10",
        "splits": "0",
        "dividends": "0",
        **overrides,
    }


def response(records):
    return {
        "provider": "synthetic",
        "library_version": "fixture",
        "fetched_at": "2024-01-01T00:00:00+00:00",
        "records": records,
        "metadata": {},
    }


def pending_job(security_id, start="2023-01-03", end="2023-01-05", *, batch_id=None):
    with session() as s, s.begin():
        job = Job(
            kind="market_history",
            title="Synthetic publication unit",
            priority=-100,
            target={
                "symbol": "SYNTH",
                "security_id": security_id,
                "start_date": start,
                "end_date": end,
            },
            idempotency_key=uuid.uuid4().hex,
        )
        s.add(job)
        s.flush()
        identifier = job.id
        if batch_id is not None:
            scope = s.scalar(select(RequestScope).where(
                RequestScope.batch_id == batch_id, RequestScope.security_id == security_id,
            ))
            assert scope is not None
            s.add(BatchJob(scope_id=scope.id, job_id=job.id, active=True))
    lease = claim({"market_history"})
    assert lease.id == identifier
    return lease


def commit_prices(security_id, payload, *, start="2023-01-03", end="2023-01-05", fail_after=False, batch_id=None):
    lease = pending_job(security_id, start, end, batch_id=batch_id)
    source = save_object(json.dumps(payload, sort_keys=True).encode())
    results = []

    def write(s, current):
        results.append(persist_prices(s, current, payload))
        if fail_after:
            s.flush()
            raise RuntimeError("Synthetic failure after complete price write")

    assert fenced(
        lease,
        source=source,
        business_write=write,
        done=1,
        checkpoint={"unit": 1},
        status="SUCCEEDED",
    )
    return results[0], source


def cached(security_id):
    with session() as s:
        return current_cache(s, security_id)


def initial_prices(security_id, *, start="2022-11-01", end=None):
    """A cache covering research scopes around 2023-01-03..05 (other days are gaps)."""
    end = end or str(date.today())
    commit_prices(
        security_id,
        response([bar("2023-01-03"), bar("2023-01-04"), bar("2023-01-05")]),
        start=start, end=end,
    )
    return cached(security_id).id


def test_market_detail_uses_cached_split_only_prices(security_id):
    from iirp.analysis.calendar import sessions
    from iirp.market.reads import market_detail
    from iirp.models import MarketQuote

    days = sessions(date(2023, 1, 3), date(2023, 5, 31))
    commit_prices(
        security_id,
        response([bar(str(day), str(100 + i)) for i, day in enumerate(days)]),
        start=str(days[0]),
        end=str(days[-1]),
    )
    with session() as s, s.begin():
        s.add(
            MarketQuote(
                symbol="SYNTH",
                data={"as_of": "2023-01-05", "source": "synthetic_quote", "value": "100"},
            )
        )
    result = market_detail("SYNTH")
    assert len(result["items"]) == 60
    assert [row["date"] for row in result["items"]] == [str(day) for day in days[-60:]]
    assert result["data"]["chart_start"] == str(days[-60])
    assert result["data"]["price_cache_id"] == cached(security_id).id
    assert result["data"]["as_of"] == "2023-01-05"
    assert result["data"]["chart_end"] == str(days[-1])
    assert result["data"]["chart_source"] == "synthetic"
    assert "仅拆股调整" in zh(result["data"]["chart_basis"])


def test_one_response_becomes_the_24_hour_cache_in_one_fenced_transaction(security_id):
    from iirp.models import now

    result, source = commit_prices(
        security_id,
        response([bar("2023-01-03"), bar("2023-01-04", "110"), bar("2023-01-05", "90")]),
    )
    assert result["cached"]
    with session() as s:
        bars, cache = price_bars(s, security_id)
        assert all(row["status"] == "VALID" for row in bars)
        assert abs((cache.expires_at - cache.fetched_at).total_seconds() - 24 * 3600) < 1
        assert cache.fetched_at <= now() < cache.expires_at
        coverage = coverage_for(s, s.get(Security, security_id), date(2023, 1, 3), date(2023, 1, 5))
        assert coverage["status"] == "COMPLETE" and coverage["valid_sessions"] == 3
        assert coverage["cache_id"] == cache.id and coverage["expires_at"]


def test_a_new_response_replaces_the_cache_but_a_narrower_one_does_not(security_id):
    commit_prices(security_id, response([bar("2023-01-03"), bar("2023-01-04"), bar("2023-01-05")]))
    first = cached(security_id).id
    result, _ = commit_prices(security_id, response([bar("2023-01-04", "101")]),
                              start="2023-01-04", end="2023-01-04")
    assert not result["cached"] and result["superseded_by"] == first
    assert cached(security_id).id == first
    wider, _ = commit_prices(security_id, response([bar("2023-01-03", "102")]),
                             start="2023-01-02", end="2023-01-06")
    assert wider["cached"]
    with session() as s:
        assert s.scalar(select(func.count()).select_from(PriceCache)) == 1
        bars, cache = price_bars(s, security_id)
        assert cache.id == wider["cache_id"] != first
        assert [row["close"] for row in bars] == ["102.000000000000"]


def test_empty_or_conflicting_responses_do_not_create_a_cache(security_id):
    result, _ = commit_prices(security_id, response([]))
    assert not result["cached"] and "空数据" in zh(result["reason"])
    payload = response([bar("2023-01-03")])
    payload["metadata"] = {"currency": "EUR"}
    result, _ = commit_prices(security_id, payload)
    assert not result["cached"] and "currency" in result["reason"]
    assert cached(security_id) is None


def test_expired_caches_and_their_results_are_deleted_and_research_marked(security_id):
    from datetime import timedelta

    from iirp.analysis.freshness import freshness
    from iirp.market.cache import expire_price_cache
    from iirp.models import AnalysisResult, now

    dataset_id = initial_prices(security_id)
    request, lease = research_lease()
    from iirp.jobs.handlers import prepare_target
    target = prepare_target(lease)
    assert publish_research(lease, compute_research(target["params"], target["bars"], today=date(2024, 2, 1)))
    with session() as s, s.begin():
        result = s.scalar(select(AnalysisResult).where(AnalysisResult.analysis_id == request["id"]))
        assert result.expires_at == s.get(PriceCache, dataset_id).expires_at
        s.get(PriceCache, dataset_id).expires_at = now() - timedelta(seconds=1)
        result.expires_at = now() - timedelta(seconds=1)
    assert expire_price_cache() == {"price_caches": 1, "analysis_results": 1}
    with session() as s:
        assert s.scalar(select(func.count()).select_from(PriceCacheBar)) == 0
        from iirp.models import AnalysisRequest
        assert freshness(s, s.get(AnalysisRequest, request["id"]))["expired"] is True


def test_missing_fields_and_missing_sessions_remain_explicit_and_out_of_statistics(security_id):
    result, _ = commit_prices(
        security_id, response([bar("2023-01-03"), bar("2023-01-04", close=None)])
    )
    assert "2023-01-05" in result["missing_dates"]
    with session() as s:
        bars, _ = price_bars(s, security_id)
        by_day = {row["date"]: row for row in bars}
        assert by_day["2023-01-04"]["status"] == "MISSING_FIELDS"
        coverage = coverage_for(s, s.get(Security, security_id), date(2023, 1, 3), date(2023, 1, 5))
        assert coverage["status"] == "PARTIAL"
        assert coverage["valid_sessions"] == 1
        assert coverage["missing_dates"] == ["2023-01-04", "2023-01-05"]
    research = compute_research(
        {
            "kind": "interval",
            "years": [2023],
            "current_year": 2024,
            "start_mmdd": "01-03",
            "end_mmdd": "01-05",
        },
        bars,
        today=date(2024, 2, 1),
    )
    assert research["effective_n"] == 0
    assert research["rows"][0]["max_drawdown"] is None


def test_duplicate_dates_roll_back_the_whole_response(security_id):
    payload = response([bar("2023-01-03"), bar("2023-01-03", "200")])
    with pytest.raises(ValueError, match="market.duplicate_session"):
        commit_prices(security_id, payload)
    with session() as s:
        assert s.scalar(select(func.count()).select_from(PriceCacheBar)) == 0
        assert s.scalar(select(func.count()).select_from(SourceObject)) == 0
        assert s.scalar(select(func.count()).select_from(PriceCache)) == 0


def test_exception_rolls_back_cache_and_source_together(security_id):
    with pytest.raises(RuntimeError, match="Synthetic failure"):
        commit_prices(
            security_id,
            response([bar("2023-01-03", splits="2"), bar("2023-01-04"), bar("2023-01-05")]),
            fail_after=True,
        )
    with session() as s:
        for model in (PriceCache, PriceCacheBar, BatchJob, RequestScope, SourceObject):
            assert s.scalar(select(func.count()).select_from(model)) == 0
        job = s.scalar(select(Job))
        assert job.checkpoint == {} and job.progress_done == 0


def test_unexplained_50_percent_jump_is_kept_but_not_valid(security_id):
    result, _ = commit_prices(
        security_id, response([bar("2023-01-03"), bar("2023-01-04"), bar("2023-01-05", "150")]))
    assert result["invalid_dates"] == ["2023-01-05"]
    with session() as s:
        bars, _ = price_bars(s, security_id)
        assert bars[-1]["status"] == "NEEDS_REVIEW" and Decimal(bars[-1]["close"]) == 150


def test_quote_identity_refresh_preserves_separate_sec_evidence(security_id):
    from iirp.market.yahoo import resolve_metadata

    with session() as s, s.begin():
        security = s.get(Security, security_id)
        security.metadata_json = {
            "verified_fiscal_year_end": "09-26",
            "sec_identity_confirmed": True,
        }
        resolve_metadata(
            s,
            security,
            {
                "metadata": {
                    "symbol": "SYNTH",
                    "quoteType": "EQUITY",
                    "exchange": "NYQ",
                    "currency": "USD",
                }
            },
        )
        assert security.metadata_json["verified_fiscal_year_end"] == "09-26"
        assert security.metadata_json["sec_identity_confirmed"] is True


def test_daily_fallback_does_not_pair_price_with_unrelated_metadata_time():
    timestamp = datetime(2024, 1, 5, 21, tzinfo=timezone.utc)
    payload = response([bar("2024-01-04", "100"), bar("2024-01-05", "110")])
    payload["metadata"] = {
        "regularMarketTime": int(timestamp.timestamp()),
        "exchangeTimezoneName": "America/New_York",
    }
    quote = quote_from_history("^GSPC", payload)
    assert quote["as_of"] == "2024-01-05"
    assert quote["source_time"] is None
    assert quote["previous_close"] == "100"
    assert quote["value"] == 110 and quote["change_percent"] == 10
    assert quote["status"] == "DAILY"


@pytest.mark.parametrize(
    "symbol,action,action_value",
    [("AAPL", "splits", "4"), ("C", "splits", "0.1"), ("KO", "dividends", "0.485")],
)
def test_dated_provider_contract_fixtures_preserve_close_without_double_adjustment(
    security_id, symbol, action, action_value
):
    contract = source_contract()
    sample = next(item for item in contract["validation_samples"] if item["symbol"] == symbol)
    path = ROOT / sample["fixture"]
    payload = json.loads(path.read_text())
    assert hashlib.sha256(path.read_bytes()).hexdigest() == sample["fixture_sha256"]
    assert payload["raw_sha256"] == sample["raw_sha256"]
    assert payload["library_version"] == contract["library_version"]
    assert payload["official_action_source"] == sample["official_source"]
    assert payload["parameters"]["auto_adjust"] is False
    assert payload["parameters"]["back_adjust"] is False
    assert payload["parameters"]["repair"] is False
    assert payload["parameters"]["keepna"] is True
    assert Decimal(payload["records"][-1][action]) == Decimal(action_value)
    with session() as s, s.begin():
        s.get(Security, security_id).symbol = symbol
    start, end = payload["records"][0]["date"], payload["records"][-1]["date"]
    commit_prices(security_id, payload, start=start, end=end)
    with session() as s:
        bars, cache = price_bars(s, security_id)
        assert cache is not None
    for received, original in zip(bars, payload["records"], strict=True):
        assert abs(Decimal(received["close"]) - Decimal(original["close"])) < Decimal(
            "0.000000000001"
        )
        assert received["status"] == "VALID"
    if symbol == "KO":
        # The real ex-dividend sample's Close falls while Adj Close rises;
        # default price research must retain the price-only direction.
        assert Decimal(bars[-1]["close"]) < Decimal(bars[0]["close"])
        assert Decimal(bars[-1]["adj_close"]) > Decimal(bars[0]["adj_close"])


def test_adapter_preserves_explicit_bounds_and_does_not_enumerate_lazy_metadata(monkeypatch):
    import pandas as pd
    import yfinance as yf
    from iirp.market.yahoo import fetch_market

    class LazyMetadata(Mapping):
        def __iter__(self):
            raise AssertionError("Enumerating metadata may trigger tradingPeriods intraday fetch")

        def __len__(self):
            return 1

        def __getitem__(self, key):
            if key == "tradingPeriods":
                raise AssertionError("Unexpected intraday metadata")
            if key == "currency":
                return "USD"
            raise KeyError(key)

    recorded = []

    class Ticker:
        history_metadata = LazyMetadata()

        def history(self, **parameters):
            recorded.append(parameters)
            return pd.DataFrame(
                {"Open": [100], "High": [101], "Low": [99], "Close": [100], "Volume": [1]},
                index=pd.DatetimeIndex(["2024-01-03"], tz="America/New_York"),
            )

    monkeypatch.setattr(yf, "Ticker", lambda symbol, **kwargs: Ticker())
    monkeypatch.setattr(yf, "set_tz_cache_location", lambda path: None)
    result = fetch_market(
        "market_history", {"symbol": "AAPL", "start_date": "2024-01-01", "end_date": "2024-01-31"}
    )
    assert len(recorded) == 1
    assert recorded[0]["start"] == "2024-01-01" and recorded[0]["end"] == "2024-02-01"
    for key, expected in {
        "interval": "1d",
        "actions": True,
        "auto_adjust": False,
        "back_adjust": False,
        "repair": False,
        "keepna": True,
        "rounding": False,
        "prepost": False,
    }.items():
        assert recorded[0][key] == expected
    assert result["metadata"]["currency"] == "USD"
    assert "tradingPeriods" not in result["metadata"]


def research_lease(kind="interval"):
    from iirp.analysis.requests import create_analysis
    from iirp.api.schemas import AnalysisInput
    from iirp.jobs.planner import plan_tick

    params = AnalysisInput(
        request_id=str(uuid.uuid4()),
        tickers=["SYNTH"],
        kind=kind,
        historical_years=1,
        current_year=2024,
        start_mmdd="01-03",
        end_mmdd="01-05",
        comparison="complete",
    ).model_dump(mode="json")
    request = create_analysis(params)
    plan_tick()
    lease = claim({"research_compute"})
    assert lease is not None
    return request, lease


def publish_research(lease, payload):
    from iirp.jobs.handlers import _persist

    source = save_object(json.dumps(payload, sort_keys=True).encode())
    return fenced(
        lease,
        source=source,
        business_write=lambda s, current: _persist(s, current, payload, {}, source),
    )


def test_research_publish_records_cache_and_calculation_metadata(security_id):
    from iirp.jobs.handlers import prepare_target
    from iirp.models import AnalysisResult

    dataset_id = initial_prices(security_id)
    request, lease = research_lease()
    target = prepare_target(lease)
    result = compute_research(
        target["params"], target["bars"], today=date(2024, 2, 1)
    )
    assert publish_research(lease, result)
    with session() as s:
        published_result = s.scalar(
            select(AnalysisResult).where(AnalysisResult.analysis_id == request["id"])
        )
        assert published_result.input_key == lease.target["input_key"]
        assert published_result.inputs["dataset_id"] == dataset_id
        metadata = published_result.data["metadata"]
        assert metadata["dataset_id"] == dataset_id and metadata["price_fetched_at"]
        assert metadata["source"] == "synthetic"
        assert metadata["price_basis"] == "SPLIT_ONLY"
        assert metadata["calculation_version"] == "research-v10-time-source-attribution"


def test_result_from_a_replaced_cache_is_not_published(security_id):
    from iirp.jobs.handlers import prepare_target
    from iirp.models import AnalysisResult

    initial_prices(security_id)
    request, lease = research_lease()
    target = prepare_target(lease)
    result = compute_research(target["params"], target["bars"], today=date(2024, 2, 1))
    commit_prices(
        security_id, response([bar("2023-01-04", "110")]), start="2022-10-01",
        end=str(date.today()), batch_id=request["batch_id"],
    )
    assert publish_research(lease, result)
    with session() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(AnalysisResult)
                .where(AnalysisResult.analysis_id == request["id"])
            )
            == 0
        )
        assert "未发布" in zh(s.get(Job, lease.id).result["message"])
