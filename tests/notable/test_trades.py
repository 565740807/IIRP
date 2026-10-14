"""Followed people's trades: three sources in one order, filters, pages, scanned and doubtful
rows, the company page block, the code each row's price windows use, the person summary
built on the Insider windows, and no price fetched by any of it.

Uses a disposable iirp_v1_test_* database.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from iirp.analysis.calendar import last_completed_session, sessions
from iirp.analysis.insider_windows import trade_windows
from iirp.db import session
from iirp.models import (
    Batch,
    Job,
    NotablePerson,
    PriceCache,
    PriceCacheBar,
    Security,
    SourceObservation,
    now,
)
from iirp.models.political import PoliticalTransaction
from iirp.notable import people
from iirp.notable.trades import (
    oge_role,
    person_summary,
    published_basis,
    review_reasons,
    sec_roles,
    trades,
)
from sqlalchemy import func, select

from tests.jobs.test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_client,
    lifecycle_database,
)
from tests.notable.fixtures import AT, politician, report, sec_trade

D = date(2026, 10, 1)


def follow(s, *keys):
    for key in keys:
        s.add(NotablePerson(key=key, source=key.split(":")[0], name=key, added_at=AT - timedelta(days=60)))
    s.flush()


@pytest.fixture
def world():
    """Ryan Cohen (followed, SEC), Nancy Pelosi (followed, House), Donald J Trump (followed, OGE),
    an unfollowed House member and an unfollowed SEC filer."""
    with session() as s, s.begin():
        follow(s, "sec:0001767470", "house:Nancy Pelosi|CA11", "oge:Donald J Trump|president")
        # Accepted 2026-10-08 18:00 UTC: published 10-08 (US Eastern).
        sec_trade(s, code="P", day=date(2026, 10, 6))
        sec_trade(s, code="S", day=date(2026, 10, 2), plan=True, accepted=AT - timedelta(days=2), shares="10")
        # Published the evening of 10-07 Eastern although 10-08 in UTC.
        sec_trade(s, code="P", day=date(2026, 10, 7), accepted=datetime(2026, 10, 8, 2, tzinfo=timezone.utc))
        # Not P/S: a grant is not a trade on this page.
        sec_trade(s, code="A", day=date(2026, 10, 6))
        sec_trade(s, owner="0000000099", owner_name="Other Person", code="P")
        pelosi = politician(s)
        report(s, pelosi, report_date=date(2026, 10, 8), rows=[
            ("NVDA", "NVIDIA Corporation - Common Stock", "purchase", date(2026, 9, 20), "1000001", "5000000"),
            ("AAPL", "Apple Inc. - Common Stock", "sale", date(2026, 9, 18), "500001", "1000000", ["ocr"]),
        ])
        report(s, pelosi, report_date=date(2026, 9, 1), rows=[
            ("GME", "GameStop Corp", "unknown", date(2026, 8, 1), "15001", "50000",
             ["ocr", "unknown_transaction_type"], "inferred"),
        ])
        # A superseded report counts through its amendment only.
        old = report(s, pelosi, report_date=date(2026, 9, 2), status="superseded",
                     rows=[("MSFT", "Microsoft", "purchase", date(2026, 8, 2), "1001", "15000")])
        report(s, pelosi, report_date=date(2026, 9, 3), amendment_status="linked", amendment_of=old.id,
               rows=[("MSFT", "Microsoft", "purchase", date(2026, 8, 2), "15001", "50000")])
        trump = politician(s, source="oge", name="Donald J Trump", district=None, position="president")
        report(s, trump, report_date=date(2026, 10, 8), rows=[
            (None, "BRISTOL CNTY MASS 5% DUE 2030 bond", "purchase", date(2026, 8, 15), "250001", "500000",
             ["ocr", "unknown_owner"]),
        ])
        other = politician(s, name="Jane Roe", district="TX01")
        report(s, other, report_date=date(2026, 10, 9), rows=[
            ("GME", "GameStop Corp", "sale", date(2026, 10, 1), "1001", "15000"),
        ])


def read(**kwargs):
    with session() as s:
        return trades(s, **kwargs)


def summary(result):
    return [(item["source"], str(item["published"]), item["ticker"], item["side"]) for item in result["items"]]


def test_three_sources_in_one_publication_order(world):
    result = read()
    assert summary(result) == [
        ("sec", "2026-10-08", "GME", "buy"),
        ("house", "2026-10-08", "NVDA", "buy"),
        ("house", "2026-10-08", "AAPL", "sell"),
        ("oge", "2026-10-08", None, "buy"),
        ("sec", "2026-10-07", "GME", "buy"),
        ("sec", "2026-10-06", "GME", "sell"),
        ("house", "2026-09-03", "MSFT", "buy"),
        ("house", "2026-09-01", "GME", "unknown"),
    ]
    assert result["total"] == 8
    first = result["items"][0]
    assert (first["amount"], first["shares"], first["price"], first["lag_days"]) == ("25000.00", "1000", "25.00", 2)
    assert first["person"]["roles"] == ["ceo", "director"] and first["person"]["watched"] is True
    assert first["url"].startswith("https://www.sec.gov/Archives/")
    sale = result["items"][5]
    assert (sale["plan"], sale["amount"]) == (True, "250.00")
    nvda = result["items"][1]
    assert (nvda["amount_low"], nvda["amount_high"], nvda["lag_days"]) == ("1000001.00", "5000000.00", 18)
    assert nvda["person"]["roles"] == ["house"] and nvda["person"]["district"] == "CA11"
    bond = result["items"][3]
    assert (bond["asset_kind"], bond["person"]["roles"], bond["person"]["name"]) == ("bond", ["president"], "Donald J. Trump")
    msft = result["items"][6]
    assert (msft["amendment"], msft["amount_low"]) == (True, "15001.00")


def test_all_scope_adds_unfollowed_politicians_but_not_unfollowed_filers(world):
    result = read(scope="all")
    assert ("house", "2026-10-09", "GME", "sell") in summary(result)
    assert result["total"] == 9
    roe = next(item for item in result["items"] if item["person"]["key"] == "house:Jane Roe|TX01")
    assert roe["person"]["watched"] is False


@pytest.mark.parametrize("filters,expected", [
    ({"source": "house"}, 4),
    ({"source": "sec"}, 3),
    ({"direction": "buy"}, 5),
    ({"direction": "sell"}, 2),
    ({"ticker": "gme"}, 4),
    ({"person": "house:Nancy Pelosi|CA11"}, 4),
    ({"person": "oge:Donald J Trump|president", "direction": "buy"}, 1),
    ({"published_from": date(2026, 10, 7), "published_to": date(2026, 10, 7)}, 1),
    ({"published_from": date(2026, 10, 8)}, 4),
])
def test_filters(world, filters, expected):
    assert read(**filters)["total"] == expected
    assert len(read(**filters)["items"]) == expected


def test_pages_neither_repeat_nor_skip(world):
    seen, cursor = [], ""
    while True:
        page = read(limit=3, cursor=cursor)
        assert page["total"] == 8
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == [item["id"] for item in read(limit=100)["items"]]
    assert len(set(seen)) == 8


def test_bad_requests(world, lifecycle_client):  # noqa: F811
    client = lifecycle_client
    assert client.get("/api/v1/notable/trades", params={"cursor": "nonsense"}).json()["detail"]["code"] == "notable.cursor_invalid"
    assert client.get("/api/v1/notable/trades", params={
        "published_from": "2026-10-08", "published_to": "2026-10-01"}).json()["detail"]["code"] == "notable.date_range_invalid"
    assert client.get("/api/v1/notable/trades", params={"person": "nobody"}).status_code == 422


def test_scanned_rows_and_rows_with_stated_doubts(world):
    items = {item["ticker"]: item for item in read(source="house")["items"]}
    # A clean scanned row: marked as scanned, no doubt.
    assert (items["AAPL"]["ocr"], items["AAPL"]["review"]) == (True, [])
    # Electronic rows are neither.
    assert (items["NVDA"]["ocr"], items["NVDA"]["review"]) == (False, [])
    # The doubts are named: an unknown direction and a code inferred from the name.
    assert items["GME"]["ocr"] is True
    assert items["GME"]["review"] == ["direction_unknown", "ticker_inferred"]


def test_doubts_derived_from_dates_and_ranges():
    class Row:
        warnings = ["ocr", "unknown_owner", "missing_amount_low", "text_encoding"]
        ticker_origin = "local_exact"
        asset_name = "Apple Inc"
        transaction_date = date(2026, 10, 9)
        amount_low = amount_high = None

    assert review_reasons(Row, date(2026, 10, 8)) == ["amount_missing", "text_garbled", "date_after_report"]
    Row.warnings, Row.transaction_date = [], date(2024, 1, 2)
    from decimal import Decimal
    Row.amount_low, Row.amount_high = Decimal("1001"), Decimal("16000")
    assert review_reasons(Row, date(2026, 10, 8)) == ["date_unlikely", "amount_unusual"]
    Row.amount_low, Row.amount_high = Decimal("50000001"), None
    Row.transaction_date = date(2026, 9, 1)
    assert review_reasons(Row, date(2026, 10, 8)) == []
    # Text recognition can leave a name with almost no letters.
    Row.asset_name = "& % 1,"
    assert review_reasons(Row, date(2026, 10, 8)) == ["asset_unreadable"]


def test_role_codes():
    assert sec_roles({"is_officer": "1", "officer_title": "President, CEO and Chairman", "is_director": "1"}) == (
        ["ceo", "director"], "President, CEO and Chairman")
    assert sec_roles({"is_officer": "true", "officer_title": "EVP & Chief Financial Officer"})[0] == ["cfo"]
    assert sec_roles({"is_officer": "1", "officer_title": "General Counsel"})[0] == ["officer"]
    assert sec_roles({"is_ten_percent_owner": "1"})[0] == ["ten_percent"]
    assert oge_role("president") == "president"
    assert oge_role("Secretary (Department of Commerce)") == "secretary.commerce"
    assert oge_role("Secretary (Department of Defense/Office of The Secretary)") == "secretary.defense"
    assert oge_role("Attorney General (Department of Justice)") == "attorney_general"
    assert oge_role("Administrator (Environmental Protection Agency)") == "administrator.epa"
    assert oge_role("Chief of Staff") == "official"


def test_company_block_reads_ninety_days_of_one_company(world, lifecycle_client):  # noqa: F811
    response = lifecycle_client.get("/api/v1/notable/trades", params={
        "issuer_id": "0001326380", "published_from": "2026-07-10", "limit": 5}).json()
    # Ryan Cohen's three GameStop trades and Pelosi's GME row; Jane Roe is not followed.
    assert [(item["source"], item["ticker"]) for item in response["items"]] == [
        ("sec", "GME"), ("sec", "GME"), ("sec", "GME"), ("house", "GME")]
    assert response["total"] == 4
    assert lifecycle_client.get("/api/v1/notable/trades", params={
        "ticker": "GME", "scope": "all", "limit": 2}).json()["total"] == 5


def prices(symbol, *, issuer_id=None, first=date(2026, 6, 1)):
    """A 24-hour price cache whose close rises by 1 each session (100, 101, ...), through the
    last completed session."""
    completed = last_completed_session()
    days = sessions(first, completed, "XNYS")
    with session() as s, s.begin():
        security = Security(symbol=symbol, name=symbol, instrument="EQUITY", currency="USD", status="VERIFIED",
                            calendar="XNYS", issuer_id=issuer_id)
        s.add(security)
        s.flush()
        stamp = now()
        cache = PriceCache(security_id=security.id, start_date=first, end_date=completed, complete_through=completed,
                           provider="synthetic", details={}, fetched_at=stamp, expires_at=stamp + timedelta(hours=24))
        s.add(cache)
        s.flush()
        for number, day in enumerate(days):
            close = Decimal(100 + number)
            s.add(PriceCacheBar(cache_id=cache.id, session_date=day, open=close, high=close, low=close, close=close,
                                status="VALID"))
    return days


def no_requests():
    with session() as s:
        for model in (Job, Batch, SourceObservation):
            assert s.scalar(select(func.count()).select_from(model)) == 0


def test_rows_name_the_code_their_windows_use(world):
    with session() as s, s.begin():
        trump = politician(s, source="oge", name="Donald J Trump", district=None, position="president")
        item = report(s, trump, report_date=date(2026, 10, 2), rows=[
            ("META", "Meta Platforms (META) [OP]", "purchase", date(2026, 9, 10), "1001", "15000"),
            ("GDX", "VanEck Gold Miners ETF (GDX)", "purchase", date(2026, 9, 10), "1001", "15000"),
            (None, "Private Partners LP", "purchase", date(2026, 9, 10), "1001", "15000"),
        ])
        kinds = {"META": "option", "GDX": "fund"}
        for row in s.scalars(select(PoliticalTransaction).where(PoliticalTransaction.report_id == item.id)):
            row.asset_type = kinds.get(row.ticker, "stock")
    items = {(item["ticker"], item["asset_name"]): item for item in read(limit=100)["items"]}
    codes = {key: (item["price_ticker"], item["price_none"]) for key, item in items.items()}
    # The SEC stock and a House stock use their own codes; an option uses the underlying stock.
    assert codes[("GME", "Issuer GME")] == ("GME", None)
    assert codes[("NVDA", "NVIDIA Corporation - Common Stock")] == ("NVDA", None)
    assert codes[("META", "Meta Platforms (META) [OP]")] == ("META", None)
    # Bonds, funds and rows without a code have no windows ("—").
    assert codes[(None, "BRISTOL CNTY MASS 5% DUE 2030 bond")] == (None, "bond")
    assert codes[("GDX", "VanEck Gold Miners ETF (GDX)")] == (None, "fund")
    assert codes[(None, "Private Partners LP")] == (None, "no_ticker")


def test_summary_uses_the_insider_windows_after_the_trade_and_after_publication(world, lifecycle_client):  # noqa: F811
    days = prices("GME", issuer_id="0001326380")
    no_requests()
    summary = lifecycle_client.get("/api/v1/notable/summary", params={"key": "sec:0001767470", "n": 1}).json()
    # Two buys (traded 10-06 and 10-07) and one plan sale. Publication windows start from the last
    # close before the market could react: accepted 10-08 14:00 ET (during the session) and 10-07
    # 22:00 ET (after the close) both count from the 10-07 close; the sale accepted 10-06 14:00 ET
    # from the 10-05 close.
    buy, sell = summary["sides"]["buy"], summary["sides"]["sell"]
    assert (summary["n"], summary["rows"], summary["no_code"], buy["trades"], sell["trades"]) == (1, 3, 0, 2, 1)
    # The same function as the Insider pages: C(t+n)/C(t) - 1 from the trade date and from the publication date.
    with session() as s:
        expected = trade_windows(s, [
            {"ticker": "GME", "date": "2026-10-06", "issuer_id": "0001326380"},
            {"ticker": "GME", "date": "2026-10-07", "issuer_id": "0001326380"},
            {"ticker": "GME", "date": "2026-10-05", "issuer_id": "0001326380"},
            {"ticker": "GME", "date": "2026-10-02", "issuer_id": "0001326380"}], 1)["items"]
    after = [item["after"]["value"] for item in expected]
    assert sorted(buy["after"]["values"]) == sorted([after[0], after[1]])
    assert buy["published"]["values"] == [after[1], after[1]]
    assert (sell["after"]["values"], sell["published"]["values"]) == ([after[3]], [after[2]])
    # With closes rising by one per session: 10-06 (session k) to 10-07 is 1/(100+k).
    k = days.index(date(2026, 10, 6))
    assert abs(Decimal(after[0]) - Decimal(1) / Decimal(100 + k)) < Decimal("1e-20")
    # Fewer than five values: listed, no median; all rose.
    assert (buy["after"]["count"], buy["after"]["up"], buy["after"]["median"]) == (2, 2, None)
    no_requests()


def test_summary_median_from_five_values_and_trades_without_prices(world):
    with session() as s, s.begin():
        pelosi = politician(s)
        report(s, pelosi, report_date=date(2026, 9, 15), rows=[
            ("AAPL", "Apple Inc. - Common Stock", "purchase", date(2026, 8, day), "1001", "15000")
            for day in (3, 4, 5, 6, 7)
        ])
    prices("AAPL")
    with session() as s:
        result = person_summary(s, "house:Nancy Pelosi|CA11", 2)
        windows = trade_windows(s, [{"ticker": "AAPL", "date": f"2026-08-0{day}"} for day in (3, 4, 5, 6, 7)], 2)
    buy = result["sides"]["buy"]
    values = sorted(Decimal(item["after"]["value"]) for item in windows["items"])
    # Five AAPL buys have values; NVDA (no prices) is counted as missing, not fetched.
    assert (buy["after"]["count"], buy["after"]["up"], buy["after"]["values"]) == (5, 5, [])
    assert Decimal(buy["after"]["median"]) == values[2]
    assert buy["after"]["missing"] == 2 and buy["trades"] == 7
    # The MSFT amendment counts once; the unknown-direction GME row is neither a buy nor a sale.
    assert result["sides"]["sell"]["trades"] == 1
    no_requests()


def test_publication_basis_is_the_last_close_before_the_market_could_react():
    eastern = timezone(timedelta(hours=-4))
    # After the close: that close. Before the open or during the session: the close before.
    assert published_basis(datetime(2026, 10, 7, 22, tzinfo=eastern)) == date(2026, 10, 7)
    assert published_basis(datetime(2026, 10, 7, 8, tzinfo=eastern)) == date(2026, 10, 6)
    assert published_basis(datetime(2026, 10, 7, 11, tzinfo=eastern)) == date(2026, 10, 6)
    # A report known only by its date: the close before that day; a weekend day counts from Friday.
    assert published_basis(published=date(2026, 10, 8)) == date(2026, 10, 7)
    assert published_basis(published=date(2026, 10, 11)) == date(2026, 10, 9)
    assert published_basis() is None


def test_political_trade_detail(world, lifecycle_client):  # noqa: F811
    with session() as s:
        row = s.scalar(select(PoliticalTransaction).where(PoliticalTransaction.ticker == "NVDA"))
    detail = lifecycle_client.get(f"/api/v1/notable/trades/{row.id}").json()
    assert detail["raw_row"] == "NVIDIA Corporation - Common Stock" and (detail["page"], detail["row_number"]) == (1, 1)
    assert detail["trade"]["person"]["key"] == "house:Nancy Pelosi|CA11"
    assert detail["trade"]["price_ticker"] == "NVDA" and str(detail["trade"]["published"]) == "2026-10-08"
    assert detail["trade"]["published_basis"] == "2026-10-07"
    assert detail["report"]["url"].startswith("https://") and detail["report"]["superseded"] is False
    missing = lifecycle_client.get("/api/v1/notable/trades/nonexistent")
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "notable.trade_not_found"


def test_person_page(world, lifecycle_client):  # noqa: F811
    pelosi = lifecycle_client.get("/api/v1/notable/person", params={"key": "house:Nancy Pelosi|CA11"}).json()
    assert (pelosi["name"], pelosi["category"], pelosi["roles"], pelosi["watched"]) == (
        "Nancy Pelosi", "house", ["house"], True)
    assert len(pelosi["reports"]) == 4 and pelosi["reports"][0]["url"].startswith("https://")
    assert [item["superseded"] for item in pelosi["reports"]].count(True) == 1
    cohen = lifecycle_client.get("/api/v1/notable/person", params={"key": "sec:0001767470"}).json()
    assert cohen["filings"]["count"] == 4 and "CIK=0001767470" in cohen["filings"]["edgar_url"]
    assert cohen["roles"] == ["ceo", "director"]
    missing = lifecycle_client.get("/api/v1/notable/person", params={"key": "house:Nobody|ZZ01"})
    assert missing.status_code == 404
    assert people.parse_key("oge:Donald J Trump|president")["position"] == "president"
