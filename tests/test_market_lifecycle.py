"""Synthetic market/version publication tests on their own PostgreSQL database."""

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
from iirp.analytics.research import compute_research
from iirp.business_models import (
    BatchJob,
    CorporateAction,
    CoverageSegment,
    DatasetBar,
    MarketBar,
    PriceDataset,
    RequestScope,
    Security,
)
from iirp.config import ROOT, settings
from iirp.db import engine, session
from iirp.market_data import (
    coverage_for,
    latest_dataset,
    persist_prices,
    price_bars,
    quote_from_history,
    source_contract,
)
from iirp.models import Base, Job, SourceObject
from iirp.queue import claim, fenced
from iirp.storage import save_object
from psycopg import sql
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url


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


def response(records, *, verified=True, evidence="synthetic-contract-A"):
    return {
        "provider": "synthetic",
        "library_version": "fixture",
        "contract": {"verified": verified, "evidence": evidence, "basis": "SPLIT_ONLY"},
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
            # This price revision belongs to the same manual research; a naked
            # priority=-100 fixture must not bypass provider demand ownership.
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
        results.append(persist_prices(s, current, payload, source["sha256"]))
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


def published(security_id):
    with session() as s:
        return latest_dataset(s, security_id)


def initial_prices(security_id, *, post_split_close="100"):
    commit_prices(
        security_id,
        response([bar("2023-01-03"), bar("2023-01-04"), bar("2023-01-05", post_split_close)]),
    )
    return published(security_id).id


def test_market_detail_uses_real_split_only_price_range(security_id):
    from iirp.analytics.calendar import sessions
    from iirp.business_models import MarketQuote
    from iirp.lifecycle import market_detail

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
    assert result["data"]["chart_dataset_id"] == published(security_id).id
    assert result["data"]["as_of"] == "2023-01-05"
    assert result["data"]["chart_end"] == str(days[-1])
    assert result["data"]["chart_source"] == "synthetic"
    assert "仅拆股调整" in result["data"]["chart_basis"]


def test_unverified_contract_preserves_observations_but_cannot_feed_statistics(security_id):
    result, source = commit_prices(
        security_id,
        response([bar("2023-01-03"), bar("2023-01-04"), bar("2023-01-05")], verified=False),
    )
    assert not result["eligible"]
    with session() as s:
        bars, dataset = price_bars(s, security_id)
        assert bars == [] and dataset is None
        assert s.scalar(select(func.count()).select_from(MarketBar)) == 3
        assert s.get(SourceObject, source["sha256"]) is not None
        segment = s.scalar(select(CoverageSegment))
        assert segment.status == "PARTIAL"
        assert "口径待核对" in segment.details["reason"]
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


def test_verified_prices_and_source_foreign_key_publish_in_one_fenced_transaction(security_id):
    result, source = commit_prices(
        security_id,
        response([bar("2023-01-03"), bar("2023-01-04", "110"), bar("2023-01-05", "90")]),
    )
    assert result["eligible"]
    with session() as s:
        bars, dataset = price_bars(s, security_id)
        assert dataset.status == "PUBLISHED" and dataset.basis == "SPLIT_ONLY"
        assert all(row["status"] == "VALID" for row in bars)
        assert {row.source_hash for row in s.scalars(select(MarketBar))} == {source["sha256"]}
        coverage = coverage_for(s, s.get(Security, security_id), date(2023, 1, 3), date(2023, 1, 5))
        assert coverage["status"] == "COMPLETE" and coverage["valid_sessions"] == 3


@pytest.mark.parametrize("phase", ["before_save", "after_save", "before_commit", "after_commit"])
def test_sigkill_price_publication_is_atomic_and_reuses_saved_source(security_id, tmp_path, phase):
    import signal
    import subprocess
    import sys
    import time
    from datetime import timedelta

    from iirp.models import now

    payload = response([bar("2023-01-03"), bar("2023-01-04"), bar("2023-01-05")])
    lease = pending_job(security_id)
    request_file = tmp_path / "fault-request.json"
    request_file.write_text(json.dumps({"job_id": lease.id, "payload": payload, "phase": phase}))
    marker = tmp_path / "reached"
    script = tmp_path / "fault-child.py"
    script.write_text("""
import json,sys,time
from pathlib import Path
from iirp.db import session
from iirp.models import Job
from iirp.queue import fenced
from iirp.storage import save_object
from iirp.market_data import persist_prices
request=json.loads(Path(sys.argv[1]).read_text())
def stop_at(phase):
    if request['phase']==phase:
        Path(sys.argv[2]).write_text(phase)
        time.sleep(15)
with session() as s:
    job=s.get(Job,request['job_id'])
stop_at('before_save')
source=save_object(json.dumps(request['payload'],sort_keys=True).encode())
stop_at('after_save')
def write(s,current):
    persist_prices(s,current,request['payload'],source['sha256'])
    s.flush()
    stop_at('before_commit')
assert fenced(job,source=source,business_write=write,status='SUCCEEDED',done=1)
stop_at('after_commit')
""")
    process = subprocess.Popen(
        [sys.executable, str(script), str(request_file), str(marker)],
        env={**os.environ, "IIRP_RUNTIME_DIR": str(tmp_path), "PYTHONPATH": str(ROOT / "backend")},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.02)
        assert marker.exists(), f"child never reached {phase}"
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=3)
        with session() as s, s.begin():
            count = s.scalar(select(func.count()).select_from(MarketBar))
            assert count == (3 if phase == "after_commit" else 0)
            if phase != "after_commit":
                s.get(Job, lease.id).lease_until = now() - timedelta(seconds=1)
        source = save_object(json.dumps(payload, sort_keys=True).encode())
        if phase != "after_commit":
            recovered = claim({"market_history"})
            assert recovered.id == lease.id and recovered.lease_token != lease.lease_token
            assert not fenced(lease, status="SUCCEEDED")
            assert fenced(
                recovered,
                source=source,
                status="SUCCEEDED",
                done=1,
                business_write=lambda s, current: persist_prices(
                    s, current, payload, source["sha256"]
                ),
            )
        with session() as s:
            assert s.scalar(select(func.count()).select_from(MarketBar)) == 3
            assert s.scalar(select(func.count()).select_from(PriceDataset)) == 1
            assert s.scalar(select(func.count()).select_from(CoverageSegment)) == 1
            assert s.scalar(select(func.count()).select_from(SourceObject)) == 1
            assert s.get(Job, lease.id).status == "SUCCEEDED"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def test_quote_identity_refresh_preserves_separate_sec_evidence(security_id):
    from iirp.market_data import resolve_metadata

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


def test_split_keeps_old_version_readable_until_full_new_basis_is_valid(security_id):
    old = initial_prices(security_id)
    result, _ = commit_prices(
        security_id, response([bar("2023-01-05", "50", splits="2")]), start="2023-01-05"
    )
    assert result["rebase"]
    assert published(security_id).id == old
    with session() as s:
        pending = s.scalar(select(PriceDataset).where(PriceDataset.status == "BUILDING"))
        assert pending is not None
        assert (
            s.scalar(
                select(func.count())
                .select_from(DatasetBar)
                .where(DatasetBar.dataset_id == pending.id)
            )
            == 1
        )
    commit_prices(
        security_id, response([bar("2023-01-03", "50"), bar("2023-01-04", "50")]), end="2023-01-04"
    )
    assert published(security_id).id != old
    with session() as s:
        bars, _ = price_bars(s, security_id)
        assert len(bars) == 3 and {Decimal(row["close"]) for row in bars} == {Decimal(50)}
        old_bars, _ = price_bars(s, security_id, old)
        assert {Decimal(row["close"]) for row in old_bars} == {Decimal(100)}


def test_rebase_date_presence_does_not_publish_invalid_ohlc(security_id):
    old = initial_prices(security_id)
    commit_prices(security_id, response([bar("2023-01-05", "50", splits="2")]), start="2023-01-05")
    commit_prices(
        security_id,
        response([bar("2023-01-03", "50"), bar("2023-01-04", "50", low="60")]),
        end="2023-01-04",
    )
    assert published(security_id).id == old, (
        "All dates present is insufficient when one linked bar is INVALID"
    )
    with session() as s:
        assert s.scalar(select(PriceDataset).where(PriceDataset.status == "BUILDING")) is not None
    commit_prices(
        security_id, response([bar("2023-01-04", "50")]), start="2023-01-04", end="2023-01-04"
    )
    assert published(security_id).id != old


def test_prior_unverified_split_observation_cannot_suppress_verified_rebuild(security_id):
    old = initial_prices(security_id)
    commit_prices(
        security_id,
        response([bar("2023-01-05", "50", splits="2")], verified=False),
        start="2023-01-05",
    )
    result, _ = commit_prices(
        security_id, response([bar("2023-01-05", "50", splits="2")]), start="2023-01-05"
    )
    assert result["rebase"]
    assert published(security_id).id == old
    with session() as s:
        assert s.scalar(select(PriceDataset).where(PriceDataset.status == "BUILDING")) is not None


def test_second_split_resets_pending_adjustment_epoch_before_publication(security_id):
    old = initial_prices(security_id)
    commit_prices(security_id, response([bar("2023-01-05", "50", splits="2")]), start="2023-01-05")
    commit_prices(
        security_id,
        response([bar("2023-01-06", "25", splits="2")]),
        start="2023-01-06",
        end="2023-01-06",
    )
    commit_prices(
        security_id, response([bar("2023-01-03", "25"), bar("2023-01-04", "25")]), end="2023-01-04"
    )
    assert published(security_id).id == old, (
        "First split's Jan 5 price cannot count toward the second split's version"
    )
    commit_prices(
        security_id,
        response([bar("2023-01-05", "25", splits="2")]),
        start="2023-01-05",
        end="2023-01-05",
    )
    assert published(security_id).id != old
    with session() as s:
        bars, _ = price_bars(s, security_id)
        assert len(bars) == 4 and {Decimal(row["close"]) for row in bars} == {Decimal(25)}


def test_rebase_accepts_reverified_unchanged_rows_in_new_complete_manifest(security_id):
    old = initial_prices(security_id)
    commit_prices(security_id, response([bar("2023-01-05", "100", splits="2")]), start="2023-01-05")
    commit_prices(
        security_id,
        response(
            [bar("2023-01-03", "60"), bar("2023-01-04", "60"), bar("2023-01-05", "100", splits="2")]
        ),
    )
    assert published(security_id).id != old, (
        "Revalidated unchanged post-split row must count toward a rebuilt version"
    )
    with session() as s:
        assert len(price_bars(s, security_id)[0]) == 3


def test_existing_rebase_rejects_mixing_another_verified_contract(security_id):
    old = initial_prices(security_id)
    commit_prices(security_id, response([bar("2023-01-05", "50", splits="2")]), start="2023-01-05")
    try:
        commit_prices(
            security_id,
            response(
                [bar("2023-01-03", "50"), bar("2023-01-04", "50")], evidence="synthetic-contract-B"
            ),
            end="2023-01-04",
        )
    except ValueError:
        pass  # Rejecting an incompatible response explicitly is also valid.
    assert published(security_id).id == old, (
        "One version cannot combine A's split day with B's history"
    )


def test_contract_basis_change_does_not_replace_complete_history_with_recent_fragment(security_id):
    old = initial_prices(security_id)
    commit_prices(
        security_id,
        response([bar("2023-01-05", "55")], evidence="synthetic-contract-B"),
        start="2023-01-05",
    )
    assert published(security_id).id == old


def test_full_revalidation_under_new_contract_can_publish_unchanged_prices(security_id):
    old = initial_prices(security_id)
    old_basis = published(security_id).basis_key
    commit_prices(
        security_id,
        response(
            [bar("2023-01-03"), bar("2023-01-04"), bar("2023-01-05")],
            evidence="synthetic-contract-B",
        ),
    )
    current = published(security_id)
    assert current.id != old and current.basis_key != old_basis


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


def test_duplicate_dates_rollback_first_flushed_row_and_source_reference(security_id):
    payload = response([bar("2023-01-03"), bar("2023-01-03", "200")])
    with pytest.raises(ValueError, match="重复"):
        commit_prices(security_id, payload)
    with session() as s:
        assert s.scalar(select(func.count()).select_from(MarketBar)) == 0
        assert s.scalar(select(func.count()).select_from(SourceObject)) == 0
        assert s.scalar(select(func.count()).select_from(PriceDataset)) == 0


def test_exception_rolls_back_bars_actions_versions_coverage_and_source_together(security_id):
    with pytest.raises(RuntimeError, match="Synthetic failure"):
        commit_prices(
            security_id,
            response([bar("2023-01-03", splits="2"), bar("2023-01-04"), bar("2023-01-05")]),
            fail_after=True,
        )
    with session() as s:
        for model in (
            MarketBar,
            BatchJob,
    CorporateAction,
            PriceDataset,
    RequestScope,
            DatasetBar,
            CoverageSegment,
            SourceObject,
        ):
            assert s.scalar(select(func.count()).select_from(model)) == 0
        job = s.scalar(select(Job))
        assert job.checkpoint == {} and job.progress_done == 0


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
    commit_prices(security_id, {**payload, "contract": contract}, start=start, end=end)
    with session() as s:
        bars, dataset = price_bars(s, security_id)
        assert dataset is not None and dataset.basis == "SPLIT_ONLY"
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
    from iirp.market_data import fetch_market

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


def test_unexplained_50_percent_revision_keeps_previous_qualified_close(security_id):
    old = initial_prices(security_id)
    result, _ = commit_prices(security_id, response([bar("2023-01-05", "150")]), start="2023-01-05")
    assert published(security_id).id == old
    assert result["invalid_dates"] == ["2023-01-05"]
    with session() as s:
        observation = s.scalar(select(MarketBar).where(MarketBar.close == Decimal(150)))
        assert observation.status == "NEEDS_REVIEW"
        bars, _ = price_bars(s, security_id)
        assert Decimal(bars[-1]["close"]) == 100


def research_lease(kind="interval"):
    from iirp.contracts import AnalysisInput
    from iirp.lifecycle import create_analysis, plan_tick

    params = AnalysisInput(
        request_id=str(uuid.uuid4()),
        tickers=["SYNTH"],
        kind=kind,
        historical_years=1,
        current_year=2024,
        current_fiscal_year=2024,
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
    from iirp.business_worker import _persist

    source = save_object(json.dumps(payload, sort_keys=True).encode())
    return fenced(
        lease,
        source=source,
        business_write=lambda s, current: _persist(s, current, payload, {}, source),
    )


def test_research_publish_records_exact_price_and_calculation_metadata(security_id):
    from iirp.business_models import AnalysisResult
    from iirp.business_worker import prepare_target

    dataset_id = initial_prices(security_id)
    request, lease = research_lease()
    target = prepare_target(lease)
    result = compute_research(
        target["params"], target["bars"], events=target["events"], today=date(2024, 2, 1)
    )
    assert publish_research(lease, result)
    with session() as s:
        published_result = s.scalar(
            select(AnalysisResult).where(AnalysisResult.analysis_id == request["id"])
        )
        assert published_result.input_key == lease.target["input_key"]
        assert published_result.inputs["dataset_id"] == dataset_id
        metadata = published_result.data["metadata"]
        assert metadata["dataset_id"] == metadata["data_version"] == dataset_id
        assert metadata["source"] == "synthetic"
        assert metadata["price_basis"] == "SPLIT_ONLY"
        assert metadata["calculation_version"] == "research-v10-time-source-attribution"


def test_stale_research_input_is_not_published_after_new_prices(security_id):
    from iirp.business_models import AnalysisResult
    from iirp.business_worker import prepare_target

    initial_prices(security_id)
    request, lease = research_lease()
    target = prepare_target(lease)
    result = compute_research(target["params"], target["bars"], today=date(2024, 2, 1))
    commit_prices(
        security_id, response([bar("2023-01-04", "110")]), start="2023-01-04", end="2023-01-04",
        batch_id=request["batch_id"],
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
        assert "未发布" in s.get(Job, lease.id).result["message"]


def test_earnings_event_snapshot_is_frozen_and_old_revision_cannot_publish(security_id):
    from iirp.business_models import AnalysisResult, EarningsEvent
    from iirp.business_worker import prepare_target

    initial_prices(security_id)
    with session() as s, s.begin():
        event = EarningsEvent(
            security_id=security_id,
            fiscal_year=2023,
            fiscal_quarter=1,
            announced_date=date(2023, 1, 4),
            time_precision="before_open",
            verified=True,
        )
        s.add(event)
        s.flush()
        event_id = event.id
    request, lease = research_lease("earnings")
    with session() as s, s.begin():
        event = s.get(EarningsEvent, event_id)
        event.time_precision = "after_close"
        event.revision += 1
    target = prepare_target(lease)
    assert target["events"][0]["revision"] == 1
    assert target["events"][0]["time_precision"] == "before_open"
    result = compute_research(
        target["params"], target["bars"], events=target["events"], today=date(2024, 2, 1)
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
