"""SEC latest polling keeps one state row and reaches its watermark.

Disposable iirp_v1_test_* database; the feed is an in-memory synthetic Atom
list served through the same parser as SEC, never the network.
"""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from iirp.db import session
from iirp.jobs.batches import defaults
from iirp.jobs.operations import ProviderFailure
from iirp.models import (
    CollectionStrategy,
    Filing,
    Job,
    SourceObject,
    SourceObservation,
    SourcePoll,
    now,
)
from iirp.sec import poll as sec_poll
from sqlalchemy import func, select

from tests.jobs.test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401

BASE = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)


def entry(number):
    accession = f"0001493152-26-{number:06d}"
    path = f"/Archives/edgar/data/1702924/{accession.replace('-', '')}/"
    accepted = (BASE + timedelta(seconds=number)).isoformat()
    return f"""<entry><id>urn:tag:sec.gov,2008:accession-number={accession}</id>
    <title>4 - SYNTHETIC ONLY (0001702924) (Reporting)</title>
    <category scheme="https://www.sec.gov/" term="4"/>
    <updated>{accepted}</updated><link rel="alternate" type="text/html"
    href="https://www.sec.gov{path}{accession}-index.htm"/>
    <summary type="html">&lt;b&gt;Filed:&lt;/b&gt; 2026-10-05</summary></entry>"""


class Feed:
    """Newest first, like SEC getcurrent; ``start``/``count`` paging."""

    def __init__(self, count):
        self.newest = count
        self.requests = 0

    def publish(self, count):
        self.newest += count

    def __call__(self, url):
        self.requests += 1
        query = parse_qs(urlsplit(url).query)
        start, count = int(query["start"][0]), int(query["count"][0])
        numbers = range(self.newest - start, max(0, self.newest - start - count), -1)
        body = "".join(entry(number) for number in numbers)
        return f'<feed xmlns="http://www.w3.org/2005/Atom">{body}</feed>'.encode()


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr("iirp.jobs.providers.sec_configured", lambda: True)
    with session() as s, s.begin():
        defaults(s)


def poll(feed):
    with session() as s, s.begin():
        row = s.get(SourcePoll, sec_poll.SOURCE)
        if row is not None:
            row.next_poll_at = now()  # Each call is due, whatever the cadence.
    return sec_poll.poll_once(fetch=feed)


def state():
    with session() as s:
        return s.get(SourcePoll, sec_poll.SOURCE)


def filings():
    with session() as s:
        return s.scalar(select(func.count()).select_from(Filing))


def test_first_poll_walks_the_feed_once_then_polls_only_new_pages():
    feed = Feed(2600)
    first = poll(feed)
    assert first["pages"] == sec_poll.POLL_PAGES and first["catchup_open"]
    row = state()
    assert row.watermark_at == BASE + timedelta(seconds=2600)
    assert row.next_poll_at <= now() + timedelta(seconds=sec_poll.CATCHUP_DELAY_SECONDS)
    assert row.last_complete_at is None
    while state().catchup:
        poll(feed)
    assert filings() == 2600
    assert state().last_complete_at is not None
    # Steady state: one page per poll and no job rows at all.
    feed.publish(30)
    requests = feed.requests
    result = poll(feed)
    assert result["head"] == "watermark_reached" and result["pages"] == 1
    assert feed.requests == requests + 1
    assert filings() == 2630
    assert state().watermark_at == BASE + timedelta(seconds=2630)
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 0


def test_burst_beyond_head_budget_is_caught_up_down_to_the_old_watermark():
    feed = Feed(100)
    while poll(feed)["catchup_open"]:
        pass
    old = state().watermark_at
    feed.publish(2500)  # More than one poll's page budget.
    result = poll(feed)
    assert result["head"] == "page_budget" and result["catchup_open"]
    assert state().catchup["watermark"] == old.isoformat()
    while state().catchup:
        poll(feed)
    assert filings() == 2600
    assert state().watermark_at == old + timedelta(seconds=2500)
    assert state().last_result["gaps"] == []


def test_list_pages_expire_after_seven_days_and_are_deleted_with_observations():
    from iirp.storage.objects import expire_sources

    feed = Feed(10)
    poll(feed)
    with session() as s:
        pages = s.scalars(select(SourceObject).where(SourceObject.media_type == "application/atom+xml")).all()
        assert pages and all(page.expires_at > now() + timedelta(days=6) for page in pages)
        assert s.scalar(select(func.count()).select_from(SourceObservation)) == len(pages)
    with session() as s, s.begin():
        for page in s.scalars(select(SourceObject)):
            page.expires_at = now() - timedelta(seconds=1)
        paths = [page.relative_path for page in s.scalars(select(SourceObject))]
    removed = expire_sources()
    assert removed["objects"] == len(paths)
    with session() as s:
        assert s.scalar(select(func.count()).select_from(SourceObject)) == 0
        assert s.scalar(select(func.count()).select_from(SourceObservation)) == 0
    from iirp.config import settings
    assert not any((settings().runtime_dir / path).exists() for path in paths)
    # Facts discovered from expired pages remain.
    assert filings() == 10


def test_permanent_registration_wins_over_expiry():
    from iirp.storage.objects import register_object, save_object

    source = save_object(b"synthetic shared content", "text/plain")
    with session() as s, s.begin():
        register_object(s, {**source, "expires_at": now() + timedelta(days=7)})
        register_object(s, source)
        register_object(s, {**source, "expires_at": now() + timedelta(days=7)})
    with session() as s:
        assert s.get(SourceObject, source["sha256"]).expires_at is None


def test_no_poll_without_contact_or_when_paused(monkeypatch):
    feed = Feed(10)
    with session() as s, s.begin():
        s.get(CollectionStrategy, "sec").enabled = False
    assert poll(feed) is None and feed.requests == 0
    with session() as s, s.begin():
        s.get(CollectionStrategy, "sec").enabled = True
    monkeypatch.setattr("iirp.jobs.providers.sec_configured", lambda: False)
    assert poll(feed) is None and feed.requests == 0


def test_source_failure_is_recorded_and_backs_off_without_moving_the_watermark():
    feed = Feed(10)
    poll(feed)
    before = state().watermark_at

    def unavailable(_url):
        raise ProviderFailure("SEC HTTP 429，共享冷却", 300, 429, source_wait=True)

    poll(unavailable)
    row = state()
    assert row.last_error and row.failures == 1
    assert row.next_poll_at >= now() + timedelta(seconds=250)
    assert row.watermark_at == before and row.lease_token is None
