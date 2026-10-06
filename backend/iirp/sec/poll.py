"""SEC latest-feed polling: one state row per source, never a job per poll.

Each poll reads the newest feed page(s) until it reaches the watermark (the
newest acceptance instant a previous poll covered without a gap) and then
advances it. If the page budget runs out first, the unread remainder is kept
as a ``catchup`` cursor that later polls continue until they reach the old
watermark or the feed's end; meanwhile the head keeps moving. The first poll
without a watermark walks the feed once that way. A feed that ends before the
watermark is a gap (recorded in ``last_result``); previous days are reconciled
by the automatic SEC history backfill from daily/quarterly indexes.

Requests go through the shared SEC slot limiter (``fetch_sec``); list pages
expire after 7 days. Discovered filings are saved idempotently and the day's
latest batch is signalled, so its planner schedules the document downloads.
"""

import base64
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from iirp.db import session
from iirp.models import Batch, CollectionStrategy, SourcePoll, now

SOURCE = "sec_latest"
HEAD_PAGES = 5
POLL_PAGES = 20
LEASE_SECONDS = 120
OPEN_MIN_SECONDS = 30
CATCHUP_DELAY_SECONDS = 5
PAUSED = ("PAUSED", "PAUSE_REQUESTED", "CANCEL_REQUESTED")


class LeaseLost(Exception):
    pass


def _row(s, *, lock=False):
    # Only the polling worker writes this row (see request_poll).
    s.execute(insert(SourcePoll).values(source=SOURCE, next_poll_at=now(), updated_at=now())
              .on_conflict_do_nothing())
    return s.get(SourcePoll, SOURCE, with_for_update=lock, populate_existing=True)


def request_poll(s, policy, *, force=False):
    """Ask for an early poll without touching the worker-owned poll row.

    Page opens already hold the SEC strategy row, so the request is noted
    there; only the polling worker writes the poll row, which keeps the web's
    short lock budget out of the poll's commits. An open takes effect at most
    30 seconds after the previous poll; a manual refresh at once.
    """
    stamp = now().isoformat()
    policy.options = {**(policy.options or {}), "poll_requested_at": stamp,
                      **({"poll_forced_at": stamp} if force else {})}


def _due(row, options, stamp):
    if row is None:
        return True
    if row.lease_until and row.lease_until > stamp:
        return False
    if row.next_poll_at <= stamp:
        return True
    last = row.last_polled_at

    def after_last(key):
        value = options.get(key)
        return bool(value) and (last is None or datetime.fromisoformat(value) > last)

    if after_last("poll_forced_at"):
        return True
    return after_last("poll_requested_at") and (
        last is None or (stamp - last).total_seconds() >= OPEN_MIN_SECONDS)


def poll_due():
    stamp = now()
    with session() as s:
        policy = s.get(CollectionStrategy, "sec")
        return _due(s.get(SourcePoll, SOURCE), (policy.options or {}) if policy else {}, stamp)


def _paused(s):
    manual = s.scalar(select(Batch.id).where(
        Batch.kind == "sec_latest", Batch.trigger == "manual", Batch.requested_action.is_(None),
        Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT"))).limit(1))
    if manual:
        return False  # An explicit refresh polls even while automatic updates are off.
    policy = s.get(CollectionStrategy, "sec")
    if policy is None or not policy.enabled:
        return True
    newest = s.scalar(select(Batch.status).where(Batch.kind == "sec_latest", Batch.trigger == "automatic")
                      .order_by(Batch.created_at.desc()).limit(1))
    return newest in PAUSED


def claim():
    """Take the poll lease when due; returns the token or None."""
    from iirp.jobs.providers import sec_configured
    from iirp.jobs.schedule import sec_poll_seconds

    with session() as s, s.begin():
        row = _row(s, lock=True)
        stamp = now()
        policy = s.get(CollectionStrategy, "sec")
        if not _due(row, (policy.options or {}) if policy else {}, stamp):
            return None
        if not sec_configured() or _paused(s):
            # Stay idle without requests; re-check on the normal cadence.
            row.next_poll_at = stamp + timedelta(seconds=sec_poll_seconds(stamp))
            row.updated_at = stamp
            return None
        token = str(uuid.uuid4())
        row.lease_token, row.lease_until, row.updated_at = token, stamp + timedelta(seconds=LEASE_SECONDS), stamp
        return token


def _iso(value):
    return value.isoformat() if value else None


def _later(left, right):
    values = [value for value in (left, right) if value]
    return max(values, key=lambda value: datetime.fromisoformat(value) if isinstance(value, str) else value,
               default=None)


def _commit_page(token, response):
    """Save one page and its filings under the lease; returns new filing count."""
    from iirp.insider.facts import persist_discovery
    from iirp.jobs.signals import latest_sec_demand, signal_batch
    from iirp.storage.objects import discovery_expiry, register_object, save_object

    sources = {}
    for document in response.get("source_documents", []):
        content = (base64.b64decode(document["payload"]) if document.get("encoding") == "base64"
                   else document["payload"].encode())
        sources[document["url"]] = {**save_object(content, document.get("media_type", "application/atom+xml")),
                                    "expires_at": discovery_expiry()}
    with session() as s, s.begin():
        row = s.get(SourcePoll, SOURCE, with_for_update=True)
        if row is None or row.lease_token != token:
            raise LeaseLost
        row.lease_until = now() + timedelta(seconds=LEASE_SECONDS)
        for source in sources.values():
            register_object(s, source)
        needed = persist_discovery(s, None, response, {url: item["sha256"] for url, item in sources.items()})
        if needed:
            # The day's demand (and any manual refresh) plans the document downloads.
            demands = set(s.scalars(select(Batch.id).where(
                Batch.kind == "sec_latest", Batch.trigger == "manual", Batch.requested_action.is_(None),
                Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT")))))
            newest = latest_sec_demand(s)
            if newest:
                demands.add(newest)
            for identifier in demands:
                signal_batch(s, identifier)
        return len(needed)


def _save(token, **values):
    with session() as s, s.begin():
        row = s.get(SourcePoll, SOURCE, with_for_update=True)
        if row is None or row.lease_token != token:
            raise LeaseLost
        for key, value in values.items():
            setattr(row, key, value)
        row.lease_until = now() + timedelta(seconds=LEASE_SECONDS)
        row.updated_at = now()


def _target(watermark, cursor):
    target = {"mode": "latest", "max_pages": 1, "page_size": 100}
    if watermark:
        target["watermark"] = watermark
    if cursor:
        target["cursor"] = cursor
    return target


def _next_cursor(response):
    """Continuation after a budget stop; a repeated page is skipped, not retried."""
    scan, cursor = response.get("scan", {}), dict(response.get("cursor") or {})
    if scan.get("reason") in ("repeated_page", "non_advancing_page"):
        # The page equals one already saved in this scan; moving past it is safe.
        cursor.update(start=int(cursor.get("start", 0)) + 100, next_url=None)
    return cursor


CONTINUE = ("page_budget", "repeated_page", "non_advancing_page")


def _run(token, stopping, fetch):
    from iirp.sec.fetch import run_sec_operation

    with session() as s:
        row = s.get(SourcePoll, SOURCE)
        watermark, catchup = _iso(row.watermark_at), row.catchup
    result = {"pages": 0, "pending_documents": 0, "gaps": []}

    def page(target):
        response = run_sec_operation("sec_discover", target, fetch=fetch)
        result["pages"] += 1
        result["pending_documents"] += _commit_page(token, response)
        return response

    # Head: newest page(s) down to the watermark.
    cursor, newest, reason = None, None, None
    for _ in range(HEAD_PAGES):
        if stopping():
            return result
        response = page(_target(watermark, cursor))
        scan = response.get("scan", {})
        reason = scan.get("reason")
        newest = _later(newest, scan.get("newest_accepted_at"))
        if scan.get("complete") or reason not in CONTINUE:
            break
        cursor = _next_cursor(response)
    result["head"] = reason
    if reason == "feed_exhausted_before_watermark" and watermark:
        result["gaps"].append({"after": watermark, "before": scan.get("oldest_accepted_at")})
    elif reason in CONTINUE:
        # Keep the oldest unresolved boundary when an earlier catchup is still open.
        catchup = {"cursor": cursor, "watermark": catchup["watermark"] if catchup else watermark}
    elif reason not in ("watermark_reached", "feed_exhausted_before_watermark"):
        result["error"] = reason
        return result
    new_watermark = _later(watermark, newest)
    _save(token, watermark_at=datetime.fromisoformat(new_watermark) if new_watermark else None, catchup=catchup)
    # Catchup: the remainder of an interrupted scan, within this poll's budget.
    while catchup and result["pages"] < POLL_PAGES and not stopping():
        response = page(_target(catchup["watermark"], catchup["cursor"]))
        scan = response.get("scan", {})
        reason = scan.get("reason")
        if scan.get("complete") or reason == "feed_exhausted_before_watermark":
            if reason == "feed_exhausted_before_watermark" and catchup["watermark"]:
                result["gaps"].append({"after": catchup["watermark"], "before": scan.get("oldest_accepted_at")})
            catchup = None
        elif reason in CONTINUE:
            catchup = {**catchup, "cursor": _next_cursor(response)}
        else:
            result["error"] = reason
            break
        _save(token, catchup=catchup)
    result["catchup_open"] = bool(catchup)
    return result


def poll_once(stopping=lambda: False, fetch=None):
    """Run one due poll; returns its result, or None when not due or not allowed."""
    from iirp.jobs.operations import ProviderFailure, fetch_sec
    from iirp.jobs.schedule import sec_poll_seconds

    token = claim()
    if token is None:
        return None
    error, retry = None, None
    try:
        result = _run(token, stopping, fetch or fetch_sec)
        error = result.get("error")
    except LeaseLost:
        return None
    except ProviderFailure as exc:
        result, error, retry = {}, str(exc), max(60.0, float(exc.retry_seconds))
    except Exception as exc:
        logging.exception("sec latest poll failed type=%s", type(exc).__name__)
        result, error, retry = {}, f"轮询失败（{type(exc).__name__}）", 60.0
    stamp = now()
    with session() as s, s.begin():
        row = s.get(SourcePoll, SOURCE, with_for_update=True)
        if row is None or row.lease_token != token:
            return None
        row.last_polled_at = stamp
        if error:
            row.failures += 1
            row.last_error = error
            delay = retry or min(900, 30 * 2 ** min(row.failures - 1, 5))
        else:
            row.failures = 0
            row.last_error = None
            row.last_success_at = stamp
            if not row.catchup:
                row.last_complete_at = stamp
            delay = CATCHUP_DELAY_SECONDS if row.catchup else sec_poll_seconds(stamp)
        row.last_result = {**result, "at": stamp.isoformat()}
        row.next_poll_at = stamp + timedelta(seconds=delay)
        row.lease_token = row.lease_until = None
        row.updated_at = stamp
    return result


def release_stale_lease():
    """A restarted coordinator does not wait for a dead poll's lease to expire."""
    with session() as s, s.begin():
        s.execute(update(SourcePoll).where(SourcePoll.source == SOURCE).values(lease_token=None, lease_until=None))
