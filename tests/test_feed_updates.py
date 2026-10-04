"""Real PostgreSQL regressions for immutable incremental Insider reading."""

import copy
from datetime import date, timedelta

import pytest
from iirp.business_models import FeedRevision, FeedSession, Filing, TransactionEvent
from iirp.db import session
from iirp.feed_index import publish_current
from iirp.feed_updates import feed_updates, pending_feed_metadata
from iirp.models import Job, now
from iirp.sec_facts import _refresh_groups, feed, feed_group
from sqlalchemy import func, select
from test_sec_facts import FIXTURE, clean, isolated_database, save  # noqa: F401


def test_unchanged_check_has_no_card_projection_or_snapshot_write(monkeypatch):
    with session() as s, s.begin():
        save(s)
        original = feed(s, kind="buy")
        count = s.scalar(select(func.count()).select_from(FeedSession))
        monkeypatch.setattr("iirp.feed_updates.feed_groups", lambda *_: pytest.fail("check read card facts"))
        checked = feed_updates(s, original["session_id"])
        assert checked["new_count"] == 0
        assert checked["groups"] == checked["changed_ids"] == []
        assert checked["target_session_id"] == original["session_id"]
        assert s.scalar(select(func.count()).select_from(FeedSession)) == count


def test_revision_delta_preserves_old_snapshot_and_group_detail_version():
    with session() as s, s.begin():
        save(s)
        original = feed(s, kind="buy")
        group = original["groups"][0]
        raw = FIXTURE.read_bytes().replace(b"100000", b"125000")
        save(s, xml=raw, accession="0000000123-26-000002")
        checked = feed_updates(s, original["session_id"])
        assert checked["new_count"] == 1
        assert checked["changed_ids"] == [group["id"]]
        applied = feed_updates(s, original["session_id"], include_groups=True)
        assert len(applied["groups"]) == 1
        new = applied["groups"][0]
        assert new["id"] == group["id"] and new["revision_id"] != group["revision_id"]
        assert new["matching_transactions"] == group["matching_transactions"] + 1
        assert feed(s, original["session_id"], kind="buy")["groups"] == original["groups"]
        assert feed_group(s, applied["target_session_id"], group["id"])["revision_id"] == new["revision_id"]
        assert feed_group(s, original["session_id"], group["id"])["revision_id"] == group["revision_id"]
        assert feed_updates(s, applied["target_session_id"])["new_count"] == 0


def _add_revision(s, template, suffix, *, instant=None):
    data = copy.deepcopy(template.data)
    group_key = f"test-group-{suffix:020}"
    data["id"] = group_key
    row = FeedRevision(group_key=group_key, issuer_id=template.issuer_id,
                       accepted_at=instant or template.accepted_at,
                       data=data, match_kinds=template.match_kinds,
                       row_count=template.row_count,
                       transaction_sort_dates=template.transaction_sort_dates)
    s.add(row)
    s.flush()
    publish_current(s, [row.id])  # As a real publication does.
    return row


def test_delta_pages_freeze_and_base_pagination_deduplicates_even_with_late_facts():
    with session() as s, s.begin():
        save(s)
        template = s.scalar(select(FeedRevision))
        for index in range(30):
            _add_revision(s, template, index)
        base = feed(s, kind="buy")
        original_second = feed(s, base["session_id"], base["next_cursor"], kind="buy")
        for index in range(30, 55):
            _add_revision(s, template, index)
        first = feed_updates(s, base["session_id"], include_groups=True)
        assert len(first["groups"]) == 20 and first["new_count"] == 25
        assert first["next_cursor"] == "20"
        _add_revision(s, template, 100)
        second = feed_updates(s, base["session_id"], include_groups=True,
                              target_session_id=first["target_session_id"], cursor=first["next_cursor"])
        assert len(second["groups"]) == 5 and second["next_cursor"] is None
        keys = [group["id"] for group in first["groups"] + second["groups"]]
        assert len(keys) == len(set(keys)) == 25
        assert feed(s, base["session_id"], base["next_cursor"], kind="buy") == original_second
        assert feed_updates(s, first["target_session_id"])["new_count"] == 1
        other = feed(s, kind="sell")
        with pytest.raises(ValueError, match="不匹配"):
            feed_updates(s, other["session_id"], include_groups=True,
                         target_session_id=first["target_session_id"], cursor="20")


def test_tombstone_and_filter_departure_are_explicit_without_erasing_old_facts():
    with session() as s, s.begin():
        save(s)
        original = feed(s, kind="buy")
        group = original["groups"][0]
        for event in s.scalars(select(TransactionEvent)):
            event.status = "SUPERSEDED"
        s.flush()
        _refresh_groups(s, {(s.scalar(select(FeedRevision)).issuer_id, date(2026, 9, 1))})
        checked = feed_updates(s, original["session_id"])
        assert checked["new_count"] == 1
        applied = feed_updates(s, original["session_id"], include_groups=True)
        assert applied["removed_ids"] == [group["id"]]
        assert applied["groups"] == []
        assert feed_group(s, original["session_id"], group["id"])["total"] == 1
        assert feed_updates(s, applied["target_session_id"])["new_count"] == 0


def test_pending_total_counts_filings_and_stages_not_five_preview_or_job_attempts():
    with session() as s, s.begin():
        states = [None, "QUEUED", "RUNNING", "RETRY_WAIT", "PAUSED", "FAILED", "CANCELLED", "PARTIAL"]
        for index, state in enumerate(states):
            accession = f"0000000123-26-{index + 100:06}"
            s.add(Filing(accession=accession, form="4", status="DISCOVERED"))
            if state:
                s.add(Job(kind="sec_document", title="Synthetic document", target={"accession": accession},
                          idempotency_key=f"pending-{index}", status=state,
                          lease_token="valid-test-lease" if state == "RUNNING" else None,
                          lease_until=now() + timedelta(minutes=5) if state == "RUNNING" else None))
                # Historical attempt must neither multiply filing count nor mask latest status.
                s.add(Job(kind="sec_document", title="Synthetic old document", target={"accession": accession},
                          idempotency_key=f"pending-old-{index}", status="FAILED", created_at=now() - timedelta(days=1)))
        s.add(Filing(accession="0000000123-26-000999", form="4", visible=False))
        s.flush()
        summary, preview = pending_feed_metadata(s, preview=True)
        assert summary["total"] == len(states) == 8
        assert summary["preview_count"] == len(preview) == 5
        assert sum(stage["count"] for stage in summary["stages"]) == 8
        assert {stage["id"] for stage in summary["stages"]} == {
            "discovered", "waiting_download", "fetching_parsing", "retry_wait", "paused", "failed", "cancelled", "needs_review",
        }
        assert "含历史回补" in summary["scope"]
        checked, preview = pending_feed_metadata(s)
        assert checked["total"] == 8 and preview == []


def test_incremental_cursor_rejects_missing_target_and_expired_baseline():
    with session() as s, s.begin():
        save(s)
        original = feed(s, kind="buy")
        with pytest.raises(ValueError, match="缺少目标"):
            feed_updates(s, original["session_id"], include_groups=True, cursor="20")
        saved = s.get(FeedSession, original["session_id"])
        saved.expires_at = now() - timedelta(seconds=1)
        with pytest.raises(ValueError, match="过期"):
            feed_updates(s, saved.id)


def test_pending_running_requires_live_lease_and_keeps_control_intent():
    with session() as s, s.begin():
        examples = [
            ("RUNNING", "valid", now() + timedelta(minutes=5)),
            ("RUNNING", "expired", now() - timedelta(seconds=1)),
            ("RUNNING", None, now() + timedelta(minutes=5)),
            ("PAUSE_REQUESTED", "expired", now() - timedelta(seconds=1)),
            ("CANCEL_REQUESTED", None, None),
            ("PAUSE_REQUESTED", "valid", now() + timedelta(minutes=5)),
            ("CANCEL_REQUESTED", "valid", now() + timedelta(minutes=5)),
        ]
        for index, (status, token, until) in enumerate(examples):
            accession = f"0000000123-26-{800 + index:06}"
            s.add(Filing(accession=accession, form="4"))
            s.add(Job(kind="sec_document", title="Synthetic lease stage", target={"accession": accession},
                      idempotency_key=f"lease-{index}", status=status, lease_token=token, lease_until=until))
        s.flush()
        summary, _ = pending_feed_metadata(s)
        counts = {item["id"]: item["count"] for item in summary["stages"]}
        assert summary["total"] == 7
        assert counts == {"fetching_parsing": 1, "waiting_recovery": 2,
                          "pause_requested": 2, "cancel_requested": 2}
