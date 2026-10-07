"""Run one SEC operation (latest, indexes, document) through the caller's rate-limited fetch."""

import json
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from hashlib import sha256
from urllib.parse import parse_qs, urlencode, urlsplit

from iirp.sec.parse import (
    ENTITY_SEARCH_URL,
    LATEST_URL,
    SEC_BASE,
    TICKERS_URL,
    SecSourceError,
    _accession,
    _atom,
    _bounded,
    _cik,
    _date,
    _decode,
    _encoded,
    _filing_urls,
    _form,
    _instant,
    _local,
    _merge_entries,
    _text,
    _xml,
    parse_company_tickers,
    parse_entity_search,
    parse_filing_index,
    parse_index,
    parse_submission,
    parse_submissions,
    validate_sec_url,
)

Fetch = Callable[[str], bytes]


class _Transport:
    def __init__(self, fetch: Fetch):
        self.fetch = fetch
        self.documents: list[dict] = []

    def get(self, url: str, media_type: str) -> bytes:
        url = validate_sec_url(url)
        payload = _bounded(self.fetch(url))
        encoding, text = _encoded(payload)
        self.documents.append(
            {
                "url": url,
                "media_type": media_type,
                "encoding": encoding,
                "payload": text,
                "sha256": sha256(payload).hexdigest(),
            }
        )
        return payload


def _status(exc: Exception) -> int | None:
    return getattr(exc, "status_code", getattr(getattr(exc, "response", None), "status_code", None))


def _result(
    transport: _Transport,
    *,
    entries=None,
    filing=None,
    complete=False,
    reason: str,
    cursor=None,
    **scan,
) -> dict:
    result = {
        "source_documents": transport.documents,
        "scan": {"complete": complete, "reason": reason, **scan},
        "cursor": cursor,
    }
    if entries is not None:
        result["entries"] = _merge_entries(entries)
    if filing is not None:
        result["filing"] = filing
    return result


def _positive_int(value, name: str, upper: int) -> int:
    if type(value) is not int or not 1 <= value <= upper:
        raise SecSourceError(f"invalid_{name}")
    return value


def _latest(target: dict, transport: _Transport) -> dict:
    count = _positive_int(target.get("page_size", 100), "page_size", 100)
    budget = _positive_int(target.get("max_pages", 1), "max_pages", 100)
    cursor = dict(target.get("cursor") or {})
    start = cursor.get("start", 0)
    if type(start) is not int or start < 0:
        raise SecSourceError("invalid_feed_start")
    watermark = _instant(target.get("watermark"))
    if target.get("watermark") and not watermark:
        raise SecSourceError("invalid_continuous_watermark")
    seen = list(cursor.get("page_fingerprints", []))
    entries, urls, raw_total = [], [], 0
    oldest, newest = cursor.get("oldest_accepted_at"), cursor.get("newest_accepted_at")
    timing_gap = cursor.get("timing_gap", False)
    source_next = cursor.get("next_url")

    def finish(reason, complete=False, next_cursor=None):
        return _result(
            transport,
            entries=entries,
            complete=complete,
            reason=reason,
            cursor=next_cursor,
            mode="latest",
            coverage_scope="feed_acceptance_scan",
            oldest_accepted_at=oldest,
            newest_accepted_at=newest,
            watermark=watermark,
            raw_entry_count=raw_total,
            pages_scanned=len(urls),
            timing_gap=timing_gap,
        )

    for _ in range(budget):
        url = source_next or LATEST_URL + "?" + urlencode(
            {
                "action": "getcurrent",
                "owner": "only",
                "count": count,
                "start": start,
                "output": "atom",
            }
        )
        url = validate_sec_url(url)
        query = parse_qs(urlsplit(url).query)
        if (
            urlsplit(url).path != "/cgi-bin/browse-edgar"
            or query.get("action") != ["getcurrent"]
            or query.get("output") != ["atom"]
            or query.get("owner") not in (["only"], ["include"])
        ):
            raise SecSourceError("invalid_feed_page_url")
        try:
            payload = transport.get(url, "application/atom+xml")
            page, raw_count, next_url = _atom(payload)
        except SecSourceError as exc:
            return finish(str(exc), next_cursor={**cursor, "start": start})
        fingerprint = (
            sha256(
                json.dumps(
                    [[entry["accession"], entry["raw_titles"]] for entry in page], sort_keys=True
                ).encode()
            ).hexdigest()
            if page
            else sha256(payload).hexdigest()
        )
        if fingerprint in seen or url in urls:
            return finish("repeated_page", next_cursor={**cursor, "start": start})
        seen.append(fingerprint)
        urls.append(url)
        raw_total += raw_count
        times = [entry["accepted_at"] for entry in page if entry["accepted_at"]]
        ordered = [datetime.fromisoformat(value) for value in times]
        timing_gap = (
            timing_gap or len(times) != len(page) or ordered != sorted(ordered, reverse=True)
        )
        if times:
            values = [value for value in [oldest, newest, *times] if value]
            oldest = min(values, key=datetime.fromisoformat)
            newest = max(values, key=datetime.fromisoformat)
        entries.extend(page)
        if (
            watermark
            and times
            and min(ordered) <= datetime.fromisoformat(watermark)
            and not timing_gap
        ):
            return finish("watermark_reached", complete=True)
        if raw_count < count and not next_url:
            return finish("feed_exhausted_before_watermark")
        new_start = start + raw_count
        if next_url:
            next_query = parse_qs(urlsplit(next_url).query)
            next_start = next_query.get("start", [""])[0]
            if not next_start.isdigit() or int(next_start) <= start:
                return finish("non_advancing_page", next_cursor={**cursor, "start": start})
            new_start = int(next_start)
        if new_start <= start:
            return finish("non_advancing_page", next_cursor={**cursor, "start": start})
        start, source_next = new_start, next_url
        cursor = {
            "start": start,
            "next_url": source_next,
            "page_fingerprints": seen,
            "oldest_accepted_at": oldest,
            "newest_accepted_at": newest,
            "timing_gap": timing_gap,
        }
    return finish("page_budget", next_cursor=cursor)


def _quarter(value: date) -> tuple[int, int]:
    return value.year, (value.month - 1) // 3 + 1


def _index_as_of(payload: bytes) -> date | None:
    header = _decode(payload).split("CIK|", 1)[0]
    match = re.search(r"Last Data Received:\s*([^\r\n]+)", header, re.I)
    if not match:
        return None
    value = match.group(1).strip()
    for pattern in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            pass
    return None


def _required_index_day(end: date) -> date:
    from pandas.tseries.holiday import USFederalHolidayCalendar

    holidays = {
        value.date()
        for value in USFederalHolidayCalendar().holidays(start=end - timedelta(days=30), end=end)
    }
    while end.weekday() >= 5 or end in holidays:
        end -= timedelta(days=1)
    return end


def _indexes(target: dict, transport: _Transport, mode: str) -> dict:
    start_raw, end_raw = _date(target.get("start_date")), _date(target.get("end_date"))
    if not start_raw or not end_raw or start_raw > end_raw:
        raise SecSourceError("invalid_index_date_range")
    start, end = date.fromisoformat(start_raw), date.fromisoformat(end_raw)
    budget = _positive_int(target.get("max_pages", 1), "max_pages", 100)
    cursor = dict(target.get("cursor") or {})
    entries, scanned = [], list(cursor.get("scanned_indexes", []))
    stale = [
        item
        for item in scanned
        if not item.get("as_of") or item["as_of"] < item.get("required_through", "")
    ]
    # Explicit SEC business dates may be supplied by the scheduler. Do not infer
    # that NYSE holidays are SEC holidays, or call an absent daily index empty.
    explicit = target.get("index_dates")
    if explicit is not None:
        if mode != "daily" or not isinstance(explicit, list) or any(not _date(x) for x in explicit):
            raise SecSourceError("invalid_explicit_index_dates")
        dates = sorted({x for x in explicit if start_raw <= x <= end_raw})
    else:
        dates = None
    if mode == "daily":
        current_raw = _date(cursor.get("date", start_raw))
        if not current_raw or not start_raw <= current_raw <= end_raw:
            raise SecSourceError("daily_cursor_outside_range")
        current = date.fromisoformat(current_raw)
        if dates is not None:
            pending = [date.fromisoformat(value) for value in dates if value >= current_raw]
        else:
            pending = []
            while current <= end:
                if current.weekday() < 5:
                    pending.append(current)
                current += timedelta(days=1)
        items = []
        for day in pending:
            year, quarter = _quarter(day)
            items.append(
                (
                    day.isoformat(),
                    f"{SEC_BASE}/Archives/edgar/daily-index/{year}/QTR{quarter}/master.{day:%Y%m%d}.idx",
                )
            )
        cursor_key = "date"
    else:
        first, last = _quarter(start), _quarter(end)
        if "quarter" in cursor:
            match = re.fullmatch(r"(\d{4})-Q([1-4])", cursor["quarter"])
            if not match:
                raise SecSourceError("invalid_quarter_cursor")
            first = int(match.group(1)), int(match.group(2))
            if not _quarter(start) <= first <= last:
                raise SecSourceError("quarter_cursor_outside_range")
        items = []
        while first <= last:
            year, quarter = first
            items.append(
                (
                    f"{year}-Q{quarter}",
                    f"{SEC_BASE}/Archives/edgar/full-index/{year}/QTR{quarter}/master.idx",
                )
            )
            first = (year + 1, 1) if quarter == 4 else (year, quarter + 1)
        cursor_key = "quarter"

    def finish(reason, complete=False, next_cursor=None):
        if next_cursor is not None:
            next_cursor = {**next_cursor, "scanned_indexes": scanned}
        return _result(
            transport,
            entries=entries,
            complete=complete,
            reason=reason,
            cursor=next_cursor,
            mode=mode,
            coverage_scope="index_filing_dates",
            start_date=start_raw,
            end_date=end_raw,
            scanned_indexes=scanned,
        )

    for offset, (key, url) in enumerate(items):
        if offset >= budget:
            return finish("page_budget", next_cursor={cursor_key: key})
        try:
            payload = transport.get(url, "text/plain")
        except Exception as exc:
            if _status(exc) not in {404, 410}:
                raise
            return finish(
                "index_not_ready" if _status(exc) == 404 else "index_unavailable",
                next_cursor={cursor_key: key},
            )
        try:
            page = parse_index(payload)
        except SecSourceError as exc:
            return finish(str(exc), next_cursor={cursor_key: key})
        entries.extend(entry for entry in page if start_raw <= entry["filing_date"] <= end_raw)
        as_of = _index_as_of(payload)
        if mode == "daily":
            expected_through = date.fromisoformat(key)
        else:
            year, quarter = map(int, key.replace("-Q", " ").split())
            following = date(year + 1, 1, 1) if quarter == 4 else date(year, quarter * 3 + 1, 1)
            expected_through = _required_index_day(min(end, following - timedelta(days=1)))
        if as_of is None or as_of < expected_through:
            stale.append(key)
        scanned.append(
            {
                cursor_key: key,
                "url": url,
                "sha256": sha256(payload).hexdigest(),
                "ownership_count": len(page),
                "as_of": as_of.isoformat() if as_of else None,
                "required_through": expected_through.isoformat(),
            }
        )
    if stale:
        return finish(
            "index_not_current_to_end"
            if any(item["as_of"] for item in scanned)
            else "index_as_of_unknown"
        )
    return finish("requested_indexes_scanned", complete=True)


def _document(target: dict, transport: _Transport) -> dict:
    source_url = target.get("submission_url") or target.get("index_url")
    if not isinstance(source_url, str):
        raise SecSourceError("filing_source_url_required")
    accession = target.get("accession") or _accession(source_url)
    if _accession(accession) != accession:
        raise SecSourceError("invalid_filing_accession")
    urls = _filing_urls(source_url, accession)
    index = None
    submission_url = target.get("submission_url")
    try:
        if not submission_url:
            index_url = target.get("index_url") or urls["index_url"]
            payload = transport.get(index_url, "text/html")
            index = parse_filing_index(payload, index_url)
            submission_url = index["submission_url"]
        payload = transport.get(submission_url, "text/plain")
        filing = parse_submission(payload, source_url=submission_url, accession=accession)
    except Exception as exc:
        if _status(exc) in {404, 410}:
            # A missing full submission can still leave verified primary XML and
            # its actual Accepted field available through the filing index.
            if index is None and target.get("index_url") and _status(exc) == 404:
                try:
                    index_url = validate_sec_url(target["index_url"])
                    index = parse_filing_index(transport.get(index_url, "text/html"), index_url)
                except Exception as index_exc:
                    if _status(index_exc) not in {404, 410} and not isinstance(
                        index_exc, SecSourceError
                    ):
                        raise
            if index and index["document_urls"] and _status(exc) == 404:
                return _document_from_index(index, transport, target)
            return _result(
                transport, complete=False, reason="filing_unavailable", accession=accession
            )
        if isinstance(exc, SecSourceError):
            return _result(transport, complete=False, reason=str(exc), accession=accession)
        raise
    previous = (index or {}).get("accepted_at") or _instant(target.get("accepted_at"))
    if (
        previous
        and filing["accepted_at"]
        and datetime.fromisoformat(previous) != datetime.fromisoformat(filing["accepted_at"])
    ):
        # Header is preserved, but neither contradictory instant becomes an
        # eligible exact-reaction baseline until this discrepancy is reviewed.
        filing["acceptance_observations"] = [previous, filing["accepted_at"]]
        filing["accepted_at"] = None
        filing["accepted_at_source"] = None
        filing["warnings"].append("acceptance_time_conflict")
    return _result(
        transport,
        filing=filing,
        complete=True,
        reason="ownership_document_validated",
        acceptance_time_complete=filing["accepted_at"] is not None,
    )


def _document_from_index(index: dict, transport: _Transport, target: dict) -> dict:
    if len(index["document_urls"]) != 1:
        return _result(
            transport,
            complete=False,
            reason="ownership_document_ambiguous",
            accession=index["accession"],
        )
    document_url = index["document_urls"][0]
    try:
        xml = transport.get(document_url, "application/xml")
        root = _xml(xml)
        form = _form(_text(root, "documentType"))
        if (
            _local(root) != "ownershipDocument"
            or not form
            or (target.get("form") and _form(target["form"]) != form)
        ):
            raise SecSourceError("unsupported_ownership_document")
    except Exception as exc:
        if _status(exc) in {404, 410} or isinstance(exc, SecSourceError):
            return _result(
                transport,
                complete=False,
                reason="ownership_document_unavailable_or_invalid",
                accession=index["accession"],
            )
        raise
    encoding, payload = _encoded(xml)
    filing = {
        "accession": index["accession"],
        "form": form,
        "accepted_at": index["accepted_at"],
        "accepted_at_source": index["accepted_at_source"],
        "filing_date": index["filing_date"],
        "document_url": document_url,
        "xml_payload": payload,
        "xml_encoding": encoding,
        "xml_sha256": sha256(xml).hexdigest(),
        "warnings": ["full_submission_unavailable"],
    }
    if not filing["accepted_at"]:
        filing["warnings"].append("acceptance_time_missing_or_invalid")
    return _result(
        transport,
        filing=filing,
        complete=True,
        reason="ownership_document_validated",
        acceptance_time_complete=filing["accepted_at"] is not None,
    )


# Older submission files followed for one entity; each holds about 1,000 filings.
ENTITY_OLDER_FILES = 3


def _entity(target: dict, transport: _Transport) -> dict:
    """Ownership filings of one CIK (issuer or reporting owner) filed in a date range.

    EDGAR lists Form 3/4/5 under the issuer and under every reporting owner,
    so one submissions file answers both a company and a person lookup.
    """
    cik = _cik(target.get("cik"))
    start, end = _date(target.get("start_date")), _date(target.get("end_date"))
    if not start or not end or start > end:
        raise SecSourceError("invalid_entity_range")
    payload = transport.get(f"https://data.sec.gov/submissions/CIK{cik}.json", "application/json")
    parsed = parse_submissions(payload, cik=cik)
    entries = list(parsed["entries"])
    older = [file for file in parsed["files"] if (file["filing_to"] or "9999") >= start]
    for file in older[:ENTITY_OLDER_FILES]:
        entries += parse_submissions(transport.get(file["url"], "application/json"), cik=cik)["entries"]
    selected = [entry for entry in entries if start <= entry["filing_date"] <= end]
    data = _json(payload)
    return _result(
        transport,
        entries=selected,
        complete=len(older) <= ENTITY_OLDER_FILES,
        reason="entity_submissions_read",
        coverage_scope="entity_ownership_filings",
        entity={"cik": cik, "name": data.get("name"), "tickers": list(data.get("tickers") or [])[:5],
                "entity_type": data.get("entityType")},
    )


def _lookup(target: dict, transport: _Transport) -> dict:
    """Candidates for a typed ticker or name: company tickers first, then EDGAR's entity search."""
    query = str(target.get("query", "")).strip()
    if not 0 < len(query) <= 100:
        raise SecSourceError("invalid_lookup_query")
    candidates = []
    if target.get("mode") == "ticker":
        wanted = query.upper().replace(".", "-")
        for row in parse_company_tickers(transport.get(TICKERS_URL, "application/json")):
            if row["ticker"].upper().replace(".", "-") == wanted:
                candidates.append({"cik": row["cik"], "name": row["name"], "tickers": [row["ticker"]],
                                   "exchange": row["exchange"], "ticker": row["ticker"]})
    if not candidates:
        url = ENTITY_SEARCH_URL + "?" + urlencode({"keysTyped": query})
        candidates = parse_entity_search(transport.get(url, "application/json"))
    return {
        **_result(transport, complete=True, reason="lookup_candidates_read",
                  coverage_scope="current_candidates_only"),
        "candidates": candidates,
        # Ticker matches also link an existing security to its issuer.
        "entries": [row for row in candidates if row.get("ticker")],
    }


def run_sec_operation(kind: str, target: dict, *, fetch: Fetch) -> dict:
    """Execute one bounded operation through the shared SEC-budget callback.

    ``sec_discover``: mode latest(default)/daily/quarterly; max_pages defaults 1,
    page_size 100; daily/quarterly require inclusive start_date/end_date. Pass the
    returned cursor unchanged on continuation. Latest completion additionally
    requires a reconciled ``watermark`` (ISO instant with offset); exhausting the
    limited feed is a gap requiring daily/quarterly reconciliation. The coordinator
    should periodically restart Latest at page zero to overlap a moving feed.

    ``sec_document``: accession plus submission_url or index_url from discovery.
    ``sec_identity``: optional tickers filter, current SEC identity candidates.
    ``sec_submissions``: cik, optionally a validated url from a declared older file.

    Source-format failures retain fetched documents and return an incomplete scan.
    HTTP errors other than absent documents/indexes propagate for shared cooldown.
    Callers must never treat a complete index/feed scan as parsed issuer coverage.
    """
    if not isinstance(target, dict) or not callable(fetch):
        raise SecSourceError("target_and_shared_fetch_required")
    transport = _Transport(fetch)
    if kind == "sec_discover":
        mode = target.get("mode", "latest")
        if mode == "latest":
            return _latest(target, transport)
        if mode in {"daily", "quarterly"}:
            return _indexes(target, transport, mode)
        if mode == "entity":
            return _entity(target, transport)
        raise SecSourceError("unsupported_sec_discovery_mode")
    if kind == "sec_document":
        return _document(target, transport)
    if kind == "sec_identity" and target.get("query"):
        return _lookup(target, transport)
    if kind == "sec_identity":
        payload = transport.get(TICKERS_URL, "application/json")
        candidates = parse_company_tickers(payload)
        if target.get("tickers"):
            requested = {str(value).upper() for value in target["tickers"]}
            candidates = [row for row in candidates if row["ticker"].upper() in requested]
        return {
            **_result(
                transport,
                complete=True,
                reason="current_identity_candidates_read",
                coverage_scope="current_candidates_only",
            ),
            "candidates": candidates,
            "entries": candidates,
        }
    if kind == "sec_submissions":
        cik = _cik(target.get("cik"))
        url = target.get("url") or f"https://data.sec.gov/submissions/CIK{cik}.json"
        if not re.fullmatch(
            rf"https://data\.sec\.gov/submissions/CIK{cik}(?:-submissions-\d+)?\.json", url
        ):
            raise SecSourceError("submissions_url_cik_conflict")
        payload = transport.get(url, "application/json")
        parsed = parse_submissions(payload, cik=cik)
        return {
            **_result(
                transport,
                entries=parsed["entries"],
                complete=not parsed["files"],
                reason="submitting_entity_page_read",
                coverage_scope=parsed["coverage_scope"],
            ),
            "files": parsed["files"],
        }
    raise SecSourceError("unsupported_sec_operation")
