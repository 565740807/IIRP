"""SEC discovery and document adapters with an explicit, shared transport seam.

There is intentionally no HTTP client or independent rate limiter in this module.
Every external request uses the caller's ``fetch(url) -> bytes`` callback. The caller
must reserve the global SEC budget for *each* request, reject unchecked redirects,
bound response bytes/time, honor Retry-After, and fence commits. These functions do
not write files or databases. A page budget limits one operation, never history.

SEC master indexes identify filing dates and submitting entities, not acceptance
instants or the issuer's complete insider history. A Latest feed scan is complete
only after reaching a caller-supplied previously reconciled watermark. Headers
provide actual acceptance instants; missing or conflicting instants stay explicit.
"""

import base64
import json
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree.ElementTree import ParseError
from zoneinfo import ZoneInfo

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

OWNERSHIP_FORMS = frozenset({"3", "3/A", "4", "4/A", "5", "5/A"})
SEC_BASE = "https://www.sec.gov"
LATEST_URL = SEC_BASE + "/cgi-bin/browse-edgar"
TICKERS_URL = SEC_BASE + "/files/company_tickers_exchange.json"
ET = ZoneInfo("America/New_York")
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_XML_BYTES = 5 * 1024 * 1024
_ACCESSION = re.compile(r"(?<!\d)(\d{10}-\d{2}-\d{6}|\d{18})(?!\d)")
Fetch = Callable[[str], bytes]


class SecSourceError(ValueError):
    """Source format, identity, or safety validation failed; not an empty result."""


def validate_sec_url(url: str) -> str:
    """Validate before transport, including every redirect at the caller boundary."""
    if not isinstance(url, str) or re.search(r"[\x00-\x20\x7f\\]", url):
        raise SecSourceError("unsafe_sec_url")
    try:
        parts = urlsplit(url)
        allowed = (
            parts.scheme == "https"
            and parts.hostname in {"www.sec.gov", "sec.gov", "data.sec.gov"}
            and parts.port in {None, 443}
            and parts.username is None
            and parts.password is None
            and not parts.fragment
        )
    except ValueError as exc:
        raise SecSourceError("unsafe_sec_url") from exc
    # SEC paths used here need no percent escapes. Reject encoded traversal and
    # path separators rather than relying on several layers decoding identically.
    if not allowed or "%" in parts.path or any(x in {".", ".."} for x in parts.path.split("/")):
        raise SecSourceError("unsafe_sec_url")
    if "//" in parts.path:
        raise SecSourceError("unsafe_sec_url")
    if parts.hostname == "data.sec.gov":
        allowed = (
            bool(re.fullmatch(r"/submissions/CIK\d{10}(?:-submissions-\d+)?\.json", parts.path))
            and not parts.query
        )
    elif parts.path == "/cgi-bin/browse-edgar":
        query = parse_qs(parts.query, keep_blank_values=True)
        allowed = (
            set(query)
            <= {"action", "owner", "count", "start", "output", "CIK", "company", "dateb", "type"}
            and query.get("action") in (["getcurrent"], ["getcompany"])
            and all(len(values) == 1 for values in query.values())
        )
    else:
        allowed = (
            bool(
                re.fullmatch(
                    r"/Archives/edgar/(?:data|daily-index|full-index)/[A-Za-z0-9_./-]+", parts.path
                )
            )
            or parts.path in {"/files/company_tickers.json", "/files/company_tickers_exchange.json"}
        ) and not parts.query
    if not allowed:
        raise SecSourceError("sec_url_outside_source_allowlist")
    host = "data.sec.gov" if parts.hostname == "data.sec.gov" else "www.sec.gov"
    return urlunsplit(("https", host, parts.path, parts.query, ""))


def _bounded(payload: bytes, limit: int = MAX_SOURCE_BYTES) -> bytes:
    if not isinstance(payload, bytes) or len(payload) > limit:
        raise SecSourceError("source_payload_type_or_size")
    return payload


def _xml(payload: bytes):
    try:
        root = SafeET.fromstring(_bounded(payload, MAX_XML_BYTES), forbid_dtd=True)
    except (ParseError, DefusedXmlException, ValueError) as exc:
        raise SecSourceError("invalid_or_unsafe_xml") from exc
    stack = [(root, 0)]
    nodes = 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > 60_000 or depth > 64:
            raise SecSourceError("xml_node_or_depth_limit")
        stack.extend((child, depth + 1) for child in node)
    return root


def _local(element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _text(element, name: str) -> str | None:
    child = next((c for c in element if _local(c) == name), None)
    return (child.text or "").strip() or None if child is not None else None


def _decode(payload: bytes) -> str:
    payload = _bounded(payload)
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Older SEC indexes and submissions can contain Latin-1 company names.
        # Source records below still retain original bytes exactly.
        return payload.decode("latin-1")


def _encoded(payload: bytes) -> tuple[str, str]:
    try:
        return "utf8", payload.decode("utf-8")
    except UnicodeDecodeError:
        return "base64", base64.b64encode(payload).decode("ascii")


def _cik(value) -> str:
    value = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,10}", value) or int(value) == 0:
        raise SecSourceError("invalid_cik")
    return value.zfill(10)


def _accession(value: str) -> str:
    matches = _ACCESSION.findall(value)
    canonical = {x if "-" in x else f"{x[:10]}-{x[10:12]}-{x[12:]}" for x in matches}
    if len(canonical) != 1:
        raise SecSourceError("missing_or_conflicting_accession")
    return canonical.pop()


def _form(raw: str | None) -> str | None:
    raw = (raw or "").strip().upper()
    # SEC documentType occasionally uses 4A; canonical external form is 4/A.
    if re.fullmatch(r"[345]A", raw):
        raw = raw[0] + "/A"
    return raw if raw in OWNERSHIP_FORMS else None


def _date(raw: str | None) -> str | None:
    if not raw:
        return None
    if re.fullmatch(r"\d{8}", raw):
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.isoformat() == raw else None


def _instant(raw: str | None, *, eastern: bool = False) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if eastern and re.fullmatch(r"\d{14}", raw):
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}T{raw[8:10]}:{raw[10:12]}:{raw[12:]}"
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?", raw
    ):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            if not eastern:
                return None
            first, second = parsed.replace(tzinfo=ET, fold=0), parsed.replace(tzinfo=ET, fold=1)
            if first.utcoffset() != second.utcoffset():
                return None  # DST conflict is not silently guessed.
            parsed = first
        return parsed.isoformat()
    except ValueError:
        return None


def _filing_urls(url: str, accession: str) -> dict:
    url = validate_sec_url(url)
    path = urlsplit(url).path
    match = re.fullmatch(r"/Archives/edgar/data/(\d{1,10})/(?:(\d{18})/)?([^/]+)", path)
    if match is None or _accession(path) != accession:
        raise SecSourceError("invalid_filing_url_or_accession")
    cik, compact, _ = match.groups()
    if compact and compact != accession.replace("-", ""):
        raise SecSourceError("filing_directory_accession_conflict")
    base = f"{SEC_BASE}/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"
    return {
        "index_url": base + accession + "-index.html",
        "submission_url": base + accession + ".txt",
        "filing_directory": base,
        "entity_cik": _cik(cik),
    }


def _merge_entries(entries: list[dict]) -> list[dict]:
    result: dict[str, dict] = {}
    for incoming in entries:
        key = incoming["accession"]
        if key not in result:
            result[key] = incoming
            continue
        current = result[key]
        if current["form"] != incoming["form"]:
            raise SecSourceError("accession_form_conflict")
        for field in ("entities", "raw_titles", "index_urls", "warnings"):
            for value in incoming.get(field, []):
                if value not in current.setdefault(field, []):
                    current[field].append(value)
        current["issuer_cik"] = current.get("issuer_cik") or incoming.get("issuer_cik")
        left, right = current.get("accepted_at"), incoming.get("accepted_at")
        if left and right and datetime.fromisoformat(left) != datetime.fromisoformat(right):
            current["accepted_at"] = None
            current["warnings"].append("acceptance_time_conflict")
        elif right and "acceptance_time_conflict" not in current.get("warnings", []):
            current["accepted_at"] = right
    return list(result.values())


def _atom(payload: bytes) -> tuple[list[dict], int, str | None]:
    root = _xml(payload)
    if root.tag != "{http://www.w3.org/2005/Atom}feed":
        raise SecSourceError("expected_atom_feed")
    entries, raw_count = [], 0
    next_url = None
    for child in root:
        if _local(child) == "link" and child.get("rel") == "next":
            next_url = validate_sec_url(urljoin(SEC_BASE, child.get("href", "")))
        if _local(child) != "entry":
            continue
        raw_count += 1
        title = _text(child, "title") or ""
        category_forms = {
            form for c in child if _local(c) == "category" and (form := _form(c.get("term")))
        }
        title_form = _form(title.split(" - ", 1)[0])
        if len(category_forms) > 1 or (
            category_forms and title_form and title_form not in category_forms
        ):
            raise SecSourceError("feed_form_conflict")
        form = next(iter(category_forms), title_form)
        if form is None:
            continue
        links = [
            c.get("href", "")
            for c in child
            if _local(c) == "link" and c.get("rel", "alternate") == "alternate"
        ]
        candidates = [x for x in links if re.search(r"-index\.html?(?:$|\?)", x)]
        if not candidates:
            raise SecSourceError("ownership_entry_missing_index_url")
        index_url = validate_sec_url(urljoin(SEC_BASE, candidates[0]))
        accession = _accession((_text(child, "id") or "") + " " + index_url)
        urls = _filing_urls(index_url, accession)
        match = re.search(r"\((\d{1,10})\)\s*\(([^)]+)\)\s*$", title)
        entity_cik = _cik(match.group(1)) if match else urls["entity_cik"]
        role = match.group(2) if match else "unknown"
        entity = {
            "cik": entity_cik,
            "role": role,
            "name": title.split(" - ", 1)[-1].split(f"({match.group(1)})", 1)[0].strip()
            if match
            else None,
            "url": LATEST_URL + "?" + urlencode({"action": "getcompany", "CIK": entity_cik}),
        }
        accepted = _instant(_text(child, "updated"))
        summary = unescape(_text(child, "summary") or "")
        filed = re.search(r"Filed:\s*(?:</?[^>]+>\s*)*(\d{4}-\d{2}-\d{2})", summary, re.I)
        entries.append(
            {
                "accession": accession,
                "form": form,
                "accepted_at": accepted,
                "accepted_at_source": "sec_atom_updated" if accepted else None,
                "filing_date": _date(filed.group(1)) if filed else None,
                "entity_cik": entity_cik,
                "issuer_cik": entity_cik if role.lower() == "issuer" else None,
                "entities": [entity],
                "index_url": index_url,
                "index_urls": [index_url],
                "submission_url": urls["submission_url"],
                "raw_title": title,
                "raw_titles": [title],
                "warnings": [] if accepted else ["acceptance_time_missing_or_invalid"],
            }
        )
    return _merge_entries(entries), raw_count, next_url


def parse_atom(payload: bytes) -> list[dict]:
    """Return deduplicated Ownership entries, preserving issuer/reporting labels."""
    return _atom(payload)[0]


def parse_index(payload: bytes) -> list[dict]:
    """Parse a daily or quarterly master.idx; its CIK need not be the issuer."""
    lines = _decode(payload).splitlines()
    header = next(
        (
            i
            for i, line in enumerate(lines)
            if [v.strip().lower() for v in line.split("|")]
            == ["cik", "company name", "form type", "date filed", "filename"]
        ),
        None,
    )
    if header is None:
        raise SecSourceError("expected_sec_master_index")
    entries = []
    for line in lines[header + 1 :]:
        if not line.strip() or set(line.strip()) == {"-"}:
            continue
        parts = [x.strip() for x in line.split("|")]
        if len(parts) != 5:
            raise SecSourceError("malformed_master_index_row")
        raw_cik, name, raw_form, filed, path = parts
        form = _form(raw_form)
        if form is None:
            continue
        cik, filing_date = _cik(raw_cik), _date(filed)
        if filing_date is None or not re.fullmatch(
            r"edgar/data/\d{1,10}/(?:\d{18}/)?\d{10}-\d{2}-\d{6}\.txt", path
        ):
            raise SecSourceError("invalid_master_index_date_or_path")
        accession = _accession(path)
        source_url = SEC_BASE + "/Archives/" + path
        urls = _filing_urls(source_url, accession)
        if urls["entity_cik"] != cik:
            raise SecSourceError("master_index_cik_path_conflict")
        entries.append(
            {
                "accession": accession,
                "form": form,
                "accepted_at": None,
                "accepted_at_source": None,
                "filing_date": filing_date,
                "entity_cik": cik,
                "issuer_cik": None,
                "entities": [{"cik": cik, "name": name, "role": "index_entity"}],
                "index_url": urls["index_url"],
                "index_urls": [urls["index_url"]],
                "submission_url": source_url,
                "raw_title": name,
                "raw_titles": [name],
                "warnings": ["acceptance_time_not_in_index"],
            }
        )
    return _merge_entries(entries)


class _IndexHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.tokens, self.links = [], [], []
        self._row, self._cell = None, None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = {"text": "", "links": []}
        elif tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
            if self._cell is not None:
                self._cell["links"].append(attrs["href"])

    def handle_data(self, data):
        if data.strip():
            self.tokens.append(data.strip())
            if self._cell is not None:
                self._cell["text"] += data

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self._cell is not None:
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row, self._cell = None, None


def _same_filing_document(url: str, directory: str) -> str:
    url = validate_sec_url(url)
    if not url.startswith(directory):
        raise SecSourceError("document_outside_filing_directory")
    suffix = url[len(directory) :]
    # Styled XML links serve a browser transform, not the actual document URL.
    suffix = re.sub(r"^xsl[A-Za-z0-9_-]+/", "", suffix)
    if not suffix or "/" in suffix:
        raise SecSourceError("invalid_filing_document_path")
    return directory + suffix


def parse_filing_index(payload: bytes, source_url: str) -> dict:
    accession = _accession(source_url)
    urls = _filing_urls(source_url, accession)
    parser = _IndexHTML()
    parser.feed(_decode(payload))
    document_urls, submission_url = [], None
    for row in parser.rows:
        if not any(_form(cell["text"]) for cell in row):
            continue
        for cell in row:
            for href in cell["links"]:
                if urlsplit(href).path.lower().endswith(".xml"):
                    url = _same_filing_document(urljoin(source_url, href), urls["filing_directory"])
                    if url not in document_urls:
                        document_urls.append(url)
    for href in parser.links:
        if urlsplit(href).path.endswith(accession + ".txt"):
            candidate = validate_sec_url(urljoin(source_url, href))
            _filing_urls(candidate, accession)
            submission_url = candidate
            break
    accepted, filing_date = None, None
    for position, token in enumerate(parser.tokens[:-1]):
        if token == "Accepted":
            accepted = _instant(parser.tokens[position + 1], eastern=True)
        elif token == "Filing Date":
            filing_date = _date(parser.tokens[position + 1])
    if not document_urls and not submission_url:
        raise SecSourceError("filing_index_missing_ownership_documents")
    return {
        "accession": accession,
        "accepted_at": accepted,
        "accepted_at_source": "sec_filing_index" if accepted else None,
        "filing_date": filing_date,
        "document_urls": document_urls,
        "submission_url": submission_url or urls["submission_url"],
    }


def _ownership_fragment(text: bytes) -> bytes:
    # Preserve source bytes; never serialize a parsed tree as original evidence.
    opening = re.search(rb"<(?:[A-Za-z_][\w.-]*:)?ownershipDocument(?:\s[^>]*|)>", text)
    closing = re.search(rb"</(?:[A-Za-z_][\w.-]*:)?ownershipDocument\s*>", text)
    if not opening or not closing or closing.start() <= opening.start():
        raise SecSourceError("ownership_document_missing_or_truncated")
    start = opening.start()
    declaration = re.search(rb"<\?xml\s[^?]*\?>\s*$", text[:start])
    if declaration:
        start = declaration.start()
    payload = text[start : closing.end()]
    root = _xml(payload)
    if _local(root) != "ownershipDocument" or _form(_text(root, "documentType")) is None:
        raise SecSourceError("unsupported_ownership_document")
    return payload


def parse_submission(payload: bytes, *, source_url: str, accession: str | None = None) -> dict:
    """Read actual SGML acceptance header and one unambiguous Ownership XML."""
    _bounded(payload)
    expected = accession or _accession(source_url)
    urls = _filing_urls(source_url, expected)
    header_match = re.search(
        rb"<SEC-HEADER>(.*?)(?:</SEC-HEADER>|<DOCUMENT>)", payload, re.S | re.I
    )
    if not header_match:
        raise SecSourceError("sec_submission_header_missing")
    header = _decode(header_match.group(1))
    match = re.search(r"ACCESSION NUMBER:\s*(\d{10}-\d{2}-\d{6})", header)
    if not match or match.group(1) != expected:
        raise SecSourceError("submission_accession_conflict")
    form_match = re.search(r"CONFORMED SUBMISSION TYPE:\s*([^\r\n<]+)", header)
    header_form = _form(form_match.group(1)) if form_match else None
    if header_form is None:
        raise SecSourceError("unsupported_submission_form")
    accepted_match = re.search(r"<ACCEPTANCE-DATETIME>\s*(\d{14})(?!\d)", header, re.I)
    accepted = _instant(accepted_match.group(1), eastern=True) if accepted_match else None
    filed_match = re.search(r"FILED AS OF DATE:\s*(\d{8})(?!\d)", header)
    filing_date = _date(filed_match.group(1)) if filed_match else None
    candidates = []
    for document in re.findall(rb"<DOCUMENT>(.*?)</DOCUMENT>", payload, re.S | re.I):
        meta, separator, text = document.partition(b"<TEXT>")
        if not separator:
            continue
        type_match = re.search(rb"<TYPE>\s*([^\r\n<]+)", meta, re.I)
        form = _form(type_match.group(1).decode("ascii", "strict")) if type_match else None
        if form is None:
            continue
        filename_match = re.search(rb"<FILENAME>\s*([^\r\n<]+)", meta, re.I)
        if not filename_match:
            raise SecSourceError("ownership_document_filename_missing")
        filename = filename_match.group(1).decode("ascii", "strict").strip()
        document_url = _same_filing_document(
            urls["filing_directory"] + filename, urls["filing_directory"]
        )
        xml = _ownership_fragment(text)
        xml_form = _form(_text(_xml(xml), "documentType"))
        if form != xml_form or (header_form and form != header_form):
            raise SecSourceError("submission_document_form_conflict")
        candidates.append((xml, document_url, form))
    if len(candidates) != 1:
        raise SecSourceError("ownership_document_missing_or_ambiguous")
    xml, document_url, form = candidates[0]
    encoding, encoded = _encoded(xml)
    return {
        "accession": expected,
        "form": form,
        "accepted_at": accepted,
        "accepted_at_source": "sec_submission_header" if accepted else None,
        "filing_date": filing_date,
        "document_url": document_url,
        "xml_payload": encoded,
        "xml_encoding": encoding,
        "xml_sha256": sha256(xml).hexdigest(),
        "warnings": [] if accepted else ["acceptance_time_missing_or_invalid"],
    }


def _json(payload: bytes) -> dict:
    try:
        value = json.loads(_bounded(payload))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SecSourceError("invalid_sec_json") from exc
    if not isinstance(value, dict):
        raise SecSourceError("expected_sec_json_object")
    return value


def parse_company_tickers(payload: bytes) -> list[dict]:
    """Return current identity candidates, never historical security identity proof."""
    data = _json(payload)
    if "fields" in data:
        fields, rows = data.get("fields"), data.get("data")
        if (
            not isinstance(fields, list)
            or not isinstance(rows, list)
            or not {"cik", "name", "ticker", "exchange"} <= set(fields)
        ):
            raise SecSourceError("invalid_company_tickers_columns")
        if any(not isinstance(row, list) or len(row) != len(fields) for row in rows):
            raise SecSourceError("company_tickers_row_width")
        rows = [dict(zip(fields, row, strict=True)) for row in rows]
    else:
        rows = list(data.values())
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise SecSourceError("invalid_company_ticker_row")
        ticker = row.get("ticker")
        if not isinstance(ticker, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9.\-^=]{0,29}", ticker
        ):
            raise SecSourceError("invalid_company_ticker")
        result.append(
            {
                "cik": _cik(row.get("cik", row.get("cik_str"))),
                "name": row.get("name", row.get("title")),
                "ticker": ticker,
                "exchange": row.get("exchange"),
                "mapping_status": "candidate",
                "valid_from": None,
                "valid_to": None,
            }
        )
    return result


def parse_submissions(payload: bytes, *, cik: str) -> dict:
    """Parse recent or a declared older JSON file, retaining history-file pointers."""
    data, cik = _json(payload), _cik(cik)
    if "cik" in data and _cik(data["cik"]) != cik:
        raise SecSourceError("submissions_cik_conflict")
    filings = data.get("filings", {})
    columns = filings.get("recent", {}) if "filings" in data else data
    accessions = columns.get("accessionNumber")
    if not isinstance(accessions, list):
        raise SecSourceError("missing_submissions_accession_column")
    if any(
        not isinstance(values, list) or len(values) != len(accessions)
        for values in columns.values()
    ):
        raise SecSourceError("misaligned_submissions_columns")
    if not {"form", "filingDate"} <= columns.keys():
        raise SecSourceError("missing_submissions_required_columns")
    entries = []
    for i, raw_accession in enumerate(accessions):
        form = _form(columns["form"][i])
        if not form:
            continue
        accession = _accession(raw_accession)
        base = f"{SEC_BASE}/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"
        accepted = _instant(columns.get("acceptanceDateTime", [None] * len(accessions))[i])
        filing_date = _date(columns["filingDate"][i])
        if filing_date is None:
            raise SecSourceError("invalid_submissions_filing_date")
        document = columns.get("primaryDocument", [None] * len(accessions))[i]
        entries.append(
            {
                "accession": accession,
                "form": form,
                "filing_date": filing_date,
                "accepted_at": accepted,
                "accepted_at_source": "sec_submissions" if accepted else None,
                "entity_cik": cik,
                "issuer_cik": None,
                "index_url": base + accession + "-index.html",
                "submission_url": base + accession + ".txt",
                "document_url": _same_filing_document(base + document, base) if document else None,
                "warnings": [] if accepted else ["acceptance_time_missing_or_invalid"],
            }
        )
    files = []
    for declared in filings.get("files", []):
        name = declared.get("name", "")
        if not re.fullmatch(rf"CIK{cik}-submissions-\d+\.json", name):
            raise SecSourceError("unsafe_submissions_history_filename")
        files.append(
            {
                "url": "https://data.sec.gov/submissions/" + name,
                "filing_from": _date(declared.get("filingFrom")),
                "filing_to": _date(declared.get("filingTo")),
                "filing_count": declared.get("filingCount"),
            }
        )
    return {
        "entries": _merge_entries(entries),
        "files": files,
        "coverage_scope": "submitting_entity_only",
    }


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
        raise SecSourceError("unsupported_sec_discovery_mode")
    if kind == "sec_document":
        return _document(target, transport)
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
