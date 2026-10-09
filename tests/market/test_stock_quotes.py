"""Visible-stock batching, persistent reuse and D22 stock quote selection."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from iirp.db import session
from iirp.jobs.queue import claim, fenced, should_yield_to_manual
from iirp.market import stock_quotes
from iirp.market.stock_quotes import (
    KIND,
    fetch_stock_quotes,
    persist_stock_quotes,
    request_stock_quotes,
    stock_quote,
    stock_quote_views,
    stock_session,
)
from iirp.models import Batch, BatchJob, Job, MarketQuote, RequestScope, SourceBudget
from sqlalchemy import select

from tests.market.test_market_lifecycle import clean_market, market_database  # noqa: F401


def at(value):
    return datetime.fromisoformat(value)


@pytest.mark.parametrize(("instant", "period", "due"), [
    ("2026-10-07T15:00:00+00:00", "regular", "2026-10-07T15:01:00+00:00"),
    ("2026-10-07T12:00:00+00:00", "pre", "2026-10-07T12:05:00+00:00"),
    ("2026-10-07T21:00:00+00:00", "post", "2026-10-07T21:05:00+00:00"),
    ("2026-10-10T15:00:00+00:00", "closed", "2026-10-12T08:00:00+00:00"),
    # Black Friday closes at 13:00 Eastern, not at the ordinary 16:00.
    ("2026-11-27T18:05:00+00:00", "post", "2026-11-27T18:10:00+00:00"),
])
def test_stock_refresh_uses_exchange_sessions(instant, period, due):
    actual_period, actual_due = stock_session(at(instant))
    assert actual_period == period and actual_due == at(due)


def test_closed_quotes_use_regular_close_and_extended_quotes_use_paired_time():
    raw = {"regularMarketPrice": 100, "regularMarketTime": at("2026-10-07T20:00:00+00:00").timestamp(),
           "postMarketPrice": 101, "postMarketTime": at("2026-10-07T21:00:00+00:00").timestamp()}
    extended = stock_quote("SYNTH", raw, at("2026-10-07T21:01:00+00:00"))
    assert extended["value"] == "101" and extended["price_session"] == "post"
    closed = stock_quote("SYNTH", raw, at("2026-10-08T01:00:00+00:00"))
    assert closed["value"] == "100" and closed["as_of"] == "2026-10-07"
    assert closed["status"] == "CLOSED"
    raw["postMarketTime"] = at("2026-10-08T21:00:00+00:00").timestamp()
    assert stock_quote("SYNTH", raw, at("2026-10-07T21:01:00+00:00"))["value"] is None


def test_all_visible_symbols_use_one_quote_endpoint_call(monkeypatch):
    from iirp.market import http

    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
            json=lambda: {"quoteResponse": {"result": [{"symbol": "AAPL"}, {"symbol": "MSFT"}], "error": None}})
    monkeypatch.setattr(stock_quotes, "_crumb", "synthetic")
    monkeypatch.setattr(http, "timed_session", lambda: SimpleNamespace(
        reset=lambda: None, snapshot=lambda: {"http_requests": len(calls)}, session=SimpleNamespace(get=get)))
    result = fetch_stock_quotes({"symbols": ["MSFT", "AAPL", "MSFT"]})
    assert len(calls) == 1 and calls[0][1]["params"]["symbols"] == "AAPL,MSFT"
    assert result["symbols"] == ["AAPL", "MSFT"]
    assert "crumb" not in result and result["timing"]["http_requests"] == 1


def test_visible_stock_requests_reuse_overlap_and_fresh_cache(monkeypatch):
    current = at("2026-10-07T15:00:00+00:00")
    monkeypatch.setattr(stock_quotes, "now", lambda: current)
    with session() as s, s.begin():
        first = request_stock_quotes(s, ["AAPL", "MSFT"])
        assert request_stock_quotes(s, ["MSFT", "AAPL"]) == first
        overlapping = request_stock_quotes(s, ["MSFT", "NVDA"])
        assert first[0] in overlapping and len(overlapping) == 2
        targets = [job.target["symbols"] for job in s.scalars(select(Job).where(Job.kind == KIND))]
        assert sorted(targets) == [["AAPL", "MSFT"], ["NVDA"]]
        s.add(MarketQuote(symbol="GOOG", data={"next_refresh_at": (current + timedelta(seconds=60)).isoformat()}, fetched_at=current))
        s.flush()
        assert request_stock_quotes(s, ["GOOG"]) == []


def test_failed_symbol_is_negatively_cached_and_older_response_cannot_replace_price(monkeypatch):
    current = at("2026-10-07T15:00:00+00:00")
    monkeypatch.setattr(stock_quotes, "now", lambda: current)
    target = SimpleNamespace(target={"symbols": ["AAPL", "MISSING"]})
    response = {"fetched_at": current.isoformat(), "quotes": [
        {"symbol": "AAPL", "regularMarketPrice": 100, "regularMarketTime": current.timestamp()}]}
    with session() as s, s.begin():
        assert persist_stock_quotes(s, target, response) == {"quoted": 1, "missing_symbols": ["MISSING"]}
        assert request_stock_quotes(s, ["MISSING"]) == []
        earlier = current - timedelta(minutes=1)
        persist_stock_quotes(s, target, {"fetched_at": earlier.isoformat(), "quotes": [
            {"symbol": "AAPL", "regularMarketPrice": 99, "regularMarketTime": earlier.timestamp()}]})
        assert s.get(MarketQuote, "AAPL").data["value"] == "100"


def test_missing_refresh_retains_prior_price_and_closed_read_selects_regular_close(monkeypatch):
    current = at("2026-10-07T21:00:00+00:00")
    monkeypatch.setattr(stock_quotes, "now", lambda: current)
    target = SimpleNamespace(target={"symbols": ["AAPL"]})
    raw = {"symbol": "AAPL", "regularMarketPrice": 100,
           "regularMarketTime": at("2026-10-07T20:00:00+00:00").timestamp(),
           "postMarketPrice": 101, "postMarketTime": current.timestamp()}
    with session() as s, s.begin():
        persist_stock_quotes(s, target, {"fetched_at": current.isoformat(), "quotes": [raw]})
        persist_stock_quotes(s, target, {"fetched_at": (current + timedelta(minutes=5)).isoformat(), "quotes": []})
        assert s.get(MarketQuote, "AAPL").data["value"] == "101"
    monkeypatch.setattr(stock_quotes, "now", lambda: at("2026-10-08T01:00:00+00:00"))
    with session() as s, s.begin():
        closed = stock_quote_views(s, ["AAPL"])[0]
        assert closed["value"] == "100" and closed["status"] == "CLOSED"
        assert request_stock_quotes(s, ["AAPL"]) == []


def test_an_obsolete_lease_cannot_publish_current_quotes():
    with session() as s, s.begin():
        identifiers = request_stock_quotes(s, ["AAPL"])
        current = s.get(Job, identifiers[0])
        current.status, current.lease_token = "RUNNING", "new-token"
        current.lease_until = stock_quotes.now() + timedelta(seconds=30)
        stale = SimpleNamespace(id=current.id, kind=KIND, lease_token="old-token", control_version=0)
    assert not fenced(stale, business_write=lambda s, current: persist_stock_quotes(s, current, {
        "fetched_at": stock_quotes.now().isoformat(), "quotes": []}))
    with session() as s:
        assert s.get(MarketQuote, "AAPL") is None


def test_overview_reads_are_not_limited_to_a_visible_request_batch():
    symbols = [f"SYNTH{i}" for i in range(201)]
    with session() as s:
        assert len(stock_quote_views(s, symbols)) == 201


def test_opening_another_tab_preserves_a_paused_quote_job():
    with session() as s, s.begin():
        identifiers = request_stock_quotes(s, ["AAPL"])
        job = s.get(Job, identifiers[0])
        job.status, job.requested_action = "PAUSED", "pause"
        s.flush()
        assert request_stock_quotes(s, ["AAPL"]) == identifiers
        assert job.status == "PAUSED" and job.requested_action == "pause"


def manual_market_history(s):
    batch = Batch(request_id=str(uuid4()), scope_key=uuid4().hex, kind="market_history",
                  title="Synthetic history demand", params={}, trigger="manual")
    s.add(batch)
    s.flush()
    scope = RequestScope(batch_id=batch.id, symbol="SYNTH")
    job = Job(kind="market_history", title="Synthetic history fetch", target={"symbol": "SYNTH"},
              idempotency_key=uuid4().hex, priority=10)
    s.add_all([scope, job])
    s.flush()
    s.add(BatchJob(scope_id=scope.id, job_id=job.id, active=True))
    return job.id


def test_visible_quotes_are_claimed_ahead_of_manual_history_in_the_same_lane():
    with session() as s, s.begin():
        history_id = manual_market_history(s)
        quote_id = request_stock_quotes(s, ["AAPL", "MSFT"])[0]
    claimed = claim({KIND, "market_identity", "market_history", "market_quote"})
    assert claimed is not None and claimed.id == quote_id
    assert not should_yield_to_manual(claimed)
    with session() as s:
        assert s.get(Job, history_id).status == "QUEUED"


def test_direct_quote_subscription_is_manual_demand_and_automatic_quotes_yield():
    with session() as s, s.begin():
        manual_market_history(s)
        quote_id = request_stock_quotes(s, ["AAPL"])[0]
        automatic = Job(kind="market_quote", title="Synthetic automatic quote",
                        target={"symbol": "SYNTH"}, idempotency_key=uuid4().hex,
                        priority=0, trigger="automatic", status="RUNNING",
                        lease_token=str(uuid4()), lease_until=stock_quotes.now() + timedelta(seconds=30))
        s.add(automatic)
        s.flush()
        direct_quote = s.get(Job, quote_id)
    assert not should_yield_to_manual(direct_quote)
    assert should_yield_to_manual(automatic)
    # The direct quote subscription alone also preempts background source work.
    with session() as s, s.begin():
        for batch in s.scalars(select(Batch)):
            batch.status = "SUCCEEDED"
    assert should_yield_to_manual(automatic)


def test_stock_quotes_obey_the_existing_shared_yahoo_cooldown():
    with session() as s, s.begin():
        quote_id = request_stock_quotes(s, ["AAPL"])[0]
        s.add(SourceBudget(provider="yfinance", failures=1,
                           next_allowed_at=stock_quotes.now() + timedelta(minutes=5)))
    assert claim({KIND, "market_history", "market_quote"}) is None
    with session() as s:
        job = s.get(Job, quote_id)
        assert job.status == "QUEUED" and job.attempts == 0
