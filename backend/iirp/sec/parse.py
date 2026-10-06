"""Validate SEC URLs and parse EDGAR responses: Atom, indexes, filing pages, submissions."""

import base64
import json
import re
from datetime import date, datetime
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
