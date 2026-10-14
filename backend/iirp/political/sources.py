"""Discover public political disclosures from official indexes.

The caller owns the HTTP session, contact User-Agent, persisted request budget and
source-object retention. No transport or retry loop lives in this module.
"""

import csv
import hashlib
import io
import re
import zipfile
from collections.abc import Callable
from datetime import date, datetime
from html import unescape
from typing import Any
from urllib.parse import unquote, urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

HOUSE_INDEX = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
SENATE_INDEX = "https://efdsearch.senate.gov/search/"
OGE_INDEX = (
    "https://www.oge.gov/web/oge.nsf/Officials%20Individual%20Disclosures%20Search%20Collection"
)
WHITE_HOUSE_INDEX = "https://www.whitehouse.gov/disclosures/"

Report = dict[str, Any]
Fetch = Callable[..., httpx.Response]
Retain = Callable[[str, bytes, str], Any]


class SourceUnavailable(Exception):
    """An explicit source outcome; the caller translates the reason token."""

    def __init__(self, source: str, reason: str, status_code: int | None = None):
        super().__init__(reason)
        self.source = source
        self.reason = reason
        self.status_code = status_code


def official_url(url: str, source: str) -> bool:
    parsed = urlsplit(url)
    hosts = {
        "house": {"disclosures-clerk.house.gov"},
        "senate": {"efdsearch.senate.gov"},
        "oge": {"www.oge.gov", "oge.gov", "www2.oge.gov", "extapps2.oge.gov",
                "www.whitehouse.gov", "whitehouse.gov", "oge.box.com"},
    }
    return parsed.scheme == "https" and parsed.hostname in hosts[source] and not parsed.username


def _read(fetch: Fetch, source: str, url: str, retain: Retain | None, *,
          method: str = "GET", data: dict | None = None) -> httpx.Response:
    if not official_url(url, source):
        raise SourceUnavailable(source, "unofficial_link")
    for _ in range(6):
        response = fetch(method, url, data=data)
        if retain is not None:
            retain(url, response.content,
                   response.headers.get("content-type", "application/octet-stream"))
        if response.status_code not in {301, 302, 303, 307, 308}:
            break
        url = urljoin(url, response.headers.get("location", ""))
        if not official_url(url, source):
            raise SourceUnavailable(source, "unofficial_link")
        if response.status_code in {301, 302, 303}:
            method, data = "GET", None
    else:
        raise SourceUnavailable(source, "redirect_loop")
    if response.status_code in {401, 403, 429}:
        raise SourceUnavailable(source, "blocked", response.status_code)
    if response.status_code >= 400:
        raise SourceUnavailable(source, "http_error", response.status_code)
    if not official_url(str(response.url), source):
        raise SourceUnavailable(source, "unofficial_link")
    return response


def _date(value: str) -> date | None:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m.%d.%y", "%m.%d.%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def house_index(content: bytes, year: int) -> list[Report]:
    """Use receipt dates from the annual TSV, never dates encoded in DocIDs."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = [item for item in archive.infolist()
                       if item.filename.lower().endswith(f"{year}fd.txt")]
            if len(members) != 1 or members[0].file_size > 32 * 1024 * 1024:
                raise SourceUnavailable("house", "index_changed")
            text = archive.read(members[0]).decode("utf-8-sig")
    except (zipfile.BadZipFile, UnicodeError, RuntimeError) as exc:
        raise SourceUnavailable("house", "index_changed") from exc
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    required = {"Last", "First", "FilingType", "StateDst", "FilingDate", "DocID"}
    if not required.issubset(reader.fieldnames or []):
        raise SourceUnavailable("house", "index_changed")
    reports = {}
    for row in reader:
        if row["FilingType"].strip() != "P":
            continue
        doc_id = row["DocID"].strip()
        filed = _date(row["FilingDate"])
        if not re.fullmatch(r"\d{7,8}", doc_id) or filed is None:
            raise SourceUnavailable("house", "index_changed")
        reports[doc_id] = {
            "external_id": doc_id,
            "name": " ".join(part.strip() for part in (row["First"], row["Last"],
                                row.get("Suffix", "")) if part.strip()),
            "district": row["StateDst"].strip() or None,
            "position": "representative",
            "filed_date": filed.isoformat(),
            "url": f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf",
            "amendment_status": "original",
        }
    return list(reports.values())


def _house(fetch: Fetch, today: date, retain: Retain | None) -> list[Report]:
    reports = []
    for year in range(2025, today.year + 1):
        response = _read(fetch, "house", HOUSE_INDEX.format(year=year), retain)
        reports.extend(house_index(response.content, year))
    return reports


def _links(html: str, base: str, source: str):
    for anchor in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        url = urljoin(base, anchor["href"])
        if official_url(url, source):
            yield anchor, url


def _oge_report(anchor, url: str) -> Report | None:
    text = anchor.get_text(" ", strip=True)
    filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    context = " ".join((text, filename))
    if not re.search(r"periodic.?transaction|278[\s_-]*t", context, re.I):
        return None
    dates = re.findall(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", context)
    filed = next((parsed for value in dates if (parsed := _date(value)) is not None), None)
    if filed is None or filed < date(2025, 1, 20):
        return None
    prefix = re.split(r"periodic.?transaction|278[\s_-]*t", text, flags=re.I)[0]
    prefix = re.sub(r"[\s–—_-]+$", "", prefix).strip()
    if "vice president" in prefix.lower():
        position, name = "vice_president", re.sub(r"^Vice President\s+", "", prefix, flags=re.I)
    elif "president" in prefix.lower():
        position, name = "president", re.sub(r"^President\s+", "", prefix, flags=re.I)
    else:
        position, name = "executive_official", prefix
    if position == "executive_official":
        return None
    name = _name(name)
    if not name:
        return None
    amended = bool(re.search(r"amend", context, re.I))
    parts = re.findall(r"\((\d+)\)", context)
    return {
        "external_id": hashlib.sha256(url.encode()).hexdigest(),
        "name": name, "district": None, "position": position,
        "filed_date": None, "url": url,
        "document_date": filed.isoformat(), "date_source": "official_index_label",
        "document_part": int(parts[0]) if parts else None,
        "amendment_status": "unresolved" if amended else "original",
    }


def _name(value: str) -> str:
    if "," in value:
        last, first = value.split(",", 1)
        value = first.strip() + " " + last.strip()
    return " ".join(value.replace(".", "").split())


def _cabinet(title: str, agency: str) -> bool:
    if re.fullmatch(r"Secretary(?: of .+)?|Attorney General", title, re.I):
        return True
    offices = {
        "Office of Management and Budget", "Office of the Director of National Intelligence",
        "Central Intelligence Agency", "Environmental Protection Agency",
        "Small Business Administration", "Office of the United States Trade Representative",
    }
    return agency in offices and bool(re.fullmatch(
        r"Director|Administrator|United States Trade Representative|U\.S\. Trade Representative",
        title, re.I))


def _executive_position(title: str) -> str | None:
    if re.fullmatch(r"Vice President(?: of the United States)?", title, re.I):
        return "vice_president"
    if re.fullmatch(r"President(?: of the United States)?", title, re.I):
        return "president"
    return None


def _table_query(offset: int, count: int, columns: list[str]) -> dict[str, str]:
    data = {"start": str(offset), "length": str(count), "draw": str(offset // count + 1),
            "search[value]": "", "search[regex]": "false",
            "order[0][column]": "0", "order[0][dir]": "desc"}
    for index, column in enumerate(columns):
        data.update({f"columns[{index}][data]": column,
                     f"columns[{index}][name]": "",
                     f"columns[{index}][searchable]": "true",
                     f"columns[{index}][orderable]": "true",
                     f"columns[{index}][search][value]": "",
                     f"columns[{index}][search][regex]": "false"})
    return data


def _oge_api(fetch: Fetch, index: httpx.Response, retain: Retain | None, *,
             offset: int = 0, max_pages: int | None = None) -> dict:
    """Read the DataTables endpoint actually declared by the official index."""
    endpoint = re.search(r"https://extapps2\.oge\.gov/201/Presiden\.nsf/API\.xsp/[^\s\"'<>]+",
                         unescape(index.text))
    if endpoint is None:
        return {"entries": [], "cursor": None, "complete": True}
    reports = []
    pages = 0
    previous_rows = None
    while True:
        query = _table_query(offset, 100, ["docDate", "title", "type", "name", "agency", "level"])
        query["columns[2][search][value]"] = "278 Transaction"
        page = _read(fetch, "oge", endpoint[0] + "?" + urlencode(query), retain)
        try:
            body = page.json()
            rows, total = body["data"], int(body["recordsFiltered"])
            if not isinstance(rows, list) or total < 0 or any(
                    not isinstance(row, dict) for row in rows):
                raise ValueError("index")
        except (ValueError, TypeError, KeyError) as exc:
            raise SourceUnavailable("oge", "index_changed") from exc
        if rows and rows == previous_rows:
            raise SourceUnavailable("oge", "index_changed")
        previous_rows = rows
        for row in rows:
            if not re.search(r"278\s*(?:Transaction|[- ]T)\b",
                             BeautifulSoup(str(row.get("type", "")), "html.parser").get_text(), re.I):
                continue
            title = str(row.get("title", "")).strip()
            agency = str(row.get("agency", "")).strip()
            if (_executive_position(title) is None
                    and not _cabinet(title, agency)):
                continue
            added = _date(str(row.get("docDate", ""))[:10])
            if added is None:
                raise SourceUnavailable("oge", "index_changed")
            if added < date(2025, 1, 20):
                continue
            for anchor, url in _links(str(row.get("type", "")), str(index.url), "oge"):
                if not urlsplit(url).path.lower().endswith(".pdf"):
                    # OGE Form 201 requests require the requestor's real identity;
                    # collection does not invent identity or submit those forms.
                    continue
                name = _name(str(row.get("name", "")).strip())
                position = _executive_position(title) or (f"{title} ({agency})" if agency else title)
                filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
                dates = re.findall(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", filename)
                filed = next((value for raw in dates if (value := _date(raw)) is not None), None)
                amended = bool(row.get("amended")) or bool(re.search(r"amend", filename, re.I))
                parts = re.findall(r"\((\d+)\)", filename)
                reports.append({"external_id": hashlib.sha256(url.encode()).hexdigest(),
                                "name": name, "district": None, "position": position,
                                "filed_date": None,
                                "document_date": filed.isoformat() if filed else None,
                                "date_source": "attachment_filename",
                                "document_part": int(parts[0]) if parts else None,
                                "url": url, "index_date": added.isoformat(),
                                "amendment_status": "unresolved" if amended else "original"})
        offset += len(rows)
        pages += 1
        if offset >= total:
            return {"entries": reports, "cursor": None, "complete": True}
        if not rows:
            raise SourceUnavailable("oge", "index_changed")
        if max_pages is not None and pages >= max_pages:
            return {"entries": reports, "cursor": {"offset": offset}, "complete": False}


def _oge(fetch: Fetch, today: date, retain: Retain | None, *,
         cursor: dict | None = None, max_pages: int | None = None) -> dict:
    """Follow linked report indexes. Never synthesize attachment addresses."""
    del today
    reports: dict[str, Report] = {}
    errors = []
    api_used = False
    outcome = {"cursor": None, "complete": True}
    for initial in (WHITE_HOUSE_INDEX, OGE_INDEX):
        try:
            response = _read(fetch, "oge", initial, retain)
        except Exception as exc:
            if not isinstance(exc, (SourceUnavailable, httpx.HTTPError)) and not hasattr(
                    exc, "retry_seconds"):
                raise
            errors.append(exc)
            continue
        if initial == OGE_INDEX:
            api_used = bool(re.search(r"https://extapps2\.oge\.gov/201/Presiden\.nsf/API\.xsp/",
                                      unescape(response.text)))
            if not api_used:
                errors.append(SourceUnavailable("oge", "index_changed"))
            try:
                outcome = _oge_api(fetch, response, retain, offset=(cursor or {}).get("offset", 0),
                                   max_pages=max_pages)
            except Exception as exc:
                if not isinstance(exc, (SourceUnavailable, httpx.HTTPError)) and not hasattr(
                        exc, "retry_seconds"):
                    raise
                errors.append(exc)
                outcome = {"entries": [], "cursor": cursor, "complete": False}
            for report in outcome["entries"]:
                reports[report["url"]] = report
        queue = [(str(response.url), response.text, 0)]
        visited = {str(response.url)}
        while queue:
            base, html, depth = queue.pop(0)
            for anchor, url in _links(html, base, "oge"):
                if urlsplit(url).path.lower().endswith(".pdf"):
                    report = _oge_report(anchor, url)
                    if report is not None:
                        reports[url] = report
                elif depth < 2 and url not in visited and re.search(
                    r"PAS[+% ]|Presiden\.nsf|disclosure.*(?:president|vice)|(?:president|vice).*disclosure",
                    url + " " + anchor.get_text(" ", strip=True), re.I,
                ) and "request" not in url.lower():
                    visited.add(url)
                    linked = _read(fetch, "oge", url, retain)
                    queue.append((str(linked.url), linked.text, depth + 1))
    if not reports and outcome["complete"] and not api_used:
        if errors:
            raise errors[0]
        raise SourceUnavailable("oge", "index_changed")
    # A named amendment is linked only when its index identifies a unique original.
    for report in reports.values():
        if report["amendment_status"] != "unresolved":
            continue
        originals = [other for other in reports.values()
                     if other["name"] == report["name"]
                     and other.get("document_date") == report.get("document_date")
                     and report.get("document_date") is not None
                     and (report.get("document_part") is None
                          or other.get("document_part") == report["document_part"])
                     and other["amendment_status"] == "original"]
        if len(originals) == 1:
            report["amendment_of_external_id"] = originals[0]["external_id"]
            report["amendment_status"] = "linked"
    result = {"entries": list(reports.values()), "cursor": outcome["cursor"],
              "complete": outcome["complete"]}
    if errors:
        exc = errors[0]
        error = {"reason": getattr(exc, "reason", "http_error"),
                 "status": getattr(exc, "status_code", None)}
        if hasattr(exc, "retry_seconds"):
            # The transport's message code preserves source cooldown/contact reasons.
            error["message"] = str(exc)
        result.update(partial_error=error, complete=False)
    return result


def _senate(fetch: Fetch, today: date, retain: Retain | None, *,
            cursor: dict | None = None, max_pages: int | None = None) -> dict:
    response = _read(fetch, "senate", SENATE_INDEX, retain)
    soup = BeautifulSoup(response.text, "html.parser")
    agreement = soup.find("input", attrs={"name": "prohibition_agreement"})
    consent = agreement.find_parent("form") if agreement else None
    if consent is not None:
        data = {item["name"]: item.get("value", "") for item in
                consent.find_all("input", attrs={"type": "hidden", "name": True})}
        if not data.get("csrfmiddlewaretoken"):
            raise SourceUnavailable("senate", "session_unavailable")
        data["prohibition_agreement"] = "1"
        response = _read(fetch, "senate", urljoin(str(response.url), consent.get("action", "")),
                         retain, method="POST", data=data)
        soup = BeautifulSoup(response.text, "html.parser")
    token = soup.find("input", attrs={"name": "csrfmiddlewaretoken"})
    csrf = token.get("value", "") if token else ""
    if not csrf:
        # A standard session may supply its CSRF cookie instead of a hidden input.
        csrf = response.cookies.get("csrftoken", "")
    if not csrf:
        raise SourceUnavailable("senate", "session_unavailable")
    script_text = response.text
    endpoint = re.search(r"[\"'](/search/report/data/?)[\"']", script_text)
    if endpoint is None:
        for script in soup.find_all("script", src=True):
            url = urljoin(str(response.url), script["src"])
            if official_url(url, "senate") and "search" in url:
                script_response = _read(fetch, "senate", url, retain)
                endpoint = re.search(r"[\"'](/search/report/data/?)[\"']", script_response.text)
                if endpoint:
                    break
    if endpoint is None:
        raise SourceUnavailable("senate", "index_changed")
    reports = {}
    offset = (cursor or {}).get("offset", 0)
    pages = 0
    previous_rows = None
    while True:
        data = _table_query(offset, 100, [str(index) for index in range(5)])
        data["order[0][column]"] = "4"
        data.update({"csrfmiddlewaretoken": csrf, "report_types": "[11]",
                "filer_types": "[]", "submitted_start_date": "01/01/2025",
                "submitted_end_date": today.strftime("%m/%d/%Y"),
                "first_name": "", "last_name": "", "senator_state": "",
                "candidate_state": "", "office_id": ""})
        page = _read(fetch, "senate", urljoin(SENATE_INDEX, endpoint[1]), retain,
                     method="POST", data=data)
        try:
            body = page.json()
            rows = body["data"]
            total = int(body["recordsFiltered"])
            if not isinstance(rows, list) or total < 0:
                raise ValueError("index")
        except (ValueError, KeyError, TypeError) as exc:
            raise SourceUnavailable("senate", "index_changed") from exc
        if rows and rows == previous_rows:
            raise SourceUnavailable("senate", "index_changed")
        previous_rows = rows
        for row in rows:
            if not isinstance(row, list) or len(row) < 5:
                raise SourceUnavailable("senate", "index_changed")
            first, last = (BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
                           for value in row[:2])
            name = " ".join((first, last)).strip()
            filed = _date(BeautifulSoup(str(row[-1]), "html.parser").get_text(strip=True))
            links = list(_links(" ".join(str(value) for value in row[2:]), SENATE_INDEX, "senate"))
            for anchor, url in links:
                if not re.search(r"ptr|periodic.?transaction", anchor.get_text() + url, re.I):
                    continue
                if not name or filed is None:
                    raise SourceUnavailable("senate", "index_changed")
                reports[url] = {"external_id": urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1],
                                "name": name, "district": None, "position": "senator",
                                "filed_date": filed.isoformat(), "url": url,
                                "amendment_status": "unresolved" if re.search(r"amend", str(row), re.I)
                                else "original"}
        offset += len(rows)
        pages += 1
        if offset >= total:
            return {"entries": list(reports.values()), "cursor": None, "complete": True}
        if not rows:
            raise SourceUnavailable("senate", "index_changed")
        if max_pages is not None and pages >= max_pages:
            return {"entries": list(reports.values()), "cursor": {"offset": offset},
                    "complete": False}


def discover(source: str, fetch: Fetch, *, today: date | None = None,
             retain: Retain | None = None) -> list[Report]:
    """Return official report metadata; blocked/changed indexes are unavailable."""
    if source == "house":
        return _house(fetch, today or date.today(), retain)
    discoverer = {"senate": _senate, "oge": _oge}[source]
    return discoverer(fetch, today or date.today(), retain)["entries"]


def discover_page(source: str, fetch: Fetch, *, today: date | None = None,
                  retain: Retain | None = None, cursor: dict | None = None,
                  max_pages: int = 3) -> dict:
    """Bound a discovery operation and return a durable next-page cursor."""
    if not 1 <= max_pages <= 3:
        raise ValueError("max_pages")
    if source == "house":
        return {"entries": _house(fetch, today or date.today(), retain),
                "cursor": None, "complete": True}
    discoverer = {"senate": _senate, "oge": _oge}[source]
    return discoverer(fetch, today or date.today(), retain, cursor=cursor, max_pages=max_pages)
