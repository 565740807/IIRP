"""Disclosure queries use synthetic rows and an isolated database, never sources."""
from datetime import date
from decimal import Decimal

import httpx
from iirp.db import session
from iirp.messages import msg
from iirp.models import Job, SourcePoll, now
from iirp.models.political import PoliticalPerson, PoliticalReport, PoliticalTransaction
from iirp.storage.objects import register_object, save_object
from sqlalchemy import func, select

from tests.jobs.test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_client,
    lifecycle_database,
)


def seed():
    with session() as s, s.begin():
        s.add_all([
            PoliticalPerson(id="person-house", source="house", identity="Test CA01", name="Synthetic Person", district="CA01"),
            PoliticalPerson(id="person-oge", source="oge", identity="Executive", name="Synthetic Executive", position="President"),
        ])
        s.flush()
        for identifier, filed, status, amendment, person in (
            ("old", date(2025, 2, 1), "superseded", "original", "person-house"),
            ("new", date(2025, 2, 2), "parsed", "linked", "person-house"),
            ("unlinked", date(2025, 2, 3), "parsed", "unresolved", "person-house"),
            ("executive", date(2025, 2, 2), "parsed", "original", "person-oge"),
            ("undated", None, "discovered", "original", "person-house"),
        ):
            s.add(PoliticalReport(id=identifier, source="house" if person == "person-house" else "oge",
                                  external_id=identifier, person_id=person, filed_date=filed,
                                  url=f"https://example.gov/{identifier}.pdf", status=status,
                                  amendment_status=amendment, amendment_of_id="old" if identifier == "new" else None,
                                  page_count=2, pages_done=2 if status != "discovered" else 0,
                                  ocr_pages=1, ocr_seconds=3))
        s.flush()
        for report, row, ticker, kind, day, confidence in (
            ("old", 1, "AAPL", "purchase", date(2025, 1, 23), "parsed"),
            ("new", 1, "AAPL", "purchase", date(2025, 1, 24), "parsed"),
            ("new", 2, "MSFT", "partial_sale", date(2025, 1, 25), "review"),
            ("unlinked", 1, "AAPL", "purchase", date(2025, 1, 23), "review"),
            ("executive", 1, None, "sale", date(2025, 1, 26), "parsed"),
            ("undated", 1, "NVDA", "exchange", None, "review"),
        ):
            s.add(PoliticalTransaction(id=f"{report}-{row}", report_id=report, page=1, row_number=row,
                                      owner="spouse", asset_name="Synthetic asset", ticker=ticker,
                                      asset_type="bond" if ticker is None else "stock",
                                      transaction_type=kind, transaction_date=day,
                                      amount_low=Decimal("1001"), amount_high=Decimal("15000"),
                                      raw_row="Synthetic raw row", confidence=confidence))


def ids(response):
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def test_filters_report_order_and_pagination_exclude_duplicate_amendments(lifecycle_client):  # noqa: F811
    seed()
    client = lifecycle_client
    base = "/api/v1/political/transactions"
    first = client.get(base, params={"limit": 2})
    assert ids(first) == ["new-2", "new-1"]
    assert first.json()["total"] == 4
    second = client.get(base, params={"limit": 2, "cursor": first.json()["next_cursor"]})
    assert ids(second) == ["executive-1", "undated-1"]
    assert second.json()["next_cursor"] is None
    assert ids(client.get(base, params={"person_id": "person-house", "direction": "buy", "ticker": "aapl"})) == ["new-1"]
    assert ids(client.get(base, params={"person": "Person", "source": "house", "direction": "sell"})) == ["new-2"]
    assert ids(client.get(base, params={"transaction_from": "2025-01-25", "transaction_to": "2025-01-25"})) == ["new-2"]
    assert ids(client.get(base, params={"report_from": "2025-02-02", "report_to": "2025-02-02"})) == ["new-2", "new-1", "executive-1"]
    assert ids(client.get(base, params={"direction": "exchange"})) == ["undated-1"]
    assert ids(client.get(base, params={"person": "%"})) == []
    assert client.get(base, params={"cursor": "invalid!"}).json()["detail"]["code"] == "political.cursor_invalid"
    assert client.get(base, params={"transaction_from": "2025-02-01", "transaction_to": "2025-01-01"}).json()["detail"]["code"] == "political.date_range_invalid"
    assert client.get(base, params={"limit": 101}).status_code == 422


def test_people_report_details_review_rows_and_original_file(lifecycle_client):  # noqa: F811
    seed()
    client = lifecycle_client
    people = client.get("/api/v1/political/people", params={"source": "house"}).json()["items"]
    assert len(people) == 1
    assert (people[0]["report_count"], people[0]["latest_report_date"]) == (2, "2025-02-02")
    detail = client.get("/api/v1/political/reports/new")
    assert detail.status_code == 200
    body = detail.json()
    assert body["amendment_of_id"] == "old" and body["amendment_status"] == "linked"
    assert [row["id"] for row in body["review_rows"]] == ["new-2"]
    assert body["rows"][0]["owner"] == "spouse"
    assert body["rows"][0]["amount_low"] == "1001.00"
    # Superseded and unresolved originals remain inspectable in the archive.
    assert client.get("/api/v1/political/reports/old").json()["rows"][0]["id"] == "old-1"
    assert client.get("/api/v1/political/reports/unlinked").status_code == 200
    assert client.get("/api/v1/political/reports/missing").json()["detail"]["code"] == "political.report_not_found"
    assert client.get("/api/v1/political/reports/new/file").status_code == 404
    content = b"%PDF-1.4 synthetic government test source"
    with session() as s, s.begin():
        source = save_object(content, "application/pdf")
        register_object(s, source)
        s.get(PoliticalReport, "new").source_hash = source["sha256"]
    assert client.get("/api/v1/political/reports/new").json()["file_url"].endswith("/new/file")
    downloaded = client.get("/api/v1/political/reports/new/file")
    assert downloaded.status_code == 200 and downloaded.content == content
    assert downloaded.headers["content-type"] == "application/pdf"


def test_source_availability_counts_and_ocr_progress_are_local(lifecycle_client, monkeypatch):  # noqa: F811
    seed()
    with session() as s, s.begin():
        s.add(SourcePoll(source="political_house", last_result={"availability": "available"}, last_polled_at=now()))
        s.add(SourcePoll(source="political_senate", last_result={"availability": "unavailable"},
                         last_error=msg("political.http_unavailable", status=403), last_polled_at=now()))
        jobs_before = s.scalar(select(func.count()).select_from(Job))
    def forbidden(*args, **kwargs):
        raise AssertionError("Read APIs must not request external sources")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    rows = {row["source"]: row for row in lifecycle_client.get("/api/v1/political/sources").json()["items"]}
    house = rows["house"]
    assert house["status"] == "available" and house["last_check_at"] is not None
    assert (house["report_count"], house["active_report_count"], house["transaction_count"], house["review_count"]) == (4, 2, 3, 3)
    assert (house["page_count"], house["pages_done"], house["ocr_pages"], house["ocr_seconds"], house["pending_reports"]) == (8, 6, 4, 12, 4)
    assert rows["senate"]["status"] == "unavailable" and "403" in rows["senate"]["last_error"]
    assert rows["oge"]["status"] == "unknown"
    for path in ("transactions", "people", "reports/new"):
        assert lifecycle_client.get(f"/api/v1/political/{path}").status_code == 200
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == jobs_before


def test_official_oge_report_date_sorts_and_filters_without_inventing_filing_date(lifecycle_client):  # noqa: F811
    seed()
    with session() as s, s.begin():
        current = s.get(PoliticalReport, "executive")
        current.filed_date = None
        current.report_date = date(2025, 3, 20)
        current.report_date_source = "official_index_label"
        s.add(PoliticalReport(id="executive-earlier", source="oge", external_id="earlier",
                              person_id="person-oge", filed_date=None, report_date=date(2025, 2, 10),
                              report_date_source="official_index_label", status="parsed",
                              url="https://example.gov/earlier.pdf"))
        s.flush()
        s.add(PoliticalTransaction(id="executive-earlier-1", report_id="executive-earlier", page=1,
                                   row_number=1, owner="self", asset_name="Synthetic bond",
                                   asset_type="bond", transaction_type="purchase", confidence="parsed",
                                   transaction_date=date(2025, 1, 27), raw_row="Synthetic bond row"))
    client = lifecycle_client
    base = "/api/v1/political/transactions"
    first = client.get(base, params={"limit": 1})
    assert ids(first) == ["executive-1"]
    assert first.json()["items"][0]["filed_date"] is None
    assert first.json()["items"][0]["report_date"] == "2025-03-20"
    assert first.json()["items"][0]["report_date_source"] == "official_index_label"
    second = client.get(base, params={"limit": 1, "cursor": first.json()["next_cursor"]})
    assert ids(second) == ["executive-earlier-1"]
    # House fixture dates fall back to the known received date for older stored rows.
    third = client.get(base, params={"limit": 1, "cursor": second.json()["next_cursor"]})
    assert ids(third) == ["new-2"]
    assert ids(client.get(base, params={"report_from": "2025-03-01", "report_to": "2025-03-31"})) == ["executive-1"]
    assert ids(client.get(base, params={"source": "oge", "report_to": "2025-03-01"})) == ["executive-earlier-1"]
    assert ids(client.get(base, params={"report_from": "2025-02-02", "report_to": "2025-02-02"})) == ["new-2", "new-1"]
    person = client.get("/api/v1/political/people", params={"source": "oge"}).json()["items"][0]
    assert person["latest_report_date"] == "2025-03-20" and person["report_count"] == 2
    detail = client.get("/api/v1/political/reports/executive").json()
    assert detail["filed_date"] is None and detail["report_date"] == "2025-03-20"
    assert detail["report_date_source"] == "official_index_label"


def test_manual_check_queues_once_and_preserves_backfill_cursor(lifecycle_client, monkeypatch):  # noqa: F811
    monkeypatch.setattr("iirp.sec.contact.configured", lambda: True)
    def forbidden(*args, **kwargs):
        raise AssertionError("A manual check only queues work")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    with session() as s, s.begin():
        s.add(SourcePoll(source="political_oge", catchup={"offset": 24}))
    for _ in range(2):
        response = lifecycle_client.post("/api/v1/political/sources/oge/refresh")
        assert response.status_code == 202
        assert response.json()["items"][2]["checking"]
    with session() as s:
        jobs = s.scalars(select(Job).where(Job.kind == "political_index")).all()
        assert len(jobs) == 1
        assert jobs[0].target["cursor"] == {"offset": 24} and jobs[0].trigger == "manual"
    assert lifecycle_client.post("/api/v1/political/sources/invalid/refresh").status_code == 422


def test_disclosure_source_cooldown_stays_out_of_system_health(lifecycle_client):  # noqa: F811
    """An unavailable disclosure source is shown on the Data page, not as a system issue."""
    from datetime import timedelta

    from iirp.models import SourceBudget

    with session() as s, s.begin():
        s.add(SourceBudget(provider="political_senate", next_allowed_at=now() + timedelta(hours=6), failures=4))
    keys = [group["key"] for group in lifecycle_client.get("/api/v1/system/health").json()["groups"]]
    assert not any(key.startswith("cooldown|political_") for key in keys)

    with session() as s, s.begin():
        s.add(SourceBudget(provider="sec", next_allowed_at=now() + timedelta(hours=1), failures=1))
    keys = [group["key"] for group in lifecycle_client.get("/api/v1/system/health").json()["groups"]]
    assert "cooldown|sec" in keys
