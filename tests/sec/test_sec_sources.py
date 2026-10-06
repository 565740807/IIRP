"""Synthetic SEC transport fixtures; these tests do not claim real history coverage."""

import json
from datetime import datetime

import pytest
from iirp.sec.fetch import run_sec_operation
from iirp.sec.parse import (
    SecSourceError,
    parse_atom,
    parse_company_tickers,
    parse_filing_index,
    parse_index,
    parse_submission,
    parse_submissions,
    validate_sec_url,
)

ACCESSION = "0001493152-26-041638"
BASE = "https://www.sec.gov/Archives/edgar/data/1702924/000149315226041638/"
INDEX_URL = BASE + ACCESSION + "-index.html"
SUBMISSION_URL = BASE + ACCESSION + ".txt"
XML = b"""<?xml version="1.0"?>
<ownershipDocument><documentType>4</documentType><issuer>
<issuerCik>1702924</issuerCik><issuerName>SYNTHETIC ONLY</issuerName>
<issuerTradingSymbol>TEST</issuerTradingSymbol></issuer></ownershipDocument>"""


def atom_entry(form="4", number=41638, role="Reporting", accepted="2026-09-04T21:56:31-04:00"):
    accession = f"0001493152-26-{number:06d}"
    path = f"/Archives/edgar/data/1702924/{accession.replace('-', '')}/"
    return f"""<entry><id>urn:tag:sec.gov,2008:accession-number={accession}</id>
    <title>{form} - SYNTHETIC ONLY (0001702924) ({role})</title>
    <category scheme="https://www.sec.gov/" term="{form}"/>
    <updated>{accepted}</updated><link rel="alternate" type="text/html"
    href="https://www.sec.gov{path}{accession}-index.htm"/>
    <summary type="html">&lt;b&gt;Filed:&lt;/b&gt; 2026-09-04</summary></entry>"""


def atom(*entries, next_url=None):
    link = f'<link rel="next" href="{next_url.replace("&", "&amp;")}"/>' if next_url else ""
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom">' + link + "".join(entries) + "</feed>"
    ).encode()


def master(*rows):
    return (
        "Description: Master Index of EDGAR Dissemination Feed\n"
        "Last Data Received: September 04, 2026\n"
        "CIK|Company Name|Form Type|Date Filed|Filename\n" + "-" * 80 + "\n" + "\n".join(rows)
    ).encode()


def submission(xml=XML, acceptance="20260904215631", form="4", extra=""):
    return (
        (
            f"<SEC-DOCUMENT>{ACCESSION}.txt : 20260904\n<SEC-HEADER>\n"
            f"<ACCEPTANCE-DATETIME>{acceptance}\nACCESSION NUMBER: {ACCESSION}\n"
            f"CONFORMED SUBMISSION TYPE: {form}\nFILED AS OF DATE: 20260904\n</SEC-HEADER>\n"
            f"<DOCUMENT>\n<TYPE>{form}\n<SEQUENCE>1\n<FILENAME>ownership.xml\n<TEXT>\n<XML>\n"
        ).encode()
        + xml
        + b"\n</XML>\n</TEXT>\n</DOCUMENT>\n"
        + extra.encode()
    )


def index_html(accepted="2026-09-04 21:56:31"):
    return f"""<html><div class="infoHead">Accepted</div><div class="info">{accepted}</div>
    <div class="infoHead">Filing Date</div><div class="info">2026-09-04</div>
    <table class="tableFile"><tr><th>Seq</th><th>Description</th><th>Document</th>
    <th>Type</th><th>Size</th></tr><tr><td>1</td><td>PRIMARY DOCUMENT</td>
    <td><a href="xslF345X05/ownership.xml">ownership.xml</a></td><td>4</td><td>200</td></tr>
    <tr><td></td><td>Complete submission text file</td>
    <td><a href="{ACCESSION}.txt">{ACCESSION}.txt</a></td><td></td><td>500</td></tr>
    </table></html>""".encode()


def test_atom_accepts_exact_six_forms_and_deduplicates_entities():
    payload = atom(
        *[
            atom_entry(form, 41638 + i)
            for i, form in enumerate(["3", "3/A", "4", "4/A", "5", "5/A"])
        ],
        atom_entry("3", 41638, "Issuer"),
        atom_entry("144", 41999),
    )
    entries = parse_atom(payload)
    assert [entry["form"] for entry in entries] == ["3", "3/A", "4", "4/A", "5", "5/A"]
    assert len(entries[0]["entities"]) == 2
    assert entries[0]["issuer_cik"] == "0001702924"
    assert entries[1]["issuer_cik"] is None
    assert entries[0]["accepted_at"] == "2026-09-04T21:56:31-04:00"
    assert entries[0]["filing_date"] == "2026-09-04"
    assert entries[0]["submission_url"].endswith("/0001493152-26-041638.txt")


def test_atom_missing_time_stays_unknown_and_date_is_not_an_instant():
    entry = parse_atom(atom(atom_entry(accepted="2026-09-04")))[0]
    assert entry["accepted_at"] is None
    assert "acceptance_time_missing_or_invalid" in entry["warnings"]


@pytest.mark.parametrize(
    "payload",
    [b"<feed>", b"<html>Forbidden</html>", b'<!DOCTYPE feed [<!ENTITY x "bad">]><feed>&x;</feed>'],
)
def test_corrupt_or_unsafe_feed_is_not_an_empty_success(payload):
    with pytest.raises(SecSourceError):
        parse_atom(payload)


def test_master_index_keeps_filed_date_separate_and_supports_path_variants():
    rows = []
    for i, form in enumerate(["3", "3/A", "4", "4/A", "5", "5/A"]):
        acc = f"0001493152-26-{41638 + i:06d}"
        path = (
            f"edgar/data/1702924/{acc}.txt"
            if i % 2
            else f"edgar/data/1702924/{acc.replace('-', '')}/{acc}.txt"
        )
        rows.append(f"1702924|SYNTHETIC ONLY|{form}|2026-09-04|{path}")
    entries = parse_index(
        master(*rows, rows[0], "1|IGNORED|10-K|2026-09-04|edgar/data/1/0000000001-26-000001.txt")
    )
    assert len(entries) == 6
    assert all(row["accepted_at"] is None for row in entries)
    assert all(row["issuer_cik"] is None for row in entries)
    assert entries[0]["filing_date"] == "2026-09-04"
    assert entries[1]["index_url"].endswith("0001493152-26-041639-index.html")


@pytest.mark.parametrize(
    "url",
    [
        "http://www.sec.gov/Archives/edgar/data/1/a.xml",
        "https://www.sec.gov.evil.test/Archives/edgar/data/1/a.xml",
        "https://www.sec.gov@evil.test/Archives/edgar/data/1/a.xml",
        "https://user@www.sec.gov/Archives/edgar/data/1/a.xml",
        "https://www.sec.gov:444/Archives/edgar/data/1/a.xml",
        "https://www.sec.gov/Archives/edgar/data/1/%2e%2e/%2e%2e/a.xml",
        "https://www.sec.gov/Archives/edgar/data/1/%252e%252e/a.xml",
        "https://www.sec.gov/Archives/edgar/data/1/../../secret",
        "https://data.sec.gov/other.json",
        "https://www.sec.gov/Archives/edgar/data/1/a.xml?url=https://evil.test",
        "https://www.sec.gov/Archives/edgar/data/1/a.xml#anchor",
    ],
)
def test_sec_url_allowlist_rejects_malicious_urls(url):
    with pytest.raises(SecSourceError):
        validate_sec_url(url)


def test_filing_index_resolves_xml_without_stylesheet_and_submission():
    result = parse_filing_index(index_html(), INDEX_URL)
    assert result["document_urls"] == [BASE + "ownership.xml"]
    assert result["submission_url"] == SUBMISSION_URL
    assert result["accepted_at"] == "2026-09-04T21:56:31-04:00"


def test_full_submission_uses_real_header_and_preserves_original_xml_bytes():
    result = parse_submission(submission(), source_url=SUBMISSION_URL, accession=ACCESSION)
    assert result["xml_payload"].encode() == XML
    assert result["accepted_at"] == "2026-09-04T21:56:31-04:00"
    assert result["accepted_at_source"] == "sec_submission_header"
    assert result["document_url"] == BASE + "ownership.xml"
    assert result["form"] == "4"


def test_header_time_uses_winter_offset_and_missing_header_is_not_inferred():
    winter = parse_submission(submission(acceptance="20260102183000"), source_url=SUBMISSION_URL)
    assert winter["accepted_at"] == "2026-01-02T18:30:00-05:00"
    missing = parse_submission(submission(acceptance=""), source_url=SUBMISSION_URL)
    assert missing["accepted_at"] is None
    assert missing["filing_date"] == "2026-09-04"


def test_corrupt_ownership_xml_and_accession_conflict_fail_explicitly():
    with pytest.raises(SecSourceError):
        parse_submission(submission(xml=b"<ownershipDocument>"), source_url=SUBMISSION_URL)
    with pytest.raises(SecSourceError):
        parse_submission(submission(), source_url=SUBMISSION_URL, accession="0000000001-26-000001")


def test_document_operation_only_uses_injected_fetch_and_keeps_both_sources():
    visited = []

    def fetch(url):
        visited.append(url)
        return {INDEX_URL: index_html(), SUBMISSION_URL: submission()}[url]

    result = run_sec_operation(
        "sec_document", {"index_url": INDEX_URL, "accession": ACCESSION}, fetch=fetch
    )
    assert visited == [INDEX_URL, SUBMISSION_URL]
    assert result["scan"]["complete"]
    assert result["filing"]["accepted_at_source"] == "sec_submission_header"
    assert len(result["source_documents"]) == 2
    assert json.loads(json.dumps(result))["filing"]["xml_payload"].encode() == XML


def test_missing_full_submission_falls_back_to_real_index_and_xml():
    class Missing(Exception):
        status_code = 404

    visited = []

    def fetch(url):
        visited.append(url)
        if url == SUBMISSION_URL:
            raise Missing()
        return {INDEX_URL: index_html(), BASE + "ownership.xml": XML}[url]

    result = run_sec_operation(
        "sec_document",
        {"accession": ACCESSION, "index_url": INDEX_URL, "submission_url": SUBMISSION_URL},
        fetch=fetch,
    )
    assert visited == [SUBMISSION_URL, INDEX_URL, BASE + "ownership.xml"]
    assert result["filing"]["accepted_at_source"] == "sec_filing_index"
    assert result["filing"]["warnings"] == ["full_submission_unavailable"]


def test_feed_page_budget_returns_cursor_and_repeated_page_stays_incomplete():
    payload = atom(atom_entry(), atom_entry("4", 41637))
    result = run_sec_operation("sec_discover", {"page_size": 2}, fetch=lambda url: payload)
    assert not result["scan"]["complete"]
    assert result["scan"]["reason"] == "page_budget"
    assert result["cursor"]["start"] == 2
    resumed = run_sec_operation(
        "sec_discover", {"page_size": 2, "cursor": result["cursor"]}, fetch=lambda url: payload
    )
    assert not resumed["scan"]["complete"]
    assert resumed["scan"]["reason"] == "repeated_page"


def test_feed_short_page_is_only_feed_exhaustion_not_full_history():
    result = run_sec_operation("sec_discover", {}, fetch=lambda url: atom(atom_entry()))
    assert not result["scan"]["complete"]
    assert result["scan"]["reason"] == "feed_exhausted_before_watermark"
    assert result["cursor"] is None


def test_feed_reaching_known_continuous_watermark_closes_requested_scan():
    result = run_sec_operation(
        "sec_discover",
        {"watermark": "2026-09-04T21:57:00-04:00"},
        fetch=lambda url: atom(atom_entry()),
    )
    assert result["scan"]["complete"]
    assert result["scan"]["reason"] == "watermark_reached"


def test_daily_missing_index_retains_gap_and_stops_before_later_day():
    class NotReady(Exception):
        status_code = 404

    visited = []

    def fetch(url):
        visited.append(url)
        raise NotReady()

    result = run_sec_operation(
        "sec_discover",
        {"mode": "daily", "start_date": "2026-09-04", "end_date": "2026-09-07"},
        fetch=fetch,
    )
    assert len(visited) == 1
    assert "master.20260904.idx" in visited[0]
    assert result["scan"]["reason"] == "index_not_ready"
    assert not result["scan"]["complete"]
    assert result["cursor"]["date"] == "2026-09-04"


def test_quarterly_filters_date_range_and_round_trips_continuation():
    payload = master(
        f"1702924|SYNTHETIC|4|2026-06-30|edgar/data/1702924/{ACCESSION}.txt",
        "1702924|SYNTHETIC|4|2026-04-01|edgar/data/1702924/0001493152-26-000002.txt",
    )
    result = run_sec_operation(
        "sec_discover",
        {"mode": "quarterly", "start_date": "2026-06-01", "end_date": "2026-07-31"},
        fetch=lambda url: payload,
    )
    assert len(result["entries"]) == 1
    assert not result["scan"]["complete"]
    assert result["cursor"]["quarter"] == "2026-Q3"
    end = run_sec_operation(
        "sec_discover",
        {
            "mode": "quarterly",
            "start_date": "2026-06-01",
            "end_date": "2026-07-31",
            "cursor": result["cursor"],
        },
        fetch=lambda url: master(),
    )
    assert end["scan"]["complete"]


def test_current_quarter_cannot_claim_dates_after_index_actual_waterline():
    result = run_sec_operation(
        "sec_discover",
        {"mode": "quarterly", "start_date": "2026-09-01", "end_date": "2026-09-08"},
        fetch=lambda url: master(),
    )
    assert not result["scan"]["complete"]
    assert result["scan"]["reason"] == "index_not_current_to_end"
    assert result["scan"]["scanned_indexes"][0]["as_of"] == "2026-09-04"


def test_missing_index_publication_waterline_is_an_explicit_gap():
    result = run_sec_operation(
        "sec_discover",
        {"mode": "quarterly", "start_date": "2026-09-01", "end_date": "2026-09-04"},
        fetch=lambda url: master().replace(b"Last Data Received: September 04, 2026\n", b""),
    )
    assert not result["scan"]["complete"]
    assert result["scan"]["reason"] == "index_as_of_unknown"


def test_stale_first_quarter_remains_a_gap_after_later_quarter_continuation():
    target = {"mode": "quarterly", "start_date": "2026-06-01", "end_date": "2026-07-31"}
    old = master().replace(b"September 04, 2026", b"June 01, 2026")
    first = run_sec_operation("sec_discover", target, fetch=lambda url: old)
    last = run_sec_operation(
        "sec_discover", {**target, "cursor": first["cursor"]}, fetch=lambda url: master()
    )
    assert not last["scan"]["complete"]
    assert last["scan"]["reason"] == "index_not_current_to_end"


def test_company_candidates_are_not_verified_security_mappings():
    result = parse_company_tickers(
        json.dumps(
            {
                "fields": ["cik", "name", "ticker", "exchange"],
                "data": [
                    [320193, "Apple Inc.", "AAPL", "Nasdaq"],
                    [1067983, "Berkshire Hathaway", "BRK-B", "NYSE"],
                ],
            }
        ).encode()
    )
    assert result[0]["cik"] == "0000320193"
    assert result[0]["mapping_status"] == "candidate"
    assert result[1]["ticker"] == "BRK-B"


def test_submissions_traverse_declared_older_files_without_claiming_issuer_coverage():
    result = parse_submissions(
        json.dumps(
            {
                "cik": "1702924",
                "filings": {
                    "recent": {
                        "accessionNumber": [ACCESSION],
                        "filingDate": ["2026-09-04"],
                        "acceptanceDateTime": ["2026-09-05T01:56:31Z"],
                        "form": ["4"],
                        "primaryDocument": ["ownership.xml"],
                    },
                    "files": [
                        {
                            "name": "CIK0001702924-submissions-001.json",
                            "filingFrom": "2020-01-01",
                            "filingTo": "2024-01-01",
                        }
                    ],
                },
            }
        ).encode(),
        cik="1702924",
    )
    assert (
        result["files"][0]["url"]
        == "https://data.sec.gov/submissions/CIK0001702924-submissions-001.json"
    )
    assert result["coverage_scope"] == "submitting_entity_only"
    assert (
        datetime.fromisoformat(result["entries"][0]["accepted_at"]).utcoffset().total_seconds() == 0
    )


def test_submissions_misaligned_columns_are_not_silently_truncated():
    with pytest.raises(SecSourceError):
        parse_submissions(
            json.dumps({"accessionNumber": [ACCESSION], "form": []}).encode(), cik="1702924"
        )
