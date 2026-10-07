"""Insider lookup, before/after-n windows from the price cache, and price requests."""

from datetime import date, timedelta
from decimal import Decimal

from iirp.analysis.calendar import last_completed_session, session_window
from iirp.analysis.insider_windows import provider_symbol, request_trade_prices, trade_windows
from iirp.db import session
from iirp.insider.lookup import local_lookup
from iirp.models import Batch, Issuer, PriceCacheBar, Security, TransactionEvent
from sqlalchemy import select, update

from tests.insider.test_transaction_mapping import _seed
from tests.jobs.test_lifecycle import seed_prices
from tests.sec.test_sec_facts import isolated_database  # noqa: F401


def _demo():
    event_id, security_id, _ = _seed()
    with session() as s:
        event = s.get(TransactionEvent, event_id)
        return event_id, security_id, event.issuer_id


def test_provider_symbol_uses_yahoo_class_separator_and_rejects_placeholders():
    assert provider_symbol("brk.b") == "BRK-B"
    assert provider_symbol("NONE") is None
    assert provider_symbol("AAPL, AAPLW") is None


def test_windows_follow_d16_and_mark_future_and_unconfirmed_tickers():
    _, security_id, issuer_id = _demo()
    completed = last_completed_session()
    days = session_window(completed, 30, 0)
    seed_prices(security_id, days[0], completed, wide=True)
    trade = days[20]
    with session() as s, s.begin():
        # Closes rise by 1 per session: C(t)/C(t−n) − 1 and C(t+n)/C(t) − 1 are exact.
        bars = s.scalars(select(PriceCacheBar).join(Security, Security.id == security_id)
                         .where(PriceCacheBar.session_date.in_(days))).all()
        for bar in bars:
            bar.close = Decimal(100 + days.index(bar.session_date))
    with session() as s:
        result = trade_windows(s, [{"ticker": "DEMO", "date": trade.isoformat(), "issuer_id": issuer_id},
                                   {"ticker": "DEMO", "date": completed.isoformat(), "issuer_id": issuer_id}], 5)
    first, latest = result["items"]
    assert abs(Decimal(first["before"]["value"]) - (Decimal(120) / Decimal(115) - 1)) < Decimal("1e-20")
    assert abs(Decimal(first["after"]["value"]) - (Decimal(125) / Decimal(120) - 1)) < Decimal("1e-20")
    assert (first["before"]["start_date"], first["after"]["end_date"]) == (days[15].isoformat(), days[25].isoformat())
    # t+n has not happened yet: empty with its expected date.
    assert latest["after"]["status"] in {"pending", "intraday"} and latest["after"]["value"] is None
    assert latest["after"]["end_date"] > completed.isoformat()
    assert result["tickers"]["DEMO"]["status"] == "ready" and result["tickers"]["DEMO"]["confirmed"] is True
    with session() as s:
        other = trade_windows(s, [{"ticker": "DEMO", "date": trade.isoformat(), "issuer_id": "0000000001"}], 5)
    assert other["tickers"]["DEMO"]["confirmed"] is False
    assert other["items"][0]["before"]["status"] == "available"


def test_price_request_plans_one_fetch_and_repeats_add_nothing():
    _, security_id, issuer_id = _demo()
    with session() as s, s.begin():
        s.execute(update(Security).where(Security.id == security_id).values(symbol="DEMOX"))
    item = {"ticker": "DEMOX", "date": (date.today() - timedelta(days=20)).isoformat(), "issuer_id": issuer_id}
    with session() as s, s.begin():
        first = request_trade_prices(s, [dict(item)], 5)
    with session() as s, s.begin():
        again = request_trade_prices(s, [dict(item)], 5)
    assert first["requested"] == ["DEMOX"] and first["batch_ids"] == again["batch_ids"]
    with session() as s:
        batch = s.get(Batch, first["batch_ids"][0])
        assert batch.kind == "market_history" and batch.trigger == "automatic"
        assert batch.params["tickers"] == ["DEMOX"]
        # The default six-month view is covered by the same single request.
        assert batch.params["start_date"] <= (date.today() - timedelta(days=183)).isoformat()


def test_unknown_ticker_without_security_is_not_fetched_until_asked():
    with session() as s:
        result = trade_windows(s, [{"ticker": "ZZZQ", "date": date.today().isoformat()}], 5)
    assert result["tickers"]["ZZZQ"]["status"] == "not_fetched"


def test_local_lookup_by_ticker_and_name_words():
    _, _, issuer_id = _demo()
    with session() as s, s.begin():
        issuer = s.get(Issuer, issuer_id)
        issuer.ticker = "DEMO"
    with session() as s:
        by_ticker = local_lookup(s, "demo")
        assert by_ticker["companies"][0]["cik"] == issuer_id
        name = s.get(Issuer, issuer_id).name.split()[0]
        assert any(row["cik"] == issuer_id for row in local_lookup(s, name.lower())["companies"])
