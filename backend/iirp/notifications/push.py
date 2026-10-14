"""Phone pushes through an ntfy server, sent by the worker's ``notification_push`` job.

Waiting notifications go oldest first. More than five from the same minute go as one
digest; a test waits in ``notification_push`` and goes first. Messages are at least five
seconds apart and at most 200 go out in 24 hours (ntfy.sh lets an anonymous sender
publish one message every five seconds after a short burst, and a few hundred a day). A
refusal for too many requests (429) waits as long as the server asks (a minute when it does
not say) and is not a failure. Any other failure waits 30 s, 1, 2, 4 and 8 minutes; after
the sixth failure in a row that message is marked failed and the next one is tried. The
request goes out outside any database transaction; every result is written under the
job's lease (``jobs.queue.fenced``).
"""

import logging
import time
from datetime import timedelta

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from iirp.db import session
from iirp.messages import UserError, msg
from iirp.models import Job, now
from iirp.models.base import ACTIVE
from iirp.models.notifications import Notification, NotificationPush
from iirp.notifications import settings as notification_settings
from iirp.notifications import texts

KIND = "notification_push"
MIN_INTERVAL = 5
DAILY_LIMIT = 200
DIGEST_OVER = 5
MAX_FAILURES = 6
FIRST_WAIT = 30
MAX_WAIT = 3600
TIMEOUT = 15


def http_transport(server, message):
    """Publish one JSON message to the server root; returns (status code, Retry-After seconds or None)."""
    response = httpx.post(server, json=message, timeout=TIMEOUT, headers={"User-Agent": "IIRP"})
    retry = response.headers.get("Retry-After")
    return response.status_code, int(retry) if retry and retry.isdigit() else None


def plan(s):
    """The next messages: waiting tests, then waiting notifications by minute (a digest when
    more than five share one)."""
    messages = [{"kind": "test", "push_id": row.id, "ids": []} for row in s.scalars(
        select(NotificationPush).where(NotificationPush.kind == "test", NotificationPush.status == "pending")
        .order_by(NotificationPush.created_at, NotificationPush.id))]
    minutes = {}
    for row in s.scalars(select(Notification).where(Notification.push_state == "pending")
                         .order_by(Notification.created_at, Notification.id)):
        minutes.setdefault(row.created_at.replace(second=0, microsecond=0), []).append(row)
    for rows in minutes.values():
        if len(rows) > DIGEST_OVER:
            messages.append({"kind": "digest", "push_id": None, "ids": [row.id for row in rows], "rows": rows})
        else:
            messages.extend({"kind": "single", "push_id": None, "ids": [row.id], "rows": [row]} for row in rows)
    return messages


def _message(item, ntfy):
    topic, language = ntfy["topic"], ntfy["language"]
    if item["kind"] == "test":
        return texts.test(topic, language)
    if item["kind"] == "digest":
        return texts.digest(item["rows"], topic, language)
    return texts.payload(item["rows"][0], topic, language)


def _record(item, *, status, attempts, error=None):
    """Write one message's result: the push row and the notifications it carried."""
    def write(s, _job):
        stamp = now()
        if item["push_id"]:
            row = s.get(NotificationPush, item["push_id"])
            row.status, row.attempts, row.error = status, attempts, error
            row.sent_at = stamp if status == "sent" else None
        else:
            s.add(NotificationPush(kind=item["kind"], status=status, count=len(item["ids"]), attempts=attempts,
                                   error=error, created_at=stamp, sent_at=stamp if status == "sent" else None))
        if item["ids"]:
            s.execute(update(Notification).where(Notification.id.in_(item["ids"]))
                      .values(push_state=status))
    return write


def wanted(s):
    """Whether anything waits for the phone."""
    return bool(s.scalar(select(Notification.id).where(Notification.push_state == "pending").limit(1))
                or s.scalar(select(NotificationPush.id).where(NotificationPush.status == "pending").limit(1)))


def request(s):
    """Queue the push job unless one is already queued or running (one job serves every waiting message)."""
    from iirp.jobs.queue import enqueue

    if s.scalar(select(Job.id).where(Job.kind == KIND, Job.status.in_(ACTIVE)).limit(1)):
        return
    try:
        enqueue(s, KIND, {}, "automatic")
    except UserError:
        logging.warning("notification push not queued: the queue is full")


def queue_test(s):
    settings = notification_settings.load(s)
    if not settings["ntfy"]["enabled"] or not settings["ntfy"]["topic"]:
        raise UserError("notifications.push_off")
    s.add(NotificationPush(kind="test", status="pending", count=0, attempts=0, created_at=now()))
    s.flush()
    request(s)


def run(job, stopping, *, transport=http_transport, sleep=time.sleep):
    """Send waiting messages until none are left, a wait is due, or the worker stops."""
    from iirp.jobs.queue import fenced

    failures = int((job.checkpoint or {}).get("failures", 0))
    sent = 0
    while not stopping():
        with session() as s:
            ntfy = notification_settings.load(s)["ntfy"]
            items = plan(s)
            day = s.scalar(select(func.count()).select_from(NotificationPush).where(
                NotificationPush.status == "sent", NotificationPush.sent_at >= now() - timedelta(hours=24)))
            last = s.scalar(select(func.max(NotificationPush.sent_at)))
        if not items:
            fenced(job, status="SUCCEEDED", done=1, checkpoint={}, result={"sent": sent})
            return
        if not ntfy["enabled"] or not ntfy["topic"]:
            # Pushes were turned off while messages waited: they stay in the app only.
            def drop(s, _job):
                s.execute(update(Notification).where(Notification.push_state == "pending").values(push_state=None))
                s.execute(update(NotificationPush).where(NotificationPush.status == "pending")
                          .values(status="failed", error=msg("notifications.push_off")))
            fenced(job, business_write=drop, status="SUCCEEDED", done=1, checkpoint={}, result={"sent": sent})
            return
        if day >= DAILY_LIMIT:
            fenced(job, status="RETRY_WAIT", error=msg("notifications.daily_limit", limit=DAILY_LIMIT),
                   retry_seconds=3600, checkpoint={"failures": failures})
            return
        if last is not None:
            wait = MIN_INTERVAL - (now() - last).total_seconds()
            if wait > 0:
                sleep(wait)
                if not fenced(job):
                    return
        item = items[0]
        try:
            status, retry_after = transport(ntfy["server"], _message(item, ntfy))
            error = None if 200 <= status < 300 else msg("notifications.push_status", status=status)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            status, retry_after, error = None, None, msg("notifications.push_error", reason=type(exc).__name__)
        if error is None:
            if not fenced(job, business_write=_record(item, status="sent", attempts=failures + 1),
                          checkpoint={"failures": 0}):
                return
            failures, sent = 0, sent + 1
            continue
        if status == 429:
            fenced(job, status="RETRY_WAIT", error=error, retry_seconds=retry_after or 60,
                   checkpoint={"failures": failures})
            return
        failures += 1
        if failures >= MAX_FAILURES:
            if not fenced(job, business_write=_record(item, status="failed", attempts=failures, error=error),
                          checkpoint={"failures": 0}):
                return
            failures = 0
            continue
        fenced(job, status="RETRY_WAIT", error=error, checkpoint={"failures": failures},
               retry_seconds=min(FIRST_WAIT * 2 ** (failures - 1), MAX_WAIT))
        return


def status(s):
    """The last message's outcome and how many went out in the last 24 hours, for the settings."""
    last = s.scalar(select(NotificationPush).where(NotificationPush.status != "pending")
                    .order_by(NotificationPush.created_at.desc()).limit(1))
    day = s.scalar(select(func.count()).select_from(NotificationPush).where(
        NotificationPush.status == "sent", NotificationPush.sent_at >= now() - timedelta(hours=24)))
    waiting = (s.scalar(select(func.count()).select_from(Notification).where(Notification.push_state == "pending"))
               + s.scalar(select(func.count()).select_from(NotificationPush).where(NotificationPush.status == "pending")))
    return {"last_status": last.status if last else None, "last_kind": last.kind if last else None,
            "last_at": (last.sent_at or last.created_at) if last else None, "last_error": last.error if last else None,
            "sent_today": day, "daily_limit": DAILY_LIMIT, "waiting": waiting}


def tick():
    """Queue the push job when messages wait; called by the worker after each scan."""
    try:
        with session() as s, s.begin():
            if wanted(s):
                request(s)
    except SQLAlchemyError:
        logging.exception("notification push request failed")
