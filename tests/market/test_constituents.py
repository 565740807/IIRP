"""D25 source parsing and preservation of membership on failed weekly updates."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from iirp.db import session
from iirp.market.constituents import (
    due_indices,
    fetch_index,
    parse_nasdaq100,
    parse_sp500,
    persist_failure,
    persist_success,
)
from iirp.messages import UserError
from iirp.models import IndexConstituent, IndexConstituentState, Issuer
from sqlalchemy import select

from tests.jobs.test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401


def test_csv_keeps_gics_sector_cik_and_both_share_classes():
    rows = parse_sp500("Symbol,Security,GICS Sector,CIK\n"
                       "GOOGL,Alphabet Class A,Communication Services,1652044\n"
                       "GOOG,Alphabet Class C,Communication Services,1652044\n"
                       'BRK.B,"Berkshire, Inc.",Financials,1067983\n')
    assert [row["ticker"] for row in rows] == ["GOOGL", "GOOG", "BRK.B"]
    assert rows[0]["cik"] == "0001652044"
    assert rows[0]["industry"] == "Communication Services"
    assert rows[2]["name"] == "Berkshire, Inc."


def test_wikipedia_finds_current_component_columns_and_ignores_citations():
    rows = parse_nasdaq100("""
        <table><tr><th>Year</th><th>Return</th></tr><tr><td>2025</td><td>20%</td></tr></table>
        <table class="wikitable sortable"><tr><th>Ticker</th><th>Company</th>
        <th><a>ICB</a> Industry<sup>[1]</sup></th><th>ICB Subsector</th></tr>
        <tr><td>AAPL</td><td><a>Apple Inc.</a></td><td>Technology</td><td>Computer Hardware</td></tr>
        <tr><td>GOOG</td><td>Alphabet Inc. (Class C)<sup>[4]</sup></td><td>Technology</td><td>Software</td></tr>
        </table>""")
    assert len(rows) == 2
    assert rows[0] == {"index_name": "nasdaq100", "ticker": "AAPL", "cik": None,
                       "name": "Apple Inc.", "industry": "Technology"}
    assert rows[1]["name"] == "Alphabet Inc. (Class C)"


def test_wikipedia_accepts_previous_component_column_order():
    rows = parse_nasdaq100("""<table><tr><th>Company</th><th>Ticker</th><th>GICS Sector</th></tr>
        <tr><td>Microsoft</td><td>MSFT</td><td>Information Technology</td></tr></table>""")
    assert rows[0]["ticker"] == "MSFT"
    assert rows[0]["industry"] == "Information Technology"


@pytest.mark.parametrize("content", [
    "Symbol,Security,GICS Sector\nAAPL,Apple,Technology\n",
    "Symbol,Security,GICS Sector,CIK\n",
    "Symbol,Security,GICS Sector,CIK\nAAPL,Apple,Technology,no-cik\n",
    "Symbol,Security,GICS Sector,CIK\nAAPL,Apple,Technology,\n",
    "Symbol,Security,GICS Sector,CIK\nAAPL,Apple,Technology,320193\nAAPL,Apple,Technology,320193\n",
])
def test_csv_rejects_broken_or_duplicate_membership(content):
    with pytest.raises(UserError) as error:
        parse_sp500(content)
    assert error.value.code == "index.parse_failed"


@pytest.mark.parametrize("content", [
    "<html>Rate limited</html>",
    "<table><tr><th>Ticker</th><th>Company</th><th>Industry</th></tr></table>",
    "<table><tr><th>Ticker</th><th>Company</th><th>Industry</th></tr>"
    "<tr><td>AAPL</td><td>Apple</td></tr></table>",
])
def test_wikipedia_rejects_empty_missing_or_truncated_component_tables(content):
    with pytest.raises(UserError) as error:
        parse_nasdaq100(content)
    assert error.value.code == "index.parse_failed"


def test_download_rejects_partial_membership_instead_of_publishing_it(monkeypatch):
    client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, text="Symbol,Security,GICS Sector,CIK\nAAPL,Apple,Technology,320193\n"))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(transport=transport, **kwargs))
    with pytest.raises(UserError) as error:
        fetch_index("sp500")
    assert error.value.code == "index.parse_failed"
    assert error.value.params["reason"] == "unexpected_count"


def test_download_failure_has_translatable_status_and_no_external_error_text(monkeypatch):
    client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text="Unavailable"))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(transport=transport, **kwargs))
    with pytest.raises(UserError) as error:
        fetch_index("nasdaq100")
    assert error.value.code == "index.download_failed"
    assert error.value.params == {"index": "nasdaq100", "reason": "http_503"}


def test_failed_weekly_update_preserves_list_and_last_success_date():
    at = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    rows = parse_sp500("Symbol,Security,GICS Sector,CIK\nAAPL,Apple,Technology,320193\n")
    with session() as s, s.begin():
        persist_success(s, "sp500", rows, at=at)
    failed_at = at + timedelta(days=7)
    with session() as s, s.begin():
        assert due_indices(s, at=failed_at) == ["sp500", "nasdaq100"]
        persist_failure(s, "sp500", UserError("index.download_failed", index="sp500", reason="http_503"), at=failed_at)
    with session() as s:
        state = s.get(IndexConstituentState, "sp500")
        assert state.updated_at == at
        assert state.attempted_at == failed_at
        assert state.error == {"code": "index.download_failed", "params": {
            "index": "sp500", "reason": "http_503"}}
        assert s.scalars(select(IndexConstituent.ticker)).all() == ["AAPL"]
        assert due_indices(s, at=failed_at + timedelta(hours=1)) == ["nasdaq100"]


def test_success_replaces_membership_and_maps_missing_wikipedia_cik_locally():
    at = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    first = parse_nasdaq100("""<table><tr><th>Ticker</th><th>Company</th><th>Industry</th></tr>
        <tr><td>MSFT</td><td>Microsoft</td><td>Technology</td></tr></table>""")
    replacement = parse_nasdaq100("""<table><tr><th>Ticker</th><th>Company</th><th>Industry</th></tr>
        <tr><td>AAPL</td><td>Apple</td><td>Technology</td></tr></table>""")
    with session() as s, s.begin():
        s.add(Issuer(id="0000320193", name="Apple Inc.", ticker="AAPL"))
        s.flush()
        persist_success(s, "nasdaq100", first, at=at)
        persist_failure(s, "nasdaq100", UserError("index.download_failed"), at=at)
    with session() as s, s.begin():
        persist_success(s, "nasdaq100", replacement, at=at + timedelta(days=7))
    with session() as s:
        members = s.scalars(select(IndexConstituent)).all()
        assert len(members) == 1
        assert (members[0].ticker, members[0].cik) == ("AAPL", "0000320193")
        assert s.get(IndexConstituentState, "nasdaq100").error is None
