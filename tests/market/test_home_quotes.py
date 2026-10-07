"""Home strip freshness (live / closed / delayed) is judged at read time; no database or Yahoo."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from iirp.api.reads import _quote_freshness
from iirp.config import refresh
from iirp.messages import decode

SESSION = {"start": "2026-10-07T13:30:00+00:00", "end": "2026-10-07T20:00:00+00:00"}


def quote(fetched, status="DELAYED", session=SESSION):
    return {"fetched_at": fetched, "status": status, "session": session}


def at(text):
    return datetime.fromisoformat(text)


@pytest.mark.parametrize(("fetched", "current", "status", "state", "code"), [
    ("2026-10-07T15:00:00+00:00", "2026-10-07T15:01:00+00:00", "DELAYED", "live", None),
    ("2026-10-07T15:00:00+00:00", "2026-10-07T15:04:00+00:00", "DELAYED", "delayed", "home.quote.overdue"),
    ("2026-10-07T15:00:00+00:00", "2026-10-07T15:01:00+00:00", "STALE", "delayed", "quote.delay.stale"),
    ("2026-10-07T19:50:00+00:00", "2026-10-07T21:00:00+00:00", "DELAYED", "delayed", "home.quote.close_pending"),
    ("2026-10-07T20:40:00+00:00", "2026-10-08T01:00:00+00:00", "CLOSED", "closed", None),
    ("2026-10-07T08:00:00+00:00", "2026-10-07T08:01:00+00:00", "CLOSED", "closed", None),
])
def test_freshness_states(fetched, current, status, state, code):
    result, reason = _quote_freshness(quote(fetched, status), None, at(current))
    assert result == state
    assert (decode(reason)["code"] if reason else None) == code


def test_a_failed_refresh_after_the_last_fetch_explains_the_delay():
    failure = SimpleNamespace(updated_at=at("2026-10-07T15:00:30+00:00"), error="Yahoo cooling down")
    assert _quote_freshness(quote("2026-10-07T15:00:00+00:00"), failure, at("2026-10-07T15:01:00+00:00")) == (
        "delayed", "Yahoo cooling down")
    older = SimpleNamespace(updated_at=at("2026-10-07T14:00:00+00:00"), error="old")
    assert _quote_freshness(quote("2026-10-07T15:00:00+00:00"), older, at("2026-10-07T15:01:00+00:00"))[0] == "live"


def test_overdue_threshold_and_browser_intervals_come_from_the_config_file():
    cadence = refresh()
    assert cadence["quotes"]["overdue_seconds"] == 180
    assert cadence["sec"]["session_seconds"] == 60 and cadence["quotes"]["extended_seconds"] == 300
    assert set(cadence["browser"]) == {"home_poll_seconds", "feed_poll_seconds", "ensure_seconds"}
    fetched = datetime(2026, 10, 7, 15, tzinfo=timezone.utc)
    reason = _quote_freshness(quote(fetched.isoformat()), None, fetched + timedelta(seconds=181))[1]
    assert decode(reason) == {"code": "home.quote.overdue", "params": {"minutes": 3}}
