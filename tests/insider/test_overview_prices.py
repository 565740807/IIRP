"""Overview price checks, list sizes, ordering, issuer grouping and display numbers."""

from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from iirp.api.app import app
from iirp.db import session
from iirp.insider.feed import feed as read_feed
from iirp.insider.prices import (
    combined_check,
    display_ticker,
    feed_prices,
    money_text,
    overview_settings,
    percent_text,
    price_check,
    price_text,
    quote_symbol,
    shares_text,
)
from iirp.insider.views import _refresh_groups
from iirp.models import MarketQuote, PriceCache, PriceCacheBar, Security, now
from sqlalchemy import select

from tests.insider.test_overview import DAY, read, seed
from tests.jobs.test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401


def quote(s, symbol, value):
    """A saved closing quote, as the stock quote task stores it."""
    stamp = now()
    s.add(MarketQuote(symbol=symbol, fetched_at=stamp, data={
        "symbol": symbol, "value": value, "regular_value": value,
        "regular_time": stamp.isoformat(), "as_of": DAY.isoformat(),
        "fetched_at": stamp.isoformat(), "status": "CLOSED",
        "next_refresh_at": (stamp + timedelta(days=1)).isoformat()}))


def bar(s, symbol, close, day=DAY, issuer_id=None):
    security = s.scalar(select(Security).where(Security.symbol == symbol))
    if security is None:
        security = Security(symbol=symbol, issuer_id=issuer_id)
        s.add(security)
        s.flush()
    cache = PriceCache(security_id=security.id, start_date=day, end_date=day,
                       complete_through=day, expires_at=now() + timedelta(hours=1))
    s.add(cache)
    s.flush()
    s.add(PriceCacheBar(cache_id=cache.id, session_date=day, high=close, low=close,
                        close=close, status="VALID"))


def test_mismatch_ratio_comes_from_config_and_both_sides_of_the_threshold():
    limit = overview_settings()["price_mismatch_ratio"]
    assert limit == 10
    reference = (None, None, Decimal(10))
    assert price_check(Decimal("99.99"), reference)["status"] == "ok"
    assert price_check(Decimal(100), reference)["status"] == "mismatch"
    assert price_check(Decimal("1.0001"), reference)["status"] == "ok"
    assert price_check(Decimal(1), reference)["status"] == "mismatch"
    assert price_check(Decimal(1), reference)["ratio"] == 10
    assert price_check(Decimal(100), reference, limit=11)["status"] == "ok"


def test_reference_price_prefers_trade_day_close_then_quote_then_unchecked():
    day_close, current = (None, None, Decimal(100)), {"value": "5"}
    assert price_check(Decimal(100), day_close, current)["status"] == "ok"
    assert price_check(Decimal(100), None, current) == {"status": "mismatch", "ratio": 20}
    assert price_check(Decimal(100), (None, None, None), current)["status"] == "mismatch"
    assert price_check(Decimal(100), None, {"value": None})["status"] == "unchecked"
    assert price_check(Decimal(100))["status"] == "unchecked"
    assert price_check(None, day_close, current)["status"] == "not_applicable"


@pytest.mark.parametrize("price", [None, Decimal(0), Decimal("-1")])
def test_missing_or_zero_prices_are_not_checked(price):
    assert price_check(price, (None, None, Decimal(10)), {"value": "10"}) == {
        "status": "not_applicable", "ratio": None}
    assert price_check(price) == {"status": "not_applicable", "ratio": None}


def test_trader_line_ignores_prices_without_anything_to_check():
    na, ok = {"status": "not_applicable", "ratio": None}, {"status": "ok", "ratio": Decimal(1)}
    unchecked = {"status": "unchecked", "ratio": None}
    assert combined_check([na, na])["status"] == "not_applicable"
    assert combined_check([])["status"] == "not_applicable"
    assert combined_check([na, ok])["status"] == "ok"
    assert combined_check([na, unchecked])["status"] == "unchecked"


def test_zero_price_feed_rows_are_not_checked_and_the_ratio_is_reported():
    with session() as s, s.begin():
        seed(s, issuer="0000000101", ticker="AWD", code="A", price="0", data={"ticker": "AWD"})
        seed(s, issuer="0000000102", ticker="OPT", code="M", price=None, data={"ticker": "OPT"})
        _refresh_groups(s, {("0000000101", DAY), ("0000000102", DAY)})
    with session() as s, s.begin():
        revisions = [group["revision_id"] for group in read_feed(s)["groups"]]
        result = feed_prices(s, revisions)
    assert result["price_mismatch_ratio"] == "10"
    assert {item["price_check"] for item in result["items"].values()} == {"not_applicable"}


def test_overview_reports_the_configured_mismatch_ratio():
    assert read()["price_mismatch_ratio"] == "10"
    with TestClient(app) as client:
        assert client.get("/api/v1/insider/overview").json()["price_mismatch_ratio"] == "10"


def test_trade_day_bar_and_quote_reference_in_the_overview():
    with session() as s, s.begin():
        seed(s, issuer="0000000101", ticker="BAR", price="100")
        seed(s, issuer="0000000102", ticker="QTE", price="100")
        seed(s, issuer="0000000103", ticker="NONE1", price="100")
        bar(s, "BAR", 100)
        quote(s, "BAR", "5")
        quote(s, "QTE", "5")
    result = read()
    checks = {item["ticker"]: item["price"] for item in result["large_buys"]["items"]}
    assert checks["BAR"]["price_check"] == "ok"
    assert checks["NONE1"]["price_check"] == "unchecked"
    assert "QTE" not in checks
    assert result["large_buys"]["price_mismatch_rows"] == 1


def test_mismatched_prices_stay_visible_but_leave_rankings_and_totals():
    with session() as s, s.begin():
        bad = seed(s, issuer="0000000101", ticker="SLBT", price="2272653", shares="4545306",
                   owners=("0000000200",), roles={"is_officer": "1", "officer_title": "CEO"})
        seed(s, issuer="0000000101", ticker="SLBT", price="1.50", shares="100",
             owners=("0000000201",))
        seed(s, issuer="0000000102", ticker="GOOD", price="20", shares="10")
        quote(s, "SLBT", "1.52")
    # 150 of SLBT is counted; the mismatched 10.3 trillion is not.
    result = read()
    assert [item["ticker"] for item in result["large_buys"]["items"]] == ["GOOD", "SLBT"]
    assert result["large_buys"]["total"] == 2
    assert result["large_buys"]["price_mismatch_rows"] == 1
    assert bad.id not in {item["id"] for item in result["large_buys"]["items"]}
    executive = result["executive_buys"]["items"][0]
    assert executive["id"] == bad.id and executive["amount"] is None
    assert executive["price"]["price"] == "2272653.00"
    assert executive["price"]["price_check"] == "mismatch"
    assert executive["price"]["mismatch_ratio"] == "1495166"
    companies = result["companies"]
    assert [item["ticker"] for item in companies["items"]] == ["GOOD", "SLBT"]
    assert companies["price_mismatch_rows"] == 1
    slbt = companies["items"][1]
    assert slbt["buy_amount"] == "150.00" and slbt["buy_people"] == 2
    assert slbt["price_mismatch_rows"] == 1
    assert slbt["buy_price"]["price"] == "1.50"
    cluster = result["cluster_buys"]
    assert cluster["total"] == 1 and cluster["price_mismatch_rows"] == 1
    assert cluster["items"][0]["buy_people"] == 2
    assert cluster["items"][0]["buy_amount"] == "150.00"


def test_side_with_only_mismatched_prices_has_no_amount_and_sorts_last():
    with session() as s, s.begin():
        seed(s, issuer="0000000101", ticker="IHT", code="S", price="22593.6", shares="16000")
        seed(s, issuer="0000000102", ticker="LOSS", code="S", price="10", shares="10")
        quote(s, "IHT", "1.15")
    result = read()
    assert result["large_sales"]["items"][0]["ticker"] == "LOSS"
    assert result["large_sales"]["total"] == 1
    iht = result["companies"]["items"][-1]
    assert iht["ticker"] == "IHT" and iht["sell_amount"] is None and iht["net_amount"] is None
    assert read(company_sort="net_sell")["companies"]["items"][0]["ticker"] == "LOSS"
    assert read(min_amount=Decimal(1))["companies"]["total"] == 1


def test_lists_return_configured_rows_and_full_counts():
    settings = overview_settings()
    with session() as s, s.begin():
        for number in range(1, 61):
            roles = {"is_officer": "1", "officer_title": "CFO"} if number <= 25 else None
            seed(s, issuer=f"{number:010d}", ticker=f"T{number}", price=str(number),
                 roles=roles, data={"shares_after": "150"})
    result = read()
    assert (result["large_buys"]["total"], len(result["large_buys"]["items"])) == (
        60, settings["large_rows"])
    assert (result["executive_buys"]["total"], len(result["executive_buys"]["items"])) == (
        25, settings["executive_rows"])
    assert (result["holding_increases"]["total"],
            len(result["holding_increases"]["items"])) == (60, settings["holding_rows"])
    assert (result["companies"]["total"], len(result["companies"]["items"])) == (
        60, settings["company_page_rows"])
    assert len(read(company_limit=55)["companies"]["items"]) == 55
    assert result["company_page_rows"] == settings["company_page_rows"]
    with TestClient(app) as client:
        response = client.get("/api/v1/insider/overview", params={"company_limit": 55})
        assert response.status_code == 200
        assert client.get("/api/v1/insider/overview",
                          params={"company_limit": 0}).status_code == 422
        assert client.get("/api/v1/insider/overview",
                          params={"company_sort": "score"}).status_code == 422


def test_company_sorts():
    with session() as s, s.begin():
        seed(s, issuer="0000000101", ticker="BIG", price="100", shares="100")
        seed(s, issuer="0000000102", ticker="SELL", code="S", price="100", shares="50")
        seed(s, issuer="0000000103", ticker="MIX", price="10", shares="10", owners=("0000000200",))
        seed(s, issuer="0000000103", ticker="MIX", code="S", price="100", shares="100",
             owners=("0000000201",))
        seed(s, issuer="0000000103", ticker="MIX", code="S", price="1", shares="1",
             owners=("0000000202",))

    def order(sort):
        return [item["ticker"] for item in read(company_sort=sort)["companies"]["items"]]

    assert order("net_buy") == ["BIG", "SELL", "MIX"]
    assert order("net_sell") == ["MIX", "SELL", "BIG"]
    assert order("buy") == ["BIG", "MIX", "SELL"]
    assert order("sell") == ["MIX", "SELL", "BIG"]
    assert order("people") == ["MIX", "BIG", "SELL"]
    assert read(company_sort="people")["companies"]["items"][0]["people"] == 3


def test_clusters_sort_by_window_end_then_people_then_amount():
    with session() as s, s.begin():
        def cluster(issuer, ticker, end, people, price):
            for number in range(people):
                seed(s, issuer=issuer, ticker=ticker, day=end, price=price,
                     owners=(f"{int(issuer) + 1000 + number:010d}",))
        cluster("0000000101", "OLD", DAY - timedelta(days=3), 3, "500")
        cluster("0000000102", "TWO", DAY, 2, "50")
        cluster("0000000103", "THREE", DAY, 3, "10")
        cluster("0000000104", "RICH", DAY, 2, "90")
    result = read()
    assert [item["ticker"] for item in result["cluster_buys"]["items"]] == [
        "THREE", "RICH", "TWO", "OLD"]


def test_issuer_groups_once_with_its_first_usable_ticker():
    with session() as s, s.begin():
        seed(s, issuer="0000920760", ticker="LEN, LEN.B", data={"ticker": "LEN"},
             owners=("0000000200",))
        seed(s, issuer="0000920760", ticker="LEN, LEN.B", data={"ticker": "LEN, LEN.B"},
             owners=("0000000201",))
        quote(s, "LEN", "80")
    result = read()
    assert result["companies"]["total"] == 1
    company = result["companies"]["items"][0]
    assert company["ticker"] == "LEN" and company["buy_people"] == 2
    assert company["buy_price"]["current_price"] == "80.00"
    assert result["cluster_buys"]["total"] == 1
    assert {item["ticker"] for item in result["large_buys"]["items"]} == {"LEN"}
    assert "LEN" in result["quote_symbols"]


@pytest.mark.parametrize("texts,ticker,symbol", [
    (("LEN, LEN.B", "LEN"), "LEN", "LEN"),
    ((None, "LEN, LEN.B"), "LEN", "LEN"),
    (("BRK.B", None), "BRK.B", "BRK-B"),
    (("NONE", "ABC"), "ABC", "ABC"),
    (("TSBK", "RVSB"), "TSBK", "TSBK"),
    ((None, "INVALID/SYMBOL"), "INVALID/SYMBOL", None),
    (("NONE", "N/A"), None, None),
    (("UNKNOWN", "XYZ"), "XYZ", "XYZ"),
    ((None, None), None, None),
])
def test_display_ticker_prefers_issuer_then_first_valid_filing_symbol(texts, ticker, symbol):
    assert display_ticker(*texts) == ticker
    assert quote_symbol(ticker) == symbol


def test_feed_quotes_use_the_issuer_symbol_and_flag_mismatched_prices():
    with session() as s, s.begin():
        seed(s, issuer="0000920760", ticker="LEN, LEN.B", data={"ticker": "LEN, LEN.B"},
             price="80")
        seed(s, issuer="0000000101", ticker="UUU", data={"ticker": "UUU"}, price="5134")
        quote(s, "LEN", "80")
        quote(s, "UUU", "5.22")
        _refresh_groups(s, {("0000920760", DAY), ("0000000101", DAY)})
    with session() as s, s.begin():
        groups = {group["issuer_id"]: group for group in read_feed(s)["groups"]}
        assert groups["0000920760"]["quote_symbol"] == "LEN"
        assert groups["0000000101"]["quote_symbol"] == "UUU"
        items = feed_prices(s, [group["revision_id"] for group in groups.values()])["items"]
    by_issuer = {key.split(":")[0]: value for key, value in items.items()}
    len_price = by_issuer[groups["0000920760"]["revision_id"]]
    uuu_price = by_issuer[groups["0000000101"]["revision_id"]]
    assert len_price["current_price"] == "80.00" and len_price["price_check"] == "ok"
    assert uuu_price["price_check"] == "mismatch" and uuu_price["mismatch_ratio"] == "984"
    # The feed keeps its own amount rule; only the flag is added.
    assert uuu_price["amount"] == "513400.00"


def test_unchecked_ranking_candidates_ask_for_quotes_until_they_are_saved():
    with session() as s, s.begin():
        seed(s, issuer="0000000101", ticker="SLBT", price="2272653", shares="10")
        seed(s, issuer="0000000102", ticker="GOOD", price="20", shares="10")
        quote(s, "GOOD", "20")
    first = read()
    assert first["large_buys"]["items"][0]["ticker"] == "SLBT"
    assert first["large_buys"]["items"][0]["price"]["price_check"] == "unchecked"
    assert set(first["quote_symbols"]) == {"SLBT", "GOOD"}
    assert first["quotes_pending"] == 1
    with session() as s, s.begin():
        quote(s, "SLBT", "1.52")
    second = read()
    assert [item["ticker"] for item in second["large_buys"]["items"]] == ["GOOD"]
    assert second["quotes_pending"] == 0


def test_quote_symbols_are_capped():
    with session() as s, s.begin():
        for number in range(1, 251):
            seed(s, issuer=f"{number:010d}", ticker=f"Q{number}", price="10")
    result = read(company_limit=250)
    assert len(result["quote_symbols"]) == overview_settings()["max_quote_symbols"]


@pytest.mark.parametrize("function,value,text", [
    (price_text, Decimal("2272653.000000000000"), "2272653.00"),
    (price_text, Decimal("1"), "1.00"),
    (price_text, Decimal("0.99995"), "1.0000"),
    (price_text, Decimal("0.123456"), "0.1235"),
    (price_text, None, None),
    (money_text, Decimal("10329903316818.00000000000000"), "10329903316818.00"),
    (money_text, Decimal("-800.005"), "-800.01"),
    (percent_text, Decimal("-99.99993311781429017100278837"), "-100.00"),
    (percent_text, Decimal("1.380348584431512462920509880"), "1.38"),
    (shares_text, Decimal("160275.000000000000"), "160275"),
    (shares_text, Decimal("25.500"), "25.5"),
    (shares_text, Decimal("0"), "0"),
])
def test_display_numbers_are_rounded_in_the_backend(function, value, text):
    assert function(value) == text


def test_overview_numbers_in_the_response_are_rounded():
    with session() as s, s.begin():
        seed(s, price="12.3456789", shares="100.000", data={"shares_after": "300"})
        quote(s, "TEST", "13")
    item = read()["holding_increases"]["items"][0]
    assert item["price"]["price"] == "12.35"
    assert item["amount"] == "1234.57"
    assert item["shares"] == "100"
    assert item["holding_change"] == "50.00"
    assert item["price"]["change_percent"] == "5.30"
