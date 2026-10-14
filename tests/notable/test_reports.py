"""Reports stay intact under row filters, top-three previews and group pagination."""

from datetime import date

from iirp.db import session
from iirp.models.political import PoliticalTransaction
from iirp.notable.reports import reports
from sqlalchemy import select

from tests.jobs.test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_client,
    lifecycle_database,
)
from tests.notable.fixtures import politician, report, sec_trade
from tests.notable.test_trades import follow, no_requests, world  # noqa: F401


def read(**filters):
    with session() as s:
        return reports(s, **{"assets": "all", **filters})


def test_groups_paginate_without_splitting_a_report(world):  # noqa: F811
    all_groups = read(limit=25)["items"]
    assert len(all_groups) == 7
    seen, cursor = [], ""
    while True:
        page = read(limit=2, cursor=cursor)
        assert page["total"] == 7
        seen.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert [group["id"] for group in seen] == [group["id"] for group in all_groups]
    assert sum(len(group["trades"]) for group in seen) == 8
    assert all(group["source"] != "house" or len(group["trades"]) == 2 for group in seen[:2])
    no_requests()


def test_preview_ranks_reported_amounts_and_filters_rows_before_grouping(world, lifecycle_client):  # noqa: F811
    with session() as s, s.begin():
        person = politician(s)
        added = report(s, person, report_date=date(2026, 10, 10), rows=[
            ("AAPL", "Apple", "purchase", date(2026, 10, 1), low, high)
            for low, high in [("1001", "15000"), ("100001", "250000"), ("50001", "100000"),
                              ("15001", "50000"), (None, None)]
        ] + [(None, "City bond", "sale", date(2026, 10, 1), "1000001", "5000000")])
    group = read(limit=1)["items"][0]
    assert group["id"] == added.id
    amounts = {row["id"]: row["amount_low"] for row in group["trades"]}
    assert [amounts[id] for id in group["preview_ids"]] == ["100001.00", "50001.00", "15001.00"]
    assert (group["equities"], group["sides"], group["counts"]) == (5, {"buy": 5}, {"stock": 5, "bond": 1})
    matched = read(ticker="AAPL", direction="buy")["items"][0]
    assert len(matched["trades"]) == 5 and matched["counts"] == group["counts"]
    assert read(ticker="MISSING")["total"] == 0
    assert read(published_from=date(2026, 10, 10), published_to=date(2026, 10, 10))["total"] == 1
    response = lifecycle_client.get("/api/v1/notable/reports", params={"ticker": "AAPL", "direction": "buy"})
    assert response.status_code == 200 and len(response.json()["items"][0]["preview_ids"]) == 3
    no_requests()


def test_sec_rows_share_one_filing_and_keep_exact_amount_order():
    with session() as s, s.begin():
        follow(s, "sec:0001767470")
        first = sec_trade(s, shares="1")
        for amount in ("2", "10", "5"):
            sec_trade(s, accession=first.accession, shares=amount)
    result = read(person="sec:0001767470")
    assert result["total"] == 1
    group = result["items"][0]
    amounts = {row["id"]: row["amount"] for row in group["trades"]}
    assert [amounts[id] for id in group["preview_ids"]] == ["250.00", "125.00", "50.00"]


def test_invalid_cursor_and_dates(world, lifecycle_client):  # noqa: F811
    assert lifecycle_client.get("/api/v1/notable/reports", params={"cursor": "invalid"}).status_code == 422
    assert lifecycle_client.get("/api/v1/notable/reports", params={
        "published_from": "2026-10-10", "published_to": "2026-10-01"}).status_code == 422


def test_default_assets_hide_bonds_funds_and_unknown_types_but_keep_counts(world, lifecycle_client):  # noqa: F811
    with session() as s, s.begin():
        person = politician(s)
        added = report(s, person, report_date=date(2026, 10, 10), rows=[
            (ticker, name, "purchase", date(2026, 10, 1), "1001", "15000")
            for ticker, name in [("AAPL", "Apple"), (None, "No code stock"), ("MSFT", "Call option"),
                                 (None, "City bond"), ("FUND", "Fund"), ("UNKN", "Unclassified")]
        ])
        for row in s.scalars(select(PoliticalTransaction).where(PoliticalTransaction.report_id == added.id)):
            row.asset_type = {"MSFT": "option", "FUND": "fund", "UNKN": "unknown"}.get(row.ticker, row.asset_type)
    params = {"person": "house:Nancy Pelosi|CA11", "published_from": "2026-10-10"}
    default = lifecycle_client.get("/api/v1/notable/reports", params=params).json()["items"][0]
    all_assets = lifecycle_client.get("/api/v1/notable/reports", params={**params, "assets": "all"}).json()["items"][0]
    assert len(default["trades"]) == 3 and len(all_assets["trades"]) == 6
    assert default["counts"] == all_assets["counts"] == {"stock": 2, "option": 1, "bond": 1, "fund": 1, "other": 1}
    assert set(default["preview_ids"]) == {row["id"] for row in default["trades"]}
    # A report with bonds only disappears in the default view, without affecting the all-assets view.
    assert lifecycle_client.get("/api/v1/notable/reports", params={"source": "oge"}).json()["total"] == 0
    assert lifecycle_client.get("/api/v1/notable/reports", params={"source": "oge", "assets": "all"}).json()["total"] == 1
    no_requests()
