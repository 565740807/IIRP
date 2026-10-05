"""Company-filtered, bounded entity snapshots using synthetic isolated PG data."""

from copy import deepcopy

import pytest
from iirp.business_models import FeedSession, Issuer, TransactionEvent
from iirp.db import session
from iirp.entity_reads import read_entity_history
from sqlalchemy import select
from test_sec_facts import FIXTURE, clean, isolated_database, save  # noqa: F401


def save_company(
    s, issuer, *, day="2026-08-31", accepted="2026-09-01T18:00:00-04:00", owned=True, suffix=1, ticker=None
):
    xml = (
        FIXTURE.read_bytes()
        .replace(b"<issuerCik>123</issuerCik>", f"<issuerCik>{issuer}</issuerCik>".encode())
        .replace(b"SYNTHETIC Example Issuer", f"SYNTHETIC Company {issuer}".encode())
        .replace(b">DEMO<", f">{ticker if ticker is not None else f'S{issuer}'}<".encode())
        .replace(b"2026-08-31", day.encode())
    )
    if not owned:
        xml = xml.replace(b"<rptOwnerCik>456</rptOwnerCik>", b"<rptOwnerCik>888</rptOwnerCik>")
    save(s, xml=xml, accession=f"{issuer:010d}-26-{suffix:06d}", accepted=accepted)


def test_company_filter_preserves_all_owner_company_options_and_joint_attribution():
    with session() as s, s.begin():
        save_company(s, 123)
        save_company(s, 321)
        save_company(s, 999, owned=False)
        result = read_entity_history(s, "person", "456", issuer_id="321")
        assert result["data"]["total"] == 2
        assert {row["issuer_id"] for row in result["items"]} == {"0000000321"}
        assert {row["issuer_name"] for row in result["items"]} == {"SYNTHETIC Company 321"}
        assert all(row["owner_ids"] == ["0000000456", "0000000789"] for row in result["items"])
        assert result["data"]["companies"] == [
            {"issuer_id": "0000000123", "name": "SYNTHETIC Company 123", "ticker": "S123"},
            {"issuer_id": "0000000321", "name": "SYNTHETIC Company 321", "ticker": "S321"},
        ]

        unrelated = read_entity_history(s, "person", "456", issuer_id="999")
        assert unrelated["items"] == []
        assert unrelated["data"]["total"] == 0
        assert unrelated["data"]["companies"] == result["data"]["companies"]


def test_placeholder_ticker_keeps_source_text_without_becoming_market_symbol():
    with session() as s, s.begin():
        save_company(s, 123, ticker="none")
        result = read_entity_history(s, "person", "456")
        assert result["data"]["companies"] == [{
            "issuer_id": "0000000123",
            "name": "SYNTHETIC Company 123",
            "ticker": None,
            "issuer_ticker_raw": "none",
        }]


def test_filtered_snapshot_freezes_rows_names_and_companies_and_rejects_changed_filter():
    with session() as s, s.begin():
        save_company(s, 123)
        save_company(s, 321)
        before = read_entity_history(s, "owner", "456", issuer_id="321", limit=100)
        expected = deepcopy(before)
        snapshot = before["data"]["session_id"]
        save_company(s, 654)
        s.get(Issuer, "0000000321").name = "changed mutable name"
        for row in s.scalars(
            select(TransactionEvent).where(TransactionEvent.issuer_id == "0000000321")
        ):
            row.data = {**row.data, "ticker": "CHANGED"}
            row.status = "WITHDRAWN"
        s.flush()
        first = read_entity_history(
            s, "owner", "456", issuer_id="321", cursor=snapshot + ":0", limit=1
        )
        second = read_entity_history(
            s, "owner", "456", issuer_id="321", cursor=first["data"]["next_cursor"], limit=1
        )
        assert first["items"] + second["items"] == expected["items"]
        assert (
            first["data"]["companies"]
            == second["data"]["companies"]
            == expected["data"]["companies"]
        )
        assert second["data"]["summary"] == expected["data"]["summary"]
        with pytest.raises(ValueError, match="历史条件"):
            read_entity_history(s, "owner", "456", issuer_id="123", cursor=snapshot + ":0")


def test_recent_count_is_limited_to_date_range_before_paging():
    with session() as s, s.begin():
        # Disclosed after the latest trade: a trade dated after its acceptance is a date anomaly.
        for suffix, day in enumerate(["2026-08-01", "2026-09-02", "2026-09-04", "2026-09-07"], 1):
            save_company(s, 123, day=day, suffix=suffix, accepted="2026-09-10T18:00:00-04:00")
        args = {
            "start": "2026-09-01",
            "end": "2026-09-05",
            "recent_count": 3,
            "issuer_id": "123",
            "limit": 1,
        }
        result = read_entity_history(s, "person", "456", **args)
        assert result["data"]["total"] == 3
        assert result["data"]["coverage"]["start_date"] == "2026-09-01"
        assert result["data"]["coverage"]["end_date"] == "2026-09-05"
        dates = [row["transaction_date"] for row in result["items"]]
        while result["data"]["next_cursor"]:
            result = read_entity_history(
                s, "person", "456", cursor=result["data"]["next_cursor"], **args
            )
            dates.extend(row["transaction_date"] for row in result["items"])
        assert dates == ["2026-09-04", "2026-09-04", "2026-09-02"]


def test_acceptance_date_bounds_use_eastern_midnight():
    with session() as s, s.begin():
        save_company(s, 123, accepted="2026-09-02T03:59:00+00:00", suffix=1)
        save_company(s, 123, accepted="2026-09-02T04:00:00+00:00", suffix=2)
        result = read_entity_history(
            s, "owner", "456", "2026-09-01", "2026-09-01", date_basis="accepted", issuer_id="123"
        )
        assert result["data"]["total"] == 2
        assert all(row["accession"].endswith("000001") for row in result["items"])
        assert result["data"]["coverage"]["start_date"] == "2026-09-01"


def test_unknown_person_retains_requested_bounds_and_empty_company_options():
    with session() as s, s.begin():
        result = read_entity_history(
            s, "person", "456", "2026-06-01", "2026-09-01", issuer_id="123"
        )
        assert result["items"] == []
        assert result["data"]["companies"] == []
        assert result["data"]["coverage"]["start_date"] == "2026-06-01"
        assert result["data"]["coverage"]["end_date"] == "2026-09-01"
        assert result["data"]["coverage"]["complete"] is False


def test_immutable_entity_amendment_rows_explain_exclusion_and_keep_reported_values():
    with session() as s, s.begin():
        save_company(s, 123)
        event = s.scalar(select(TransactionEvent).where(TransactionEvent.data["code"].astext == "P"))
        event.status = "NEEDS_REVIEW"
        s.flush()
        result = read_entity_history(s, "company", "123", action="buy")
        row = result["items"][0]
        assert row["shares"] == "100000" and row["price"] == "25.25"
        assert row["amount"] is None and row["eligible_for_totals"] is False
        assert "源申报数值暂未计入确认汇总" in row["summary_exclusion_reason"]
        assert row["owners"][0]["roles"] == ["董事"]
        frozen = deepcopy(row)
        event.status = "CURRENT"
        s.flush()
        returned = read_entity_history(s, "company", "123", action="buy", cursor=result["data"]["session_id"] + ":0")
        assert returned["items"][0] == frozen


def test_person_history_roles_belong_to_each_original_filing_owner_and_company():
    with session() as s, s.begin():
        for issuer, day, role in [(321, "2026-08-01", "CFO"), (321, "2026-08-02", "CTO"), (654, "2026-08-03", None)]:
            xml = FIXTURE.read_bytes().replace(b"<issuerCik>123</issuerCik>", f"<issuerCik>{issuer}</issuerCik>".encode()).replace(b"2026-08-31", day.encode())
            if role:
                xml = xml.replace(b"<isDirector>1</isDirector><isOfficer>0</isOfficer>",
                                  f"<isDirector>0</isDirector><isOfficer>1</isOfficer><officerTitle>{role}</officerTitle>".encode())
            save(s, xml=xml, accession=f"{issuer:010}-26-0000{day[-2:]}")
        result = read_entity_history(s, "person", "456", "2026-08-01", "2026-08-03", action="buy")
        own = [next(owner for owner in row["owners"] if owner["id"] == "0000000456")["roles"] for row in result["items"]]
        assert own == [["董事"], ["CTO"], ["CFO"]]
        assert all(next(owner for owner in row["owners"] if owner["id"] == "0000000789")["roles"] == ["持股超过10%"] for row in result["items"])
        assert [row["issuer_id"] for row in result["items"]] == ["0000000654", "0000000321", "0000000321"]


@pytest.mark.parametrize("action", ["buy", "derivative"])
def test_person_summary_counts_only_focused_subject_without_multiplying_joint_trades(action):
    with session() as s, s.begin():
        save_company(s, 123, day="2026-08-30", suffix=1)
        save_company(s, 123, day="2026-08-31", suffix=2)
        save_company(s, 321, day="2026-08-31", suffix=3)
        args = {"issuer_id": "123", "action": action, "limit": 1}
        person = read_entity_history(s, "person", "456", **args)
        joint = read_entity_history(s, "person", "789", **args)
        company = read_entity_history(s, "company", "123", action=action)
        p, j, c = (result["data"]["summary"][0] for result in (person, joint, company))
        assert (p["owner_count"], p["person_count"], p["unknown_owner_count"]) == (1, 1, 0)
        assert (j["owner_count"], j["person_count"], j["unknown_owner_count"]) == (1, 0, 1)
        assert (c["owner_count"], c["person_count"], c["unknown_owner_count"]) == (2, 1, 1)
        for field in ["rows", "shares", "known_amount", "known_price_rows", "review_rows"]:
            assert p[field] == j[field] == c[field]
        assert p["rows"] == 2
        assert all(result["data"]["filings"] == 2 for result in (person, joint, company))
        assert person["items"][0]["owner_ids"] == ["0000000456", "0000000789"]
        save_company(s, 123, day="2026-08-31", suffix=4)
        second = read_entity_history(s, "person", "456", cursor=person["data"]["next_cursor"], **args)
        assert second["data"]["summary"] == person["data"]["summary"]
        assert second["data"]["filings"] == 2


def test_legacy_person_snapshot_labels_joint_scope_and_keeps_frozen_totals():
    with session() as s, s.begin():
        save_company(s, 123, suffix=1)
        save_company(s, 123, suffix=2)
        old = read_entity_history(s, "person", "456", action="buy", limit=1)
        company = read_entity_history(s, "company", "123", action="buy")
        snapshot = s.get(FeedSession, old["data"]["session_id"])
        metadata = {**snapshot.filters, "summary": company["data"]["summary"]}
        metadata.pop("summary_owner_scope")
        snapshot.filters = metadata
        save_company(s, 123, suffix=3)
        restored = read_entity_history(s, "person", "456", action="buy", limit=1, cursor=old["data"]["next_cursor"])
        assert restored["data"]["summary_owner_scope"] == "legacy_joint_subjects"
        assert "人数含联合申报主体" in restored["data"]["summary_note"]
        assert restored["data"]["summary"] == company["data"]["summary"]
        assert restored["data"]["total"] == 2
        fresh = read_entity_history(s, "person", "456", action="buy")
        assert fresh["data"]["summary_owner_scope"] == "focused_subject"
        assert fresh["data"]["summary_note"] is None
        assert fresh["data"]["summary"][0]["owner_count"] == 1
        assert fresh["data"]["total"] == 3
