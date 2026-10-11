"""Unofficial weekly index membership from public community-maintained lists.

Each list is updated once a week at a fixed time (config/refresh.toml,
``[index_constituents]``, US Eastern; Saturday 08:00 by default) or when the
reader asks from the data page. A failed attempt is tried again after
``retry_seconds``; failed downloads never replace the last usable membership list.

The S&P 500 list keeps each member's GICS sector and sub-industry; the Nasdaq-100
table keeps Wikipedia's ICB industry and subsector exactly as given.

Downloads are separate from publication: only a caller holding the worker's
lease fence may call the persistence functions, in its existing transaction.
"""

import csv
import io
import re
import uuid
from datetime import datetime, time, timedelta
from html.parser import HTMLParser

import httpx
from sqlalchemy import delete, func, select

from iirp.analysis.calendar import ET
from iirp.config import refresh
from iirp.messages import UserError, decode, msg
from iirp.models import (
    ACTIVE,
    IndexConstituent,
    IndexConstituentState,
    Issuer,
    Job,
    Subscription,
    now,
)

SOURCES = {
    "sp500": "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv",
    # The Nasdaq-100 article links to this current component table.
    "nasdaq100": "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies",
}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MINIMUM_COUNTS = {"sp500": 450, "nasdaq100": 90}
MAXIMUM_COUNTS = {"sp500": 550, "nasdaq100": 120}
_TICKER = re.compile(r"[A-Z][A-Z0-9.\-]{0,15}\Z")


def _cell(value):
    return " ".join(re.sub(r"\[[^\]]*\]", "", value).split())


def _constituent(index_name, ticker, name, industry, cik=None, *, sub_industry=None, classification=None):
    ticker, name, industry = _cell(ticker).upper(), _cell(name), _cell(industry)
    sub_industry = _cell(sub_industry) if sub_industry else None
    if not _TICKER.fullmatch(ticker) or not name:
        raise UserError("index.parse_failed", index=index_name, reason="invalid_member")
    if cik:
        cik = _cell(str(cik))
        if not re.fullmatch(r"\d{1,10}", cik) or int(cik) == 0:
            raise UserError("index.parse_failed", index=index_name, reason="invalid_cik")
        cik = cik.zfill(10)
    return {"index_name": index_name, "ticker": ticker, "cik": cik or None,
            "name": name, "industry": industry or None, "sub_industry": sub_industry or None,
            "classification": classification}


def _unique(rows, index_name):
    if not rows or len({row["ticker"] for row in rows}) != len(rows):
        raise UserError("index.parse_failed", index=index_name, reason="empty_or_duplicate")
    return rows


def parse_sp500(text):
    """Parse the datasets CSV (GICS sector and sub-industry); its CIK is authoritative for this source."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    required = {"Symbol", "Security", "GICS Sector", "CIK"}
    if not required.issubset(reader.fieldnames or []):
        raise UserError("index.parse_failed", index="sp500", reason="missing_columns")
    rows = []
    for row in reader:
        if any(row.get(key) is None for key in required) or not row["CIK"].strip():
            raise UserError("index.parse_failed", index="sp500", reason="missing_value")
        rows.append(_constituent("sp500", row["Symbol"], row["Security"], row["GICS Sector"], row["CIK"],
                                 sub_industry=row.get("GICS Sub-Industry"), classification="GICS"))
    return _unique(rows, "sp500")


class _Tables(HTMLParser):
    """Extract table cells without HTML markup or superscript citation numbers."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.depth = 0
        self.table = self.row = self.parts = None
        self.suppressed = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.depth += 1
            if self.depth == 1:
                self.table = []
        if self.depth != 1:
            return
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.parts = []
        elif tag == "sup":
            self.suppressed += 1
        elif tag == "br" and self.parts is not None:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag == "table":
            if self.depth == 1:
                self.tables.append(self.table)
                self.table = self.row = self.parts = None
            self.depth -= 1
            return
        if self.depth != 1:
            return
        if tag == "sup":
            self.suppressed = max(0, self.suppressed - 1)
        elif tag in {"td", "th"} and self.parts is not None:
            self.row.append(_cell("".join(self.parts)))
            self.parts = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.table.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.depth == 1 and self.parts is not None and not self.suppressed:
            self.parts.append(data)


def parse_nasdaq100(text):
    """Find the component table by named columns, never by table position."""
    parser = _Tables()
    parser.feed(text)
    for table in parser.tables:
        if not table:
            continue
        headers = [value.casefold() for value in table[0]]
        ticker = next((headers.index(key) for key in ("ticker", "symbol") if key in headers), None)
        company = next((headers.index(key) for key in ("company", "security") if key in headers), None)
        industry = next((headers.index(key) for key in ("icb industry", "gics sector", "gics industry", "industry")
                         if key in headers), None)
        if ticker is None or company is None or industry is None:
            continue
        sub = next((headers.index(key) for key in ("icb subsector", "gics sub-industry") if key in headers), None)
        scheme = ("ICB" if headers[industry].startswith("icb") else
                  "GICS" if headers[industry].startswith("gics") else None)
        cik = headers.index("cik") if "cik" in headers else None
        rows = []
        for row in table[1:]:
            if len(row) != len(headers):
                raise UserError("index.parse_failed", index="nasdaq100", reason="incomplete_row")
            rows.append(_constituent("nasdaq100", row[ticker], row[company], row[industry],
                                     row[cik] if cik is not None else None,
                                     sub_industry=row[sub] if sub is not None else None, classification=scheme))
        return _unique(rows, "nasdaq100")
    raise UserError("index.parse_failed", index="nasdaq100", reason="missing_table")


def fetch_index(index_name):
    """Download one bounded response, validate completeness, and return plain rows."""
    if index_name not in SOURCES:
        raise UserError("index.unknown", index=index_name)
    try:
        with httpx.Client(timeout=12, follow_redirects=True, headers={
                "User-Agent": "IIRP/1.0 (https://github.com/565740807/IIRP)"}) as client:
            with client.stream("GET", SOURCES[index_name]) as response:
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 5_000_000:
                        raise UserError("index.parse_failed", index=index_name, reason="response_too_large")
                    chunks.append(chunk)
                content = b"".join(chunks).decode("utf-8-sig")
    except httpx.HTTPError as exc:
        reason = (f"http_{exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError)
                  else type(exc).__name__)
        raise UserError("index.download_failed", index=index_name, reason=reason) from exc
    except UnicodeDecodeError as exc:
        raise UserError("index.parse_failed", index=index_name, reason="invalid_encoding") from exc
    rows = parse_sp500(content) if index_name == "sp500" else parse_nasdaq100(content)
    if not MINIMUM_COUNTS[index_name] <= len(rows) <= MAXIMUM_COUNTS[index_name]:
        raise UserError("index.parse_failed", index=index_name, reason="unexpected_count", count=len(rows))
    return rows


def schedule():
    """The fixed weekly update time from config/refresh.toml: (weekday 0-6, time, retry)."""
    config = refresh()["index_constituents"]
    weekday = WEEKDAYS.index(config["weekday"].lower())
    hour, minute = (int(part) for part in config["time"].split(":"))
    return weekday, time(hour, minute), timedelta(seconds=config["retry_seconds"])


def last_slot(at):
    """The most recent scheduled update time at or before ``at`` (US Eastern)."""
    weekday, clock, _ = schedule()
    local = at.astimezone(ET)
    day = local.date() - timedelta(days=(local.weekday() - weekday) % 7)
    slot = datetime.combine(day, clock, tzinfo=ET)
    return slot if slot <= local else slot - timedelta(days=7)


def next_slot(at):
    """The next scheduled update time after ``at``."""
    return last_slot(at) + timedelta(days=7)


def _retry_at(state):
    """When a failed attempt (newer than the last success) may be tried again; None otherwise."""
    if state is None or state.attempted_at is None:
        return None
    if state.updated_at is not None and state.attempted_at <= state.updated_at:
        return None
    return state.attempted_at + schedule()[2]


def due_indices(s, *, at=None):
    """Lists without a success since the latest scheduled time; a failure waits for its retry.

    A failed attempt keeps the previous list and its update date intact.
    """
    at = at or now()
    slot = last_slot(at)
    states = {state.index_name: state for state in s.scalars(select(IndexConstituentState))}
    due = []
    for index in SOURCES:
        state = states.get(index)
        if state is not None and state.updated_at is not None and state.updated_at >= slot:
            continue
        retry = _retry_at(state)
        if retry is not None and at < retry:
            continue
        due.append(index)
    return due


def enqueue(s, indices, *, trigger="automatic"):
    """One durable job per index unless one is already active; returns the active job ids."""
    from iirp.market.yahoo import digest

    identifiers = []
    for index in indices:
        active = s.scalar(select(Job.id).where(Job.kind == "index_constituents", Job.status.in_(ACTIVE),
                                               Job.target["index_name"].astext == index).limit(1))
        if active:
            identifiers.append(active)
            continue
        target = {"index_name": index, "week": now().strftime("%G-W%V")}
        job = Job(kind="index_constituents", title=msg("job.title.index_constituents", index=index),
                  target=target, idempotency_key=digest(["index_constituents", target, str(uuid.uuid4())]),
                  trigger=trigger, priority=50 if trigger == "automatic" else -10, progress_total=1)
        s.add(job)
        s.flush()
        s.add(Subscription(job_id=job.id, source="automatic" if trigger == "automatic" else "manual"))
        identifiers.append(job.id)
    return identifiers


def status(s, *, at=None):
    """Each list's size, last update, last failure and whether an update runs; the schedule."""
    at = at or now()
    weekday, clock, retry = schedule()
    states = {state.index_name: state for state in s.scalars(select(IndexConstituentState))}
    counts = dict(s.execute(select(IndexConstituent.index_name, func.count()).group_by(IndexConstituent.index_name)).all())
    running = set(s.scalars(select(Job.target["index_name"].astext).where(
        Job.kind == "index_constituents", Job.status.in_(ACTIVE))))
    due = set(due_indices(s, at=at))
    slot = last_slot(at)
    items = []
    for index in SOURCES:
        state = states.get(index)
        error = state.error if state else None
        retry_at = _retry_at(state)
        behind = state is None or state.updated_at is None or state.updated_at < slot
        items.append({
            "index_name": index,
            "members": counts.get(index, 0),
            "updated_at": state.updated_at if state else None,
            "attempted_at": state.attempted_at if state else None,
            "error": msg(error["code"], **error.get("params", {})) if error else None,
            "running": index in running,
            # Now when due, the retry time after a failure this week, else the next weekly time.
            "next_run_at": at if index in due else retry_at if behind and retry_at else next_slot(at),
        })
    return {"items": items, "weekday": WEEKDAYS[weekday], "time": clock.strftime("%H:%M"),
            "timezone": "America/New_York", "retry_seconds": int(retry.total_seconds()),
            "next_scheduled_at": next_slot(at)}


def persist_success(s, index_name, rows, *, at=None):
    """Atomically replace one index inside the caller's fenced transaction."""
    if index_name not in SOURCES:
        raise UserError("index.unknown", index=index_name)
    if any(row["index_name"] != index_name for row in rows):
        raise UserError("index.parse_failed", index=index_name, reason="mixed_indices")
    _unique(rows, index_name)
    at = at or now()
    # Wikipedia has no CIK column. Resolve from the local issuer or another
    # downloaded index, preserving absence if identity is not known.
    ciks = {ticker.upper(): cik for ticker, cik in s.execute(
        select(IndexConstituent.ticker, IndexConstituent.cik).where(IndexConstituent.cik.is_not(None)))}
    ciks.update({ticker.upper(): cik for ticker, cik in s.execute(
        select(Issuer.ticker, Issuer.id).where(Issuer.ticker.is_not(None)))})
    s.execute(delete(IndexConstituent).where(IndexConstituent.index_name == index_name))
    s.add_all(IndexConstituent(**dict(row, cik=row["cik"] or ciks.get(row["ticker"])), updated_at=at)
              for row in rows)
    state = s.get(IndexConstituentState, index_name)
    if state is None:
        state = IndexConstituentState(index_name=index_name)
        s.add(state)
    state.updated_at = state.attempted_at = at
    state.error = None


def persist_failure(s, index_name, error, *, at=None):
    """Record a translatable failure without touching membership or update date."""
    if index_name not in SOURCES:
        raise UserError("index.unknown", index=index_name)
    state = s.get(IndexConstituentState, index_name)
    if state is None:
        state = IndexConstituentState(index_name=index_name)
        s.add(state)
    state.attempted_at = at or now()
    state.error = decode(str(error)) or {"code": "index.download_failed", "params": {
        "index": index_name, "reason": type(error).__name__}}
