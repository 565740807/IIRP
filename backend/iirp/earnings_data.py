"""Candidate and fiscal-event evidence. SEC acceptance is never announcement time."""

import base64
import json
import re
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select

from iirp.business_models import CoverageSegment, EarningsEvent, Security
from iirp.market_data import digest, plain
from iirp.models import now
from iirp.sec_sources import validate_sec_url

ET = ZoneInfo("America/New_York")
EARNINGS_PARSER_VERSION = "earnings-evidence-v3"
PRECISIONS = {"exact", "before_open", "after_close", "date_only", "intraday", "conflict"}
MONTH_NAMES = (
    "January February March April May June July August September October November December".split()
)
MONTH_PATTERN = "|".join(f"{month}|{month[:3]}\\.?" for month in MONTH_NAMES)
DATE_PATTERN = rf"\b({MONTH_PATTERN})\s+(\d{{1,2}}),?\s+(20\d{{2}})\b"


def _date_match(match):
    month = next(
        i + 1
        for i, name in enumerate(MONTH_NAMES)
        if name[:3].lower() == match.group(1)[:3].lower()
    )
    return date(int(match.group(3)), month, int(match.group(2)))


def event_dict(event):
    def supports_actual_time(item):
        if not isinstance(item, dict) or item.get("rejected"):
            return False
        if not isinstance(item.get("time_evidence"), str) or not item["time_evidence"].strip():
            return False
        try:
            validate_source_url(item.get("source_url"))
        except ValueError:
            return False
        if (item.get("announced_date") != str(event.announced_date)
            or item.get("fiscal_year") != event.fiscal_year
            or item.get("fiscal_quarter") != event.fiscal_quarter
            or item.get("time_precision") != event.time_precision):
            return False
        if event.time_precision == "exact":
            try:
                claim = datetime.fromisoformat(str(item.get("announced_at")).replace("Z", "+00:00"))
                return event.announced_at is not None and claim.tzinfo is not None and claim == event.announced_at
            except ValueError:
                return False
        return event.time_precision in {"before_open", "after_close"}

    precise = (event.verified and event.time_precision in {"exact", "before_open", "after_close"}
               and any(supports_actual_time(item) for item in (event.evidence or [])))
    # Only an explicit creation observation proves first-seen time. A legacy
    # row's updated_at may reflect a later revision and must not substitute.
    first_observed = next(
        (item["first_observed_at"] for item in (event.evidence or []) if item.get("first_observed_at")),
        None,
    )
    checked = [
        item["verified_at"]
        for item in (event.evidence or [])
        if item.get("verified_at")
    ]
    last_verified = max(checked, key=datetime.fromisoformat) if checked else None
    evidence = event.evidence or []
    legacy_combined_source = any(
        item.get("provider") == "manual_review"
        and item.get("period_kind") in {"regular", "transition"}
        for item in evidence
    ) and not any(item.get("provider") == "manual_period_review" for item in evidence)
    return {
        "id": event.id,
        "security_id": event.security_id,
        "fiscal_year": event.fiscal_year,
        "fiscal_quarter": event.fiscal_quarter,
        "announced_date": str(event.announced_date),
        "announced_at": event.announced_at.isoformat() if event.announced_at else None,
        "time_precision": event.time_precision,
        "verified": event.verified,
        "fiscal_period_verified": bool(
            event.verified and event.fiscal_year and event.fiscal_quarter
        ),
        "precise_time_supported": bool(precise),
        "is_estimate": event.is_estimate,
        "is_primary": event.status not in {"EXCLUDED", "SECONDARY", "DUPLICATE_CONFIRMED"},
        "status": event.status,
        "legacy_combined_source": legacy_combined_source,
        "evidence": event.evidence,
        "revision": event.revision,
        "first_observed_at": first_observed,
        "last_verified_at": last_verified,
        "updated_at": event.updated_at.isoformat() if event.updated_at else None,
    }


def fetch_candidates(target, *, ticker_factory=None):
    if ticker_factory is None:
        import yfinance as yf

        ticker_factory = yf.Ticker
    offset, page_size = target.get("offset", 0), target.get("page_size", 100)
    if (
        type(offset) is not int
        or offset < 0
        or type(page_size) is not int
        or not 1 <= page_size <= 100
    ):
        raise ValueError("财报候选分页参数无效。")
    # The pinned library caches on limit only. A new object per page is required.
    frame = ticker_factory(target["symbol"]).get_earnings_dates(limit=page_size, offset=offset)
    records = []
    if frame is not None:
        for stamp, row in frame.iterrows():
            raw = stamp.isoformat()
            stamp_et = (
                stamp.tz_convert("America/New_York") if getattr(stamp, "tzinfo", None) else stamp
            )
            records.append(
                {
                    "announced_at": raw,
                    "announced_date": stamp_et.date().isoformat(),
                    "timezone_known": getattr(stamp, "tzinfo", None) is not None,
                    "values": plain(row.to_dict()),
                }
            )
    # Changing EPS estimates on a repeated page must not count as date progress.
    fingerprint = digest(sorted((r["announced_date"], r["announced_at"]) for r in records))
    seen = list(target.get("page_fingerprints", []))
    if target.get("previous_fingerprint"):
        seen.append(target["previous_fingerprint"])
    oldest = min((r["announced_date"] for r in records), default=None)
    newest = max((r["announced_date"] for r in records), default=None)
    reason, next_offset, complete = None, None, False
    if not records:
        reason = "candidate_source_empty"
    elif fingerprint in seen:
        reason = "candidate_repeated_page"
    elif target.get("previous_oldest") and oldest >= target["previous_oldest"]:
        reason = "candidate_history_not_advancing"
    elif oldest <= target["start_date"]:
        complete = True
    elif len(records) >= page_size:
        next_offset = offset + len(records)
    else:
        reason = "candidate_source_exhausted_before_requested_start"
    cursor = (
        {
            "offset": next_offset,
            "previous_fingerprint": fingerprint,
            "previous_oldest": oldest,
            "page_fingerprints": list(dict.fromkeys([*seen, fingerprint])),
        }
        if next_offset is not None
        else None
    )
    return {
        "records": records,
        "offset": offset,
        "raw_record_count": len(records),
        "fingerprint": fingerprint,
        "next_offset": next_offset,
        "cursor": cursor,
        "reason": reason,
        "searched_to": oldest,
        "newest": newest,
        "provider": "yfinance",
        "fetched_at": now().isoformat(),
        "complete": complete,
        "coverage_scope": "candidate_dates_only_not_verified_fiscal_years",
    }


def _new_event(security_id, day):
    return EarningsEvent(
        security_id=security_id,
        announced_date=day,
        announced_at=None,
        time_precision="date_only",
        verified=False,
        is_estimate=day > now().astimezone(ET).date(),
        status="CANDIDATE",
        evidence=[],
        revision=1,
    )


def _coverage(s, job, response, source_hash, kind):
    start, end = job.target.get("start_date"), job.target.get("end_date")
    if not start or not end:
        return
    marker = digest([kind, getattr(job, "id", None), source_hash])
    identifier = f"{marker[:8]}-{marker[8:12]}-{marker[12:16]}-{marker[16:20]}-{marker[20:32]}"
    if s.get(CoverageSegment, identifier):
        return
    s.add(
        CoverageSegment(
            id=identifier,
            security_id=job.target["security_id"],
            provider="yfinance" if kind == "earnings_candidates" else "sec",
            kind=kind,
            start_date=date.fromisoformat(start),
            end_date=date.fromisoformat(end),
            status="COMPLETE" if response.get("complete") else "PARTIAL",
            source_hash=source_hash,
            details={
                "scope": response.get("coverage_scope", "source_page_only"),
                "reason": response.get("reason"),
                "searched_to": response.get("searched_to"),
                "cursor": response.get("cursor"),
            },
        )
    )


def persist_candidates(s, job, response, source_hash):
    added, changed, seen = 0, 0, set()
    for record in response.get("records", []):
        day = date.fromisoformat(record["announced_date"])
        if day in seen or str(day) < job.target["start_date"] or str(day) > job.target["end_date"]:
            continue
        seen.add(day)
        event = s.scalar(
            select(EarningsEvent).where(
                EarningsEvent.security_id == job.target["security_id"],
                EarningsEvent.announced_date == day,
            )
        )
        observed = now().isoformat()
        evidence = {
            "provider": "yfinance",
            "observed_at": observed,
            "source_hash": source_hash,
            "candidate": record,
            "verified": False,
        }
        if event is None:
            event = _new_event(job.target["security_id"], day)
            evidence["first_observed_at"] = observed
            s.add(event)
            added += 1
        elif any(x.get("source_hash") == source_hash for x in (event.evidence or [])):
            continue
        else:
            event.revision = (event.revision or 1) + 1
        # Guessed provider clocks stay only in candidate provenance.
        event.evidence = [*(event.evidence or []), evidence]
        event.updated_at = now()
        changed += 1
    job.checkpoint = {
        **(job.checkpoint or {}),
        "earnings_candidates": {
            k: response.get(k)
            for k in (
                "complete",
                "reason",
                "cursor",
                "fingerprint",
                "searched_to",
                "newest",
                "raw_record_count",
            )
        },
    }
    _coverage(s, job, response, source_hash, "earnings_candidates")
    return {
        "candidate_events": added,
        "updated_events": changed,
        "searched_to": response.get("searched_to"),
        "candidate_scan_complete": bool(response.get("complete")),
        "reason": response.get("reason"),
        "candidate_cursor": response.get("cursor"),
    }


def _document(url, payload, media_type):
    try:
        text, encoding = payload.decode("utf-8"), "utf8"
    except UnicodeDecodeError:
        text, encoding = base64.b64encode(payload).decode("ascii"), "base64"
    return {"url": url, "payload": text, "encoding": encoding, "media_type": media_type}


class _ReleaseHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.published, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style"}:
            self.skip += 1
        if tag == "meta" and attrs.get("property", attrs.get("name", "")).lower() in {
            "article:published_time",
            "datepublished",
            "pubdate",
        }:
            self.published.append(attrs.get("content", ""))

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)

    def handle_data(self, value):
        if not self.skip and value.strip():
            self.parts.append(value.strip())


def _quarter_period(text):
    words = {
        "first": 1,
        "second": 2,
        "third": 3,
        "fourth": 4,
        "1st": 1,
        "2nd": 2,
        "3rd": 3,
        "4th": 4,
    }
    pattern = (
        r"\b(?:Q([1-4])|(first|second|third|fourth|1st|2nd|3rd|4th)[ -](?:fiscal[ -])?quarter)\b"
    )
    quarter = re.search(pattern, text[:700], re.I) or re.search(pattern, text[:2400], re.I)
    q = (
        int(quarter.group(1))
        if quarter and quarter.group(1)
        else words[quarter.group(2).lower()]
        if quarter
        else None
    )
    years = list(re.finditer(r"\b(?:fiscal(?:\s+year)?\s*|FY\s*)(20\d{2})\b", text[:2400], re.I))
    year = None
    if years:
        anchor = quarter.start() if quarter else 0
        ranked = sorted(years, key=lambda match: abs(match.start() - anchor))
        if len(ranked) == 1 or abs(ranked[0].start() - anchor) + 30 < abs(
            ranked[1].start() - anchor
        ):
            year = int(ranked[0].group(1))
    pair = re.search(r"\bQ([1-4])\s+(?:fiscal\s+)?(20\d{2})\b", text[:700], re.I)
    if pair:
        q, year = int(pair.group(1)), int(pair.group(2))
    elif quarter:
        adjacent = re.match(
            r"[\s-]+(?:of\s+)?(?:fiscal\s+)?(20\d{2})\b", text[quarter.end() : 700], re.I
        )
        if adjacent:
            year = int(adjacent.group(1))
        annual = re.match(
            r"\s+and\s+(?:the\s+)?(?:full|fiscal)[ -]+year\s+(20\d{2})\b",
            text[quarter.end() : 700],
            re.I,
        )
        if annual:
            year = int(annual.group(1))
    if year is None and q:
        for another in re.finditer(pattern, text[:2400], re.I):
            another_q = (
                int(another.group(1)) if another.group(1) else words[another.group(2).lower()]
            )
            if another_q != q:
                continue
            adjacent = re.match(
                r"[\s-]+(?:of\s+)?(?:fiscal\s+)?(20\d{2})\b", text[another.end() : 2400], re.I
            )
            if adjacent:
                year = int(adjacent.group(1))
                break
    return year, q


def extract_release(payload, *, filed_date, source_url, fiscal_year_end=None):
    """Require an actual earnings headline/dateline; ignore SEC/call timestamps."""
    parser = _ReleaseHTML()
    parser.feed(payload.decode("utf-8", errors="replace"))
    text = re.sub(r"\s+", " ", unescape(" ".join(parser.parts)))
    lead = text[:3000]
    actual = bool(
        re.search(
            r"\b(?:reports|announces|reported|announced)\b.{0,220}\b(?:results|earnings)\b",
            lead,
            re.I,
        )
    )
    actual = actual or bool(
        re.search(r"\b(?:reports|announces|reported|announced)\b.{0,220}\bquarter\b", lead, re.I)
        and re.search(r"\b(?:revenue|revenues|earnings|income|EPS|cash flow)\b", lead, re.I)
    )
    future_notice = re.search(
        r"\b(?:will|to)\s+(?:report|announce)\b|\bschedules?\b.{0,80}\b(?:earnings|conference)\b",
        text[:200],
        re.I,
    ) or re.search(
        r"\b(?:announces|schedules)\b.{0,100}\b(?:conference call|webcast|earnings date)\b",
        text[:250],
        re.I,
    )
    actual_headline = re.search(
        r"\b(?:reports|reported|announces|announced)\b.{0,120}\bresults\b", text[:200], re.I
    )
    if future_notice and not actual_headline:
        actual = False
    year, quarter = _quarter_period(text) if actual else (None, None)
    dates = re.compile(DATE_PATTERN, re.I)
    release_date, dateline = None, None
    date_observations = []
    filed = date.fromisoformat(filed_date)
    for match in dates.finditer(lead):
        try:
            day = _date_match(match)
        except ValueError:
            continue
        raw_before = lead[max(0, match.start() - 70) : match.start()]
        before = raw_before.lower()
        after = lead[match.end() : match.end() + 360]
        if re.search(r"(?:ended|ending|as of|period|quarter|year)\s*$", before):
            continue
        city_dateline = bool(re.search(r"\b[A-Z][A-Z .-]{1,30},\s*$", raw_before))
        if city_dateline:
            date_observations.append(str(day))
        if -1 <= (filed - day).days <= 30 and (
            city_dateline
            or match.start() < 500
            or re.search(r"\btoday\b.{0,60}\b(?:announced|reported)\b", after, re.I)
        ):
            release_date, dateline = day, match
            break
    stamp, precision = None, "date_only"
    time_evidence = None
    published = []
    for raw in parser.published:
        try:
            candidate = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if candidate.tzinfo and abs((filed - candidate.astimezone(ET).date()).days) <= 30:
            published.append(candidate)
    publication_conflict = len(set(published)) > 1 or bool(
        release_date
        and any(candidate.astimezone(ET).date() != release_date for candidate in published)
    )
    if publication_conflict:
        precision = "conflict"
        release_date = release_date or published[0].astimezone(ET).date()
    elif published:
        stamp, precision, release_date = published[0], "exact", published[0].astimezone(ET).date()
    if dateline and stamp is None and not publication_conflict:
        local = lead[dateline.end() : dateline.end() + 95]
        clock = re.match(
            r"[\s,;|/—–-]*(?:at\s+)?(\d{1,2}):(\d{2})\s*([ap])\.?m\.?\s*(EDT|EST|ET|Eastern(?:\s+Time)?)\b",
            local,
            re.I,
        )
        conference_clock = bool(
            clock
            and re.search(
                r"\b(?:conference|webcast|earnings call|conference call)\b",
                local[clock.end() :],
                re.I,
            )
        )
        if (
            clock
            and not conference_clock
            and 1 <= int(clock.group(1)) <= 12
            and int(clock.group(2)) <= 59
        ):
            hour = int(clock.group(1)) % 12 + (12 if clock.group(3).lower() == "p" else 0)
            candidate = datetime.combine(release_date, datetime.min.time(), ET).replace(
                hour=hour, minute=int(clock.group(2))
            )
            zone = clock.group(4).upper()
            if zone not in {"EST", "EDT"} or candidate.tzname() == zone:
                stamp, precision = candidate, "exact"
                time_evidence = f"发行人公告日行的实际发布时间：{local[:clock.end()].strip()}"
    if actual and release_date and stamp is None and not publication_conflict:
        statement = lead[:1000]
        # Bind the session phrase to publication of results or the release.
        # A scheduled earnings call before the open is not publication time.
        actual_release_phrase = (
            r"(?:released|issued|published|announced)\s+"
            r"(?:(?:its|the|a)\s+)?"
            r"(?:(?:quarterly|financial|earnings|fiscal|second|first|third|fourth)\s+){0,3}"
            r"(?:results|earnings|press release)\s+"
        )
        before = re.search(
            actual_release_phrase + r"before (?:the )?(?:market open|opening bell)",
            statement, re.I,
        )
        after = re.search(
            actual_release_phrase + r"after (?:the )?(?:market close|closing bell)",
            statement, re.I,
        )
        if before:
            precision, time_evidence = "before_open", before.group()
        elif after:
            precision, time_evidence = "after_close", after.group()
    period_end = None
    period = re.search(r"quarter\s+ended\s+" + DATE_PATTERN, lead, re.I)
    if period:
        try:
            period_end = _date_match(period)
        except ValueError:
            pass
    if period_end is None and actual and release_date:
        for period in re.finditer(
            r"\b(?:Three\s+Months|Quarter)\s+Ended\s+" + DATE_PATTERN, text, re.I
        ):
            try:
                candidate = _date_match(period)
            except ValueError:
                continue
            if 0 <= (release_date - candidate).days <= 200:
                period_end = candidate
                break
    rule = "literal_release_label" if year and quarter else None
    # Current fiscal-end metadata supports a stated quarter and matching actual
    # period end; it never blindly relabels all old quarters.
    if year is None and quarter and period_end and fiscal_year_end:
        month, day = map(int, fiscal_year_end.split("-"))
        expected_month = ((month - (4 - quarter) * 3 - 1) % 12) + 1
        if period_end.month == expected_month and abs(period_end.day - min(day, 28)) <= 7:
            year = period_end.year + (1 if period_end.month > month else 0)
            rule = "stated_quarter_period_end_and_verified_fiscal_end"
    reason = (
        None
        if actual and release_date and year and quarter
        else "release_date_or_fiscal_period_unconfirmed"
    )
    if publication_conflict:
        reason = "conflicting_announcement_time"
    if not actual:
        release_date, stamp, year, quarter = None, None, None, None
        reason = "not_an_actual_earnings_release"
    return {
        "announced_date": release_date.isoformat() if release_date else None,
        "announced_at": stamp.isoformat() if stamp else None,
        "time_precision": precision,
        "time_evidence": time_evidence,
        "publication_observations": [stamp.isoformat() for stamp in published],
        "date_observations": date_observations,
        "fiscal_year": year,
        "fiscal_quarter": quarter,
        "fiscal_rule": rule,
        "period_end": period_end.isoformat() if period_end else None,
        "source_url": source_url,
        "excerpt": lead,
        "reason": reason,
        "is_correction": bool(
            re.search(r"\b(?:corrects|corrected|correction|restated)\b", text[:500], re.I)
        ),
    }


def _announcement_context(payload):
    """Explicit 8-K/6-K statements link a release date to its exhibit.

    Filing/acceptance/signature dates are deliberately not candidates. Modern
    Apple EX99 attachments omit a dateline while Item2.02 states the issue date.
    """
    contexts = []
    for document in re.findall(rb"<DOCUMENT>(.*?)</DOCUMENT>", payload, re.S | re.I):
        metadata, marker, body = document.partition(b"<TEXT>")
        kind = re.search(rb"<TYPE>\s*([^\r\n<]+)", metadata, re.I)
        if (
            not marker
            or not kind
            or kind.group(1).strip().upper() not in {b"8-K", b"8-K/A", b"6-K", b"6-K/A"}
        ):
            continue
        parser = _ReleaseHTML()
        parser.feed(body.decode("utf-8", errors="replace"))
        text = re.sub(r"\s+", " ", " ".join(parser.parts))
        for match in re.finditer(r"\bOn\s+" + DATE_PATTERN, text, re.I):
            following = text[match.end() : match.end() + 650]
            issued = re.match(
                r".{0,160}?\b(?:issued|released|published)\b.{0,60}?\b(?:press|earnings)\s+release\b",
                following,
                re.I,
            )
            if not issued or not re.search(
                r"\b(?:financial|earnings|quarter|annual|results)\b", following[:400], re.I
            ):
                continue
            exhibits = re.findall(r"\bExhibit\s+(99(?:\.\d+)?)\b", following, re.I)
            if not exhibits:
                continue
            try:
                day = _date_match(match)
            except ValueError:
                continue
            contexts.append(
                {
                    "announced_date": str(day),
                    "exhibits": exhibits,
                    "excerpt": text[match.start() : match.end() + min(len(following), 650)],
                    "date_basis": "issuer_statement_linking_release_and_exhibit",
                }
            )
        for match in re.finditer(DATE_PATTERN, text, re.I):
            previous = text[max(0, match.start() - 250) : match.start()]
            following = text[match.end() : match.end() + 400]
            if not re.search(r"\bdated\s*$", previous, re.I) or not re.search(
                r"\bpress\s+release\b", previous, re.I
            ):
                continue
            if not re.search(
                r"\breporting\b.{0,100}\b(?:financial results|earnings)\b", following, re.I
            ):
                continue
            context = previous + match.group() + following
            exhibits = re.findall(r"\bExhibit\s+(99(?:\.\d+)?)\b", context, re.I)
            if not exhibits:
                continue
            try:
                day = _date_match(match)
            except ValueError:
                continue
            fiscal_year, fiscal_quarter = _quarter_period(following)
            contexts.append(
                {
                    "announced_date": str(day),
                    "exhibits": exhibits,
                    "excerpt": context,
                    "fiscal_year": fiscal_year,
                    "fiscal_quarter": fiscal_quarter,
                    "date_basis": "issuer_statement_linking_dated_release_and_exhibit",
                }
            )
    return contexts


def _listing(payload, target):
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("SEC 财报清单不是预期 JSON 对象。")
    cik = str(target["cik"]).zfill(10)
    if data.get("cik") is not None and str(data["cik"]).zfill(10) != cik:
        raise ValueError("SEC 财报清单的 CIK 与请求公司不一致。")
    recent = data.get("filings", {}).get("recent", data)
    if any(
        not isinstance(recent.get(name), list) for name in ("form", "filingDate", "accessionNumber")
    ):
        raise ValueError("SEC 财报清单缺少必要列。")
    length = len(recent["form"])
    if any(not isinstance(values, list) or len(values) != length for values in recent.values()):
        raise ValueError("SEC 财报清单列长度不一致，不能静默截断。")
    filings = []
    for i, form in enumerate(recent["form"]):
        if form not in {"8-K", "8-K/A", "6-K", "6-K/A"}:
            continue
        day = recent["filingDate"][i]
        date.fromisoformat(day)
        if not target["start_date"] <= day <= target["end_date"]:
            continue
        items = recent.get("items", [""] * length)[i]
        if form.startswith("8-K") and items and "2.02" not in items:
            continue
        accession = recent["accessionNumber"][i]
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
            raise ValueError("SEC 财报清单 accession 无效。")
        filings.append(
            {
                "accession": accession,
                "form": form,
                "submission_url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}.txt",
                "filing_date": day,
            }
        )
    older = []
    for item in data.get("filings", {}).get("files", []):
        filename = item.get("name", "")
        if not re.fullmatch(rf"CIK{cik}-submissions-\d+\.json", filename):
            raise ValueError("SEC 历史文件名与目标公司不一致。")
        if not item.get("filingTo") or not item.get("filingFrom"):
            raise ValueError("SEC 历史文件缺少覆盖日期。")
        if item["filingTo"] >= target["start_date"] and item["filingFrom"] <= target["end_date"]:
            older.append(
                {
                    "filename": filename,
                    "filing_from": item["filingFrom"],
                    "filing_to": item["filingTo"],
                }
            )
    symbol = target.get("symbol", "").upper().replace(".", "-")
    identity_verified = bool(
        data.get("cik") is not None
        and symbol
        and symbol in {str(value).upper().replace(".", "-") for value in data.get("tickers", [])}
    )
    return {
        "filings": filings,
        "next_files": sorted(older, key=lambda item: item["filing_to"], reverse=True),
        "fiscal_year_end": data.get("fiscalYearEnd"),
        "identity_verified": identity_verified,
        "metadata": {"cik": cik, "name": data.get("name"), "tickers": data.get("tickers", [])},
    }


def fetch_evidence(target, fetch):
    cik = str(target["cik"]).zfill(10)
    if not re.fullmatch(r"\d{10}", cik) or int(cik) == 0:
        raise ValueError("财报公司 CIK 无效。")
    if target.get("submission_url"):
        url = validate_sec_url(target["submission_url"])
        if not re.fullmatch(
            rf"https://www\.sec\.gov/Archives/edgar/data/{int(cik)}/\d{{18}}/\d{{10}}-\d{{2}}-\d{{6}}\.txt",
            url,
        ):
            raise ValueError("财报原文地址与公司 CIK 不一致。")
        payload, releases, rejected = fetch(url), [], []
        announcement_contexts = _announcement_context(payload)
        for document in re.findall(rb"<DOCUMENT>(.*?)</DOCUMENT>", payload, re.S | re.I):
            metadata, marker, body = document.partition(b"<TEXT>")
            kind = re.search(rb"<TYPE>\s*([^\r\n<]+)", metadata, re.I)
            filename = re.search(rb"<FILENAME>\s*([^\r\n<]+)", metadata, re.I)
            if (
                not marker
                or not kind
                or not re.fullmatch(rb"(?:EX-)?99(?:\.\d+)?", kind.group(1).strip(), re.I)
                or not filename
            ):
                continue
            name = filename.group(1).decode("ascii").strip()
            if not re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:htm|html|txt|xml)", name, re.I):
                continue
            document_url = validate_sec_url(url.rsplit("/", 1)[0] + "/" + name)
            release = extract_release(
                body,
                filed_date=target["filing_date"],
                source_url=document_url,
                fiscal_year_end=target.get("fiscal_year_end"),
            )
            exhibit = kind.group(1).decode("ascii").strip().upper().removeprefix("EX-")
            relevant = [
                context for context in announcement_contexts if exhibit in context["exhibits"]
            ]
            dates = {context["announced_date"] for context in relevant}
            if (
                not release["announced_date"]
                and len(dates) == 1
                and release["reason"] != "not_an_actual_earnings_release"
            ):
                release["announced_date"] = dates.pop()
                release["announcement_date_evidence"] = relevant
                release["announcement_context_source_url"] = url
                release["time_precision"] = "date_only"
                if release.get("fiscal_year") and release.get("fiscal_quarter"):
                    release["reason"] = None
            if release["announced_date"] and dates and dates != {release["announced_date"]}:
                release["time_precision"] = "conflict"
                release["announced_at"] = None
                release["reason"] = "conflicting_announcement_date_evidence"
                release["announcement_date_evidence"] = relevant
            periods = {
                (context.get("fiscal_year"), context.get("fiscal_quarter"))
                for context in relevant
                if context.get("fiscal_year") and context.get("fiscal_quarter")
            }
            if len(periods) == 1:
                context_year, context_quarter = next(iter(periods))
                if not release.get("fiscal_year") and release.get("fiscal_quarter") in {
                    None,
                    context_quarter,
                }:
                    release["fiscal_year"], release["fiscal_quarter"] = (
                        context_year,
                        context_quarter,
                    )
                    release["fiscal_rule"] = "issuer_statement_linking_release_and_exhibit"
                    release["fiscal_period_evidence"] = relevant
                    if release["announced_date"] and release["time_precision"] != "conflict":
                        release["reason"] = None
            if release["announced_date"] and any(
                value != release["announced_date"] for value in release.get("date_observations", [])
            ):
                release["time_precision"], release["announced_at"] = "conflict", None
                release["reason"] = "conflicting_announcement_date_evidence"
            if release["announced_date"]:
                releases.append(release)
            else:
                rejected.append(release)
        no_earnings_release = bool(rejected) and all(
            item.get("reason") == "not_an_actual_earnings_release" for item in rejected
        )
        reason = (
            None if releases or no_earnings_release else "earnings_release_attachment_unconfirmed"
        )
        return {
            "kind": "document",
            "parser_version": EARNINGS_PARSER_VERSION,
            "releases": releases,
            "rejected_releases": rejected,
            "release": releases[0] if len(releases) == 1 else None,
            "source_documents": [_document(url, payload, "text/plain")],
            "filings": [],
            "next_files": [],
            "complete": bool(releases) or no_earnings_release,
            "classification": "EARNINGS_RELEASE"
            if releases
            else "NO_EARNINGS_RELEASE"
            if no_earnings_release
            else "UNCONFIRMED",
            "reason": reason,
            "coverage_scope": "release_document_only",
        }
    filename = target.get("filename") or f"CIK{cik}.json"
    if not re.fullmatch(rf"CIK{cik}(?:-submissions-\d+)?\.json", filename):
        raise ValueError("无效 SEC submissions 文件。")
    url = validate_sec_url("https://data.sec.gov/submissions/" + filename)
    payload = fetch(url)
    return {
        **_listing(payload, target),
        "kind": "listing",
        "parser_version": EARNINGS_PARSER_VERSION,
        "source_documents": [_document(url, payload, "application/json")],
        "complete": True,
        "reason": None,
        "coverage_scope": "submissions_file_only",
    }


def reparse_evidence(target, cached_response):
    """Replay only persisted original SEC documents; missing inputs never fetch."""
    originals = {
        item["url"]: item["payload"].encode("utf-8")
        if item.get("encoding") == "utf8"
        else base64.b64decode(item["payload"], validate=True)
        for item in cached_response.get("source_documents", [])
    }

    def read(url):
        if url not in originals:
            raise ValueError("已保存来源缺少本次解析所需原文，请单独补齐来源。")
        return originals[url]

    return fetch_evidence(target, read)


def _bump(event):
    event.revision = (event.revision or 1) + 1
    event.updated_at = now()


def persist_evidence(s, job, response, sources, source_hash):
    security = s.get(Security, job.target["security_id"], with_for_update=True)
    if security is None:
        raise ValueError("财报请求的证券不存在。")
    fiscal = response.get("fiscal_year_end")
    if fiscal and re.fullmatch(r"\d{4}", fiscal) and response.get("identity_verified"):
        try:
            date(2000, int(fiscal[:2]), int(fiscal[2:]))
        except ValueError:
            raise ValueError("SEC 财年结束日不是有效月日。") from None
        metadata = response.get("metadata", {})
        if str(metadata.get("cik", "")).zfill(10) != str(security.issuer_id or "").zfill(10):
            raise ValueError("财年结束日对应的 SEC 公司身份冲突。")
        security.metadata_json = {
            **(security.metadata_json or {}),
            "verified_fiscal_year_end": fiscal[:2] + "-" + fiscal[2:],
            "fiscal_year_end_evidence": {
                "provider": "SEC submissions",
                "source_hash": next(iter(sources.values()), source_hash),
                "cik": metadata["cik"],
                "verified_at": now().isoformat(),
            },
            "sec_identity_confirmed": True,
        }
    releases = response.get("releases") or (
        [response["release"]] if response.get("release") else []
    )
    verified_events, changed, gaps = 0, 0, []
    for release in releases:
        if not release.get("announced_date"):
            continue
        day = date.fromisoformat(release["announced_date"])
        event = s.scalar(
            select(EarningsEvent)
            .where(EarningsEvent.security_id == security.id, EarningsEvent.announced_date == day)
            .with_for_update()
        )
        is_new = event is None
        if is_new:
            event = _new_event(security.id, day)
            s.add(event)
        evidence = {
            **release,
            "source_hash": sources.get(
                release["source_url"], sources.get(job.target.get("submission_url"), source_hash)
            ),
            "provider": "SEC release",
            "parser_version": EARNINGS_PARSER_VERSION,
            "observed_at": now().isoformat(),
        }
        if is_new:
            evidence["first_observed_at"] = evidence["observed_at"]
        if any(
            x.get("source_hash") == evidence["source_hash"]
            and x.get("source_url") == evidence["source_url"]
            and x.get("parser_version") == EARNINGS_PARSER_VERSION
            for x in event.evidence
        ):
            continue
        event.evidence = [*event.evidence, evidence]
        year, quarter = release.get("fiscal_year"), release.get("fiscal_quarter")
        supported = bool(type(year) is int and 1900 <= year <= 2200 and quarter in {1, 2, 3, 4})
        precision = release.get("time_precision", "date_only")
        if precision not in PRECISIONS:
            raise ValueError("公告时间精度无效。")
        stamp = (
            datetime.fromisoformat(release["announced_at"]) if release.get("announced_at") else None
        )
        if stamp is not None and (stamp.tzinfo is None or stamp.astimezone(ET).date() != day):
            raise ValueError("公告时刻必须具有时区并匹配公告日期。")
        if precision == "exact" and stamp is None:
            precision = "date_only"
        has_manual = any(x.get("provider") == "manual_review" for x in event.evidence)
        conflict = (
            event.verified
            and supported
            and (
                event.fiscal_year != year
                or event.fiscal_quarter != quarter
                or (event.announced_at and stamp and event.announced_at != stamp)
                or precision == "conflict"
            )
        )
        if (conflict or event.status == "CONFLICT") and not has_manual:
            event.verified, event.status, event.time_precision = False, "CONFLICT", "conflict"
            gaps.append("conflicting_release_observations")
        elif not has_manual and not event.verified:
            event.fiscal_year, event.fiscal_quarter = year, quarter
            event.announced_at, event.time_precision = stamp, precision
            event.verified = (
                supported and precision != "conflict" and not release.get("is_correction")
            )
            event.status = (
                "CONFLICT"
                if precision == "conflict"
                else "VERIFIED"
                if event.verified and precision in {"exact", "before_open", "after_close"}
                else "DATE_VERIFIED"
                if event.verified
                else "NEEDS_REVIEW"
            )
        elif (
            not has_manual
            and event.verified
            and supported
            and precision in {"exact", "before_open", "after_close"}
            and event.time_precision == "date_only"
        ):
            event.announced_at, event.time_precision, event.status = stamp, precision, "VERIFIED"
        if event.verified and supported and not has_manual and precision != "conflict" and not release.get("is_correction"):
            evidence["verified_at"] = now().isoformat()
        # A scheduled candidate becomes an occurred release only after actual
        # announcement evidence is accepted; passage of time alone is insufficient.
        if event.verified and not release.get("is_correction") and day <= now().astimezone(ET).date():
            event.is_estimate = False
        if not is_new:
            _bump(event)
        changed += 1
        verified_events += int(event.verified)
        if not supported or event.time_precision in {"date_only", "intraday", "conflict"}:
            gaps.append("fiscal_period_or_precise_time_needs_review")
    for rejected in response.get("rejected_releases", []):
        raw_hash = sources.get(
            rejected["source_url"], sources.get(job.target.get("submission_url"), source_hash)
        )
        for event in s.scalars(
            select(EarningsEvent).where(EarningsEvent.security_id == security.id).with_for_update()
        ):
            prior = [
                item
                for item in (event.evidence or [])
                if item.get("provider") == "SEC release"
                and item.get("source_hash") == raw_hash
                and item.get("source_url") == rejected["source_url"]
            ]
            if not prior or any(item.get("provider") == "manual_review" for item in event.evidence):
                continue
            if any(
                item.get("parser_version") == EARNINGS_PARSER_VERSION
                and item.get("rejected")
                and item.get("source_hash") == raw_hash
                for item in event.evidence
            ):
                continue
            event.evidence = [
                *event.evidence,
                {
                    **rejected,
                    "provider": "SEC release",
                    "source_hash": raw_hash,
                    "parser_version": EARNINGS_PARSER_VERSION,
                    "rejected": True,
                },
            ]
            event.verified = False
            event.status = (
                "EXCLUDED"
                if rejected.get("reason") == "not_an_actual_earnings_release"
                else "NEEDS_REVIEW"
            )
            _bump(event)
            changed += 1
    followups = []
    base = {
        key: job.target[key]
        for key in ("security_id", "symbol", "cik", "start_date", "end_date")
        if key in job.target
    }
    for older in response.get("next_files", []):
        older = {"filename": older} if isinstance(older, str) else older
        followups.append({**base, "filename": older["filename"]})
    for filing in response.get("filings", []):
        followups.append({**base, **filing})
    job.checkpoint = {
        **(job.checkpoint or {}),
        "earnings_evidence": {
            "complete": bool(response.get("complete")),
            "reason": response.get("reason"),
            "kind": response.get("kind", "document" if releases else "listing"),
            "followups": followups,
            "gaps": sorted(set(gaps)),
        },
    }
    _coverage(s, job, response, source_hash, "earnings_evidence")
    return {
        "evidence_saved": True,
        "parser_version": EARNINGS_PARSER_VERSION,
        "verified_events": verified_events,
        "updated_events": changed,
        "followup_count": len(followups),
        "reason": response.get("reason"),
        "gaps": sorted(set(gaps)),
    }


def validate_source_url(value):
    if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f\\]", value):
        raise ValueError("请提供有效的公开来源链接。")
    parts = urlsplit(value)
    try:
        valid_port = parts.port is None or 1 <= parts.port <= 65535
    except ValueError:
        valid_port = False
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or not valid_port
    ):
        raise ValueError("请提供有效的公开来源链接。")
    return value


def correct_event(s, event_id, values):
    event = s.get(EarningsEvent, event_id, with_for_update=True)
    if not event:
        raise LookupError("财报事件不存在。")
    if event.revision != values["revision"]:
        raise ValueError("事件已更新，请重新读取后核对。")
    validate_source_url(values["source_url"])
    period_kind = values.get("period_kind")
    if period_kind not in {"regular", "transition", "unknown", None}:
        raise ValueError("财期类型须为常规、过渡或未知")
    if period_kind in {"regular", "transition"}:
        if not values.get("period_kind_source_url") or not (values.get("period_kind_evidence") or "").strip():
            raise ValueError("常规/过渡财期须分别填写财期类型来源链接及原文或明确位置")
        validate_source_url(values["period_kind_source_url"])
    precision = values["time_precision"]
    if precision not in PRECISIONS:
        raise ValueError("公告时间精度无效。")
    if precision in {"exact", "before_open", "after_close"} and not (values.get("time_evidence") or "").strip():
        raise ValueError("精确时刻或盘前/盘后须有支持实际首次公开时间的来源原文或明确位置。")
    day = date.fromisoformat(str(values["announced_date"]))
    stamp = datetime.fromisoformat(values["announced_at"]) if values.get("announced_at") else None
    if precision == "exact" and (stamp is None or stamp.tzinfo is None):
        raise ValueError("精确时间必须包含时区。")
    if stamp and (stamp.tzinfo is None or stamp.astimezone(ET).date() != day):
        raise ValueError("公告时间与美东公告日期不一致。")
    existing = s.scalar(
        select(EarningsEvent).where(
            EarningsEvent.security_id == event.security_id,
            EarningsEvent.announced_date == day,
            EarningsEvent.id != event.id,
        )
    )
    if existing:
        raise ValueError(f"该公告日期已有事件 {existing.id}，请核对已有事件，不能覆盖其证据。")
    from iirp.analytics.calendar import reaction_session

    security = s.get(Security, event.security_id)
    if (
        reaction_session(
            stamp,
            announced_date=day,
            time_precision=precision,
            calendar=security.calendar or "XNYS",
        )["status"]
        == "conflicting_event_time"
    ):
        raise ValueError("公告时刻与盘前／盘后分类不一致。")
    is_primary = values.get(
        "is_primary", event.status not in {"SECONDARY", "EXCLUDED", "DUPLICATE_CONFIRMED"}
    )
    prior = event_dict(event)
    prior.pop("evidence")
    reviewed_at = now().isoformat()
    event.evidence = [
        *(event.evidence or []),
        {
            "provider": "manual_review",
            "source_url": values["source_url"],
            "announced_date": str(day),
            "announced_at": stamp.isoformat() if stamp else None,
            "time_precision": precision,
            "time_evidence": values.get("time_evidence") if precision in {"exact", "before_open", "after_close"} else None,
            "observed_at": reviewed_at,
            "verified_at": reviewed_at if precision != "conflict" else None,
            "fiscal_year": values["fiscal_year"],
            "fiscal_quarter": values["fiscal_quarter"],
            "note": values["note"],
            "is_primary": bool(is_primary),
            "previous": prior,
            "reviewed_at": reviewed_at,
        },
        *([{
            "provider": "manual_period_review",
            "review_revision": event.revision + 1,
            "source_url": values.get("period_kind_source_url") if period_kind != "unknown" else None,
            "fiscal_year": values["fiscal_year"],
            "fiscal_quarter": values["fiscal_quarter"],
            "period_kind": period_kind,
            "period_kind_evidence": values.get("period_kind_evidence") if period_kind != "unknown" else None,
            "reviewed_at": reviewed_at,
            "verified_at": reviewed_at if period_kind != "unknown" else None,
        }] if period_kind is not None else []),
    ]
    event.fiscal_year, event.fiscal_quarter = values["fiscal_year"], values["fiscal_quarter"]
    event.announced_date, event.announced_at, event.time_precision = day, stamp, precision
    event.verified = precision != "conflict"
    event.is_estimate = day > now().astimezone(ET).date()
    event.status = (
        "VERIFIED"
        if event.verified and precision in {"exact", "before_open", "after_close"}
        else "DATE_VERIFIED"
        if event.verified
        else "CONFLICT"
    )
    if not is_primary:
        event.status = "SECONDARY"
    _bump(event)
    from iirp.business_models import AnalysisRequest, Batch, RequestScope
    from iirp.lifecycle import _plan_compute

    for scope in s.scalars(
        select(RequestScope).where(RequestScope.security_id == event.security_id)
    ):
        batch = s.get(Batch, scope.batch_id)
        if (
            batch.requested_action is None
            and batch.status not in {"PAUSED", "CANCELLED", "PAUSE_REQUESTED", "CANCEL_REQUESTED"}
            and s.scalar(select(AnalysisRequest.id).where(AnalysisRequest.batch_id == batch.id))
        ):
            _plan_compute(s, scope, batch, s.get(Security, event.security_id))
            batch.status = "RUNNING"
    return {"items": [event_dict(event)], "data": {"history_preserved": True}}


def read_events(s, ticker=""):
    query = select(EarningsEvent, Security.symbol).join(
        Security, Security.id == EarningsEvent.security_id
    )
    if ticker:
        query = query.where(Security.symbol == ticker.upper())
    return {
        "items": [
            {**event_dict(event), "symbol": symbol}
            for event, symbol in s.execute(
                query.order_by(EarningsEvent.announced_date.desc()).limit(500)
            )
        ],
        "data": {
            "notice": "候选日期、已核对财季与精确公告时间分别记录；每个反应窗口独立判断成熟度。"
        },
    }
