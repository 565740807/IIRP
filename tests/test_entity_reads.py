"""Immutable company/person paging; all source fixtures are synthetic."""

from copy import deepcopy
from datetime import timedelta

import pytest
from iirp.business_models import FeedSession, FilingVersion, TransactionEvent
from iirp.db import session
from iirp.models import now
from iirp.sec_facts import _compact_row, _event_view, _summary, entity_history
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session
from test_sec_facts import FIXTURE, clean, isolated_database, save  # noqa: F401


def test_old_entity_page_reads_original_observation_after_current_event_changes():
    with session() as s, s.begin():
        save(s)
        before = entity_history(s, "owner", "456", recent_count=50, limit=100)
        first = deepcopy(before["items"])
        snapshot = before["data"]["session_id"]
        # A source correction creates a new observation and marks old facts.
        save(s, xml=FIXTURE.read_bytes().replace(b"25.25", b"30.25"))
        old = s.get(TransactionEvent, first[0]["id"])
        old.data = {
            **old.data,
            "price_per_share": "999999",
            "owner_names": {"0000000456": "changed"},
        }
        old.status = "WITHDRAWN"
        old.transaction_date = None
        old.accepted_at = None
        s.flush()
        after = entity_history(s, "owner", "456", recent_count=50, limit=1, cursor=snapshot + ":0")
        second = entity_history(
            s, "owner", "456", recent_count=50, limit=1, cursor=after["data"]["next_cursor"]
        )
        assert after["items"] + second["items"] == first
        assert after["data"]["summary"] == before["data"]["summary"]


def test_entity_page_does_not_materialize_mutable_facts_or_full_snapshot_models():
    with session() as s, s.begin():
        save(s)
    loaded = []

    def observed(_s, item):
        if isinstance(item, (FeedSession, FilingVersion, TransactionEvent)):
            loaded.append(type(item).__name__)

    event.listen(Session, "loaded_as_persistent", observed)
    try:
        with session() as s, s.begin():
            first = entity_history(s, "company", "123", limit=1)
            last = entity_history(s, "company", "123", limit=1, cursor=first["data"]["next_cursor"])
            assert len(first["items"]) == len(last["items"]) == 1
            assert loaded == []
            metadata = s.scalar(
                select(FeedSession.filters).where(FeedSession.id == first["data"]["session_id"])
            )
            assert "rows" not in metadata
            assert metadata["schema"] == "entity-index-v1"
    finally:
        event.remove(Session, "loaded_as_persistent", observed)


def test_entity_whole_scope_sql_totals_match_economic_row_rules():
    with session() as s, s.begin():
        save(s)
        events = s.scalars(select(TransactionEvent)).all()
        events[0].status = "NEEDS_REVIEW"
        s.flush()
        expected = _summary([_compact_row(_event_view(row, compact=True)) for row in events])
        result = entity_history(s, "company", "123", limit=1)
        assert sorted(result["data"]["summary"], key=lambda r: r["code"]) == sorted(
            expected, key=lambda r: r["code"]
        )
        assert result["data"]["total"] == 2


def test_missing_immutable_observation_never_falls_back_to_current_mutable_data():
    with session() as s, s.begin():
        save(s)
        version = s.scalar(select(FilingVersion))
        version.data = {**version.data, "rows": []}
        s.flush()
        with pytest.raises(ValueError, match="不可变"):
            entity_history(s, "company", "123")


def test_legacy_entity_snapshot_remains_readable_until_expiry():
    with session() as s, s.begin():
        save(s)
        rows = [
            _compact_row(_event_view(row, compact=True))
            for row in s.scalars(select(TransactionEvent))
        ]
        legacy = FeedSession(
            revision_ids=[],
            filters={
                "purpose": "entity",
                "kind": "owner",
                "id": "0000000456",
                "start": None,
                "end": None,
                "recent_count": 50,
                "date_basis": "transaction_date",
                "as_of": now().isoformat(),
                "rows": rows,
            },
            expires_at=now() + timedelta(hours=1),
        )
        s.add(legacy)
        s.flush()
        result = entity_history(s, "owner", "456", recent_count=50, session_id=legacy.id)
        assert result["items"] == rows


def test_first_index_restores_normal_statement_budget_and_supports_both_date_orders():
    with session() as s, s.begin():
        save(s)
        previous = s.scalar(select(func.current_setting("statement_timeout")))
        for basis in ("transaction_date", "accepted_at"):
            result = entity_history(s, "owner", "456", recent_count=1, date_basis=basis)
            assert result["data"]["total"] == 1
            assert len(result["items"]) == 1
            assert s.scalar(select(func.current_setting("statement_timeout"))) == previous
