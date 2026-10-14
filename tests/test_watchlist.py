"""Watched stock codes: kept in the database, compared like every code, local only.

Uses a disposable iirp_v1_test_* database.
"""

from datetime import datetime, timedelta

import httpx
from iirp.db import session
from iirp.market import stock_quotes, yahoo
from iirp.models import (
    Batch,
    IndexConstituent,
    Issuer,
    Job,
    Preferences,
    SourceObservation,
    WatchlistItem,
    now,
)
from sqlalchemy import func, select

from tests.jobs.test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_client,
    lifecycle_database,
)


def tickers(body):
    return [item["ticker"] for item in body["items"]]


def test_add_remove_and_dot_dash_are_one_code(lifecycle_client):  # noqa: F811
    client = lifecycle_client
    assert client.get("/api/v1/watchlist").json() == {"items": [], "limit": 500, "acknowledged_at": None}
    assert tickers(client.post("/api/v1/watchlist", json={"ticker": "brk.b"}).json()) == ["BRK-B"]
    # The same code in another spelling is already there; adding it again changes nothing.
    again = client.post("/api/v1/watchlist", json={"ticker": "BRK-B"}).json()
    assert tickers(again) == ["BRK-B"]
    assert tickers(client.post("/api/v1/watchlist", json={"ticker": " $nvda "}).json()) == ["BRK-B", "NVDA"]
    assert tickers(client.delete("/api/v1/watchlist/BRK.B").json()) == ["NVDA"]
    # Removing a code that is not watched is not an error.
    assert tickers(client.delete("/api/v1/watchlist/MSFT").json()) == ["NVDA"]
    rejected = client.post("/api/v1/watchlist", json={"ticker": "^GSPC"})
    assert rejected.status_code == 422 and rejected.json()["detail"]["code"] == "watchlist.ticker_invalid"
    with session() as s:
        assert s.scalars(select(WatchlistItem.ticker)).all() == ["NVDA"]


def test_import_reports_new_existing_and_invalid_codes_and_keeps_unknown_ones(lifecycle_client):  # noqa: F811
    client = lifecycle_client
    with session() as s, s.begin():
        s.add(Issuer(id="0000320193", name="APPLE INC", ticker="AAPL"))
        s.add(IndexConstituent(index_name="sp500", ticker="MSFT", cik="0000789019", name="Microsoft",
                               updated_at=now()))
    client.post("/api/v1/watchlist", json={"ticker": "AAPL"})
    body = client.post("/api/v1/watchlist/import",
                       json={"text": "aapl, MSFT\nBRK.B BRK-B;ZZZQ\t^GSPC,,  ES=F, toolongcodexx"}).json()
    assert body["added"] == ["MSFT", "BRK-B", "ZZZQ"]
    assert body["existing"] == ["AAPL"]
    assert body["invalid"] == ["^GSPC", "ES=F", "toolongcodexx"]
    assert body["over_limit"] == []
    items = {item["ticker"]: item for item in body["items"]}
    assert list(items) == ["AAPL", "BRK-B", "MSFT", "ZZZQ"]
    assert (items["AAPL"]["name"], items["AAPL"]["issuer_id"], items["AAPL"]["local"]) == ("APPLE INC", "0000320193", True)
    # The index list's name comes first; its CIK opens the company page.
    assert (items["MSFT"]["name"], items["MSFT"]["issuer_id"]) == ("Microsoft", "0000789019")
    # Nothing local knows ZZZQ: it is kept all the same, without a name.
    assert (items["ZZZQ"]["name"], items["ZZZQ"]["issuer_id"], items["ZZZQ"]["local"]) == (None, None, False)
    assert client.post("/api/v1/watchlist/import", json={"text": "x" * 10_001}).status_code == 422


def test_at_most_five_hundred_codes(lifecycle_client):  # noqa: F811
    client = lifecycle_client
    with session() as s, s.begin():
        s.add_all(WatchlistItem(ticker=f"T{index:03d}") for index in range(498))
    body = client.post("/api/v1/watchlist/import", json={"text": "AAA BBB CCC T001"}).json()
    assert (body["added"], body["existing"], body["over_limit"]) == (["AAA", "BBB"], ["T001"], ["CCC"])
    full = client.post("/api/v1/watchlist", json={"ticker": "CCC"})
    assert full.status_code == 422 and full.json()["detail"] == {"code": "watchlist.full", "params": {"limit": 500}}
    # A code already watched can still be "added" when the list is full.
    assert client.post("/api/v1/watchlist", json={"ticker": "AAA"}).status_code == 200
    client.delete("/api/v1/watchlist/AAA")
    assert client.post("/api/v1/watchlist", json={"ticker": "CCC"}).status_code == 200
    with session() as s:
        assert s.scalar(select(func.count()).select_from(WatchlistItem)) == 500


def test_acknowledgement_is_kept_on_the_server_and_only_moves_forward(lifecycle_client):  # noqa: F811
    client = lifecycle_client
    first = now() - timedelta(days=7)
    body = client.put("/api/v1/watchlist/acknowledged", json={"at": first.isoformat()}).json()
    assert body["acknowledged_at"] == first.isoformat().replace("+00:00", "Z")
    # An older time (another browser that read earlier) never moves it back.
    older = client.put("/api/v1/watchlist/acknowledged", json={"at": (first - timedelta(days=1)).isoformat()}).json()
    assert older["acknowledged_at"] == body["acknowledged_at"]
    # A time in the future is taken as now.
    later = client.put("/api/v1/watchlist/acknowledged", json={"at": (now() + timedelta(days=3)).isoformat()}).json()
    assert first < datetime.fromisoformat(later["acknowledged_at"]) <= now()
    assert client.put("/api/v1/watchlist/acknowledged", json={"at": "2026-10-01T00:00:00"}).status_code == 422
    # Every browser reads the same time; the general preferences do not show it.
    assert client.get("/api/v1/watchlist").json()["acknowledged_at"] == later["acknowledged_at"]
    assert "watchlist" not in client.get("/api/v1/preferences").json()["values"]
    with session() as s:
        assert "acknowledged_at" in s.get(Preferences, 1).values["watchlist"]


def test_nothing_is_fetched_or_queued(lifecycle_client, monkeypatch):  # noqa: F811
    def forbidden(*args, **kwargs):
        raise AssertionError("the watchlist must not ask a source")

    monkeypatch.setattr(yahoo, "fetch_market", forbidden)
    monkeypatch.setattr(stock_quotes, "fetch_stock_quotes", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    client = lifecycle_client
    client.post("/api/v1/watchlist", json={"ticker": "NVDA"})
    client.post("/api/v1/watchlist/import", json={"text": "AAPL MSFT NOLOCAL"})
    client.get("/api/v1/watchlist")
    client.put("/api/v1/watchlist/acknowledged", json={"at": now().isoformat()})
    client.delete("/api/v1/watchlist/NVDA")
    with session() as s:
        assert s.scalar(select(func.count()).select_from(SourceObservation)) == 0
        assert s.scalar(select(func.count()).select_from(Job)) == 0
        assert s.scalar(select(func.count()).select_from(Batch)) == 0
