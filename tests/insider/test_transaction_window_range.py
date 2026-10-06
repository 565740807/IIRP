"""Insider acquisition follows its three actual observation windows, not a buffer."""

from datetime import date, datetime
from uuid import uuid4

import pytest
from iirp.db import session
from iirp.jobs.lifecycle import _create, transaction_window
from iirp.models import Batch, RequestScope, TransactionEvent
from sqlalchemy import select

from tests.insider.test_transaction_mapping import _seed
from tests.sec.test_sec_facts import clean, isolated_database  # noqa: F401


def _mapped_event(transaction_day, accepted_at):
    event_id, security_id, _ = _seed()
    with session() as s, s.begin():
        event = s.get(TransactionEvent, event_id)
        event.transaction_date = date.fromisoformat(transaction_day)
        event.accepted_at = datetime.fromisoformat(accepted_at)
        event.data = {**event.data, "security_mapping": {
            "security_id": security_id, "version": 1,
            "evidence": "SYNTHETIC isolated range-planning test",
        }}
    return event_id


@pytest.mark.parametrize(
    "transaction_day,accepted_at,start,end",
    [
        ("2026-09-15", "2026-09-17T08:00:00-04:00", "2026-09-08", "2026-09-23"),
        ("2026-09-15", "2026-09-17T18:30:24-04:00", "2026-09-08", "2026-09-24"),
        ("2026-09-15", "2026-09-17T12:00:00-04:00", "2026-09-08", "2026-09-24"),
        ("2026-09-15", "2026-09-19T12:00:00-04:00", "2026-09-08", "2026-09-25"),
        ("2026-09-01", "2026-09-07T12:00:00-04:00", "2026-08-25", "2026-09-14"),
        ("2026-09-15", "2026-09-18T00:30:24+00:00", "2026-09-08", "2026-09-24"),
        ("2026-09-15", "2026-09-17T09:30:00-04:00", "2026-09-08", "2026-09-24"),
        ("2026-09-15", "2026-09-17T16:00:00-04:00", "2026-09-08", "2026-09-24"),
        ("2026-09-17", "2026-09-17T08:00:00-04:00", "2026-09-09", "2026-09-24"),
    ],
    ids=["before-open", "after-close", "intraday", "weekend", "labor-day", "utc-date",
         "exact-open", "exact-close", "trade-window-ends-last"],
)
def test_new_window_acquires_exact_observation_union(transaction_day, accepted_at, start, end):
    # End dates are independently enumerated XNYS sessions, including Labor Day.
    event_id = _mapped_event(transaction_day, accepted_at)
    result = transaction_window(event_id, str(uuid4()))
    params = result["batch"]["params"]
    assert (params["start_date"], params["end_date"]) == (start, end)
    assert result["batch"]["price_range"]["target_end_date"] == end
    with session() as s:
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == result["batch_id"]))
        assert (str(scope.start_date), str(scope.end_date)) == (start, end)


def test_old_request_replay_keeps_frozen_range_but_new_request_gets_exact_range():
    event_id = _mapped_event("2026-09-15", "2026-09-17T18:30:24-04:00")
    old_request = str(uuid4())
    with session() as s, s.begin():
        legacy, _ = _create(s, {
            "request_id": old_request, "kind": "market_history", "tickers": ["DEMO"],
            "start_date": "2026-09-08", "end_date": "2026-09-25",
            "purpose": "insider_window", "transaction_id": event_id,
        })
        legacy_id, original_params = legacy.id, dict(legacy.params)
    replay = transaction_window(event_id, old_request)
    assert replay["reused"] and replay["batch_id"] == legacy_id
    assert replay["batch"]["params"] == original_params
    fresh = transaction_window(event_id, str(uuid4()))
    assert fresh["batch_id"] != legacy_id
    assert fresh["batch"]["params"]["end_date"] == "2026-09-24"
    with session() as s:
        assert s.get(Batch, legacy_id).params == original_params
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == legacy_id))
        assert str(scope.end_date) == "2026-09-25"


def test_request_identity_cannot_replay_another_transaction():
    first = _mapped_event("2026-09-15", "2026-09-17T18:30:24-04:00")
    second = _mapped_event("2026-09-15", "2026-09-17T18:30:24-04:00")
    request_id = str(uuid4())
    transaction_window(first, request_id)
    with pytest.raises(ValueError, match="同一请求标识"):
        transaction_window(second, request_id)
