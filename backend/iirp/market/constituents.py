"""Unofficial weekly index membership from the sources selected in D25.

Downloads are separate from publication: only a caller holding the worker's
lease fence may call the persistence functions, in its existing transaction.
Failed downloads never replace the last usable membership list.
"""

import csv
import io
import re
from datetime import timedelta
from html.parser import HTMLParser

import httpx
from sqlalchemy import delete, select

from iirp.messages import UserError, decode
from iirp.models import IndexConstituent, IndexConstituentState, Issuer, now

SOURCES = {
    "sp500": "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv",
    # The Nasdaq-100 article links to this current component table.
    "nasdaq100": "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies",
}
REFRESH_INTERVAL = timedelta(days=7)
MINIMUM_COUNTS = {"sp500": 450, "nasdaq100": 90}
MAXIMUM_COUNTS = {"sp500": 550, "nasdaq100": 120}
_TICKER = re.compile(r"[A-Z][A-Z0-9.\-]{0,15}\Z")


def _cell(value):
    return " ".join(re.sub(r"\[[^\]]*\]", "", value).split())


def _constituent(index_name, ticker, name, industry, cik=None):
    ticker, name, industry = _cell(ticker).upper(), _cell(name), _cell(industry)
    if not _TICKER.fullmatch(ticker) or not name:
        raise UserError("index.parse_failed", index=index_name, reason="invalid_member")
    if cik:
        cik = _cell(str(cik))
        if not re.fullmatch(r"\d{1,10}", cik) or int(cik) == 0:
            raise UserError("index.parse_failed", index=index_name, reason="invalid_cik")
        cik = cik.zfill(10)
    return {"index_name": index_name, "ticker": ticker, "cik": cik or None,
            "name": name, "industry": industry or None}


def _unique(rows, index_name):
    if not rows or len({row["ticker"] for row in rows}) != len(rows):
        raise UserError("index.parse_failed", index=index_name, reason="empty_or_duplicate")
    return rows


def parse_sp500(text):
    """Parse the datasets CSV; its CIK is authoritative for this source."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    required = {"Symbol", "Security", "GICS Sector", "CIK"}
    if not required.issubset(reader.fieldnames or []):
        raise UserError("index.parse_failed", index="sp500", reason="missing_columns")
    rows = []
    for row in reader:
        if any(row.get(key) is None for key in required) or not row["CIK"].strip():
            raise UserError("index.parse_failed", index="sp500", reason="missing_value")
        rows.append(_constituent("sp500", row["Symbol"], row["Security"],
                                 row["GICS Sector"], row["CIK"]))
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
        cik = headers.index("cik") if "cik" in headers else None
        rows = []
        for row in table[1:]:
            if len(row) != len(headers):
                raise UserError("index.parse_failed", index="nasdaq100", reason="incomplete_row")
            rows.append(_constituent("nasdaq100", row[ticker], row[company], row[industry],
                                     row[cik] if cik is not None else None))
        return _unique(rows, "nasdaq100")
    raise UserError("index.parse_failed", index="nasdaq100", reason="missing_table")


def fetch_index(index_name):
    """Download one bounded response, validate completeness, and return plain rows."""
    if index_name not in SOURCES:
        raise UserError("index.unknown", index=index_name)
    try:
        with httpx.Client(timeout=12, follow_redirects=True, headers={
                "User-Agent": "IIRP2/1.0 (https://github.com/565740807/iirp2)"}) as client:
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


def due_indices(s, *, at=None):
    """Weekly attempts; a failed attempt keeps the previous update date intact."""
    at = at or now()
    states = {state.index_name: state for state in s.scalars(select(IndexConstituentState))}
    return [index for index in SOURCES if index not in states
            or states[index].attempted_at is None
            or at - states[index].attempted_at >= REFRESH_INTERVAL]


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
