"""Offline index discovery and session/error behavior."""

import io
import zipfile
from datetime import date
from pathlib import Path

import httpx
import pytest
from iirp.political.sources import (
    HOUSE_INDEX,
    OGE_INDEX,
    SENATE_INDEX,
    WHITE_HOUSE_INDEX,
    SourceUnavailable,
    discover,
    discover_page,
    house_index,
)


def zipped_tsv(text, year=2025):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{year}FD.txt", text)
        archive.writestr(f"{year}FD.xml", "<unused/>")
    return buffer.getvalue()


HOUSE_TSV = (Path(__file__).parent / "fixtures/house_index.tsv").read_text() + (
    # A synthetic annual report proves that only PTR rows are selected.
    "Hon.\tExample\tAnnual\t\tO\tCA01\t2025\t02/11/2025\t1234567\n"
)


def response(url, text="", status=200, **kwargs):
    return httpx.Response(status, text=text, request=httpx.Request("GET", url), **kwargs)


def test_house_electronic_and_scanned_reports_use_index_receipt_date():
    reports = house_index(zipped_tsv(HOUSE_TSV), 2025)
    assert len(reports) == 2
    assert reports[0]["name"] == "Nancy Pelosi"
    assert reports[0]["district"] == "CA11"
    assert reports[0]["filed_date"] == "2025-01-17"
    assert reports[0]["url"].endswith("/2025/20026590.pdf")
    assert reports[1]["name"] == "Rohit Khanna"
    assert reports[1]["filed_date"] == "2025-02-06"
    assert reports[1]["url"].endswith("/2025/8220750.pdf")
    assert reports[0]["amendment_status"] == "original"


def test_house_each_backfill_year_is_retained():
    calls, retained = [], []

    def fetch(method, url, data=None):
        calls.append(url)
        year = 2025 if "2025" in url else 2026
        return httpx.Response(200, content=zipped_tsv(HOUSE_TSV, year),
                              request=httpx.Request(method, url))

    discover("house", fetch, today=date(2026, 10, 12), retain=lambda *args: retained.append(args))
    assert calls == [HOUSE_INDEX.format(year=2025), HOUSE_INDEX.format(year=2026)]
    assert len(retained) == 2


@pytest.mark.parametrize("content", [b"not zip", zipped_tsv("invalid\theader\n")])
def test_house_bad_index_is_unavailable(content):
    with pytest.raises(SourceUnavailable, match="index_changed"):
        house_index(content, 2025)


def test_oge_follows_only_official_index_links_and_links_unique_amendment():
    original = "https://www.whitehouse.gov/wp-content/uploads/2025/08/original.pdf"
    amended = "https://www.whitehouse.gov/wp-content/uploads/2026/01/amended.pdf"
    html = f"""
      <a href='{original}'>President Donald J. Trump Periodic Transaction Report 08.12.25</a>
      <a href='{amended}'>President Donald J. Trump Periodic Transaction Report Amendment 08.12.25</a>
      <a href='https://example.net/report.pdf'>President Other Periodic Transaction Report 08.12.25</a>
      <a href='/wp-content/uploads/annual.pdf'>President Donald J. Trump Annual Report</a>
    """
    calls = []

    def fetch(method, url, data=None):
        calls.append(url)
        return response(url, html if url == WHITE_HOUSE_INDEX else "<html></html>")

    reports = discover("oge", fetch)
    assert calls == [WHITE_HOUSE_INDEX, OGE_INDEX]
    assert len(reports) == 2
    assert reports[1]["amendment_of_external_id"] == reports[0]["external_id"]
    assert reports[0]["position"] == "president"
    assert reports[0]["filed_date"] is None
    assert reports[0]["document_date"] == "2025-08-12"
    assert reports[0]["date_source"] == "official_index_label"
    assert reports[1]["amendment_status"] == "linked"


def test_oge_does_not_guess_a_report_when_no_index_links_exist():
    with pytest.raises(SourceUnavailable, match="index_changed"):
        discover("oge", lambda method, url, data=None: response(url, "<html></html>"))


@pytest.mark.parametrize("failure", ["blocked", "timeout"])
def test_oge_preserves_white_house_reports_when_pas_index_is_unavailable(failure):
    original = "https://www.whitehouse.gov/wp-content/uploads/2025/08/original.pdf"

    def fetch(method, url, data=None):
        if url == WHITE_HOUSE_INDEX:
            return response(url, f"<a href='{original}'>President Donald J. Trump Periodic Transaction Report 08.12.25</a>")
        if failure == "blocked":
            return response(url, status=403)
        raise httpx.ConnectTimeout("network", request=httpx.Request(method, url))

    result = discover_page("oge", fetch)
    assert len(result["entries"]) == 1
    assert result["entries"][0]["name"] == "Donald J Trump"
    assert result["complete"] is False
    assert result["cursor"] is None
    assert result["partial_error"] == {
        "reason": "blocked" if failure == "blocked" else "http_error",
        "status": 403 if failure == "blocked" else None,
    }


def test_oge_dynamic_index_includes_vice_president_and_public_cabinet_links():
    endpoint = "https://extapps2.oge.gov/201/Presiden.nsf/API.xsp/v3/rest"
    calls = []

    def fetch(method, url, data=None):
        calls.append(url)
        if url == WHITE_HOUSE_INDEX:
            return response(url, "<html></html>")
        if url == OGE_INDEX:
            return response(url, f'<body onload="apiUrl = &quot;{endpoint}&quot;"></body>')
        assert url.startswith(endpoint + "?")
        rows = [
            {"type": "278 Transaction <a href='https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index/123/$FILE/Vance-02.21.2025-278T.pdf'>PDF</a>",
             "name": "Vance, JD", "title": "Vice President", "docDate": "2025-03-01T00:00:00"},
            {"type": "278 Transaction <a href='https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index/234/$FILE/Secretary-02.22.2025-278T.pdf'>PDF</a>",
             "name": "Example, Jane", "title": "Secretary", "docDate": "2025-03-01T00:00:00"},
            {"type": "278 Transaction <a href='https://extapps2.oge.gov/201/Presiden.nsf/201%20Request?OpenForm&Filer=Example'>Request this Document</a>",
             "name": "Example, Protected", "title": "Secretary", "docDate": "2025-03-01T00:00:00"},
        ]
        return httpx.Response(200, json={"data": rows, "recordsFiltered": 3},
                              request=httpx.Request(method, url))

    reports = discover("oge", fetch)
    assert len(reports) == 2
    assert reports[0]["name"] == "JD Vance"
    assert reports[0]["position"] == "vice_president"
    assert reports[0]["filed_date"] is None
    assert reports[0]["document_date"] == "2025-02-21"
    assert reports[0]["index_date"] == "2025-03-01"
    assert reports[1]["position"] == "Secretary"
    assert len(calls) == 3


def test_oge_discovery_resumes_bounded_pages_and_ignores_non_transaction_reports():
    endpoint = "https://extapps2.oge.gov/201/Presiden.nsf/API.xsp/v3/rest"
    offsets = []

    def fetch(method, url, data=None):
        from urllib.parse import parse_qs, urlsplit
        if url == WHITE_HOUSE_INDEX:
            return response(url, "<html></html>")
        if url == OGE_INDEX:
            return response(url, f'<script>const apiUrl="{endpoint}"</script>')
        offset = int(parse_qs(urlsplit(url).query)["start"][0])
        offsets.append(offset)
        rows = [{"type": "278 Annual", "name": f"Example, Person{offset + index}",
                 "docDate": "2025-03-01"}
                for index in range(min(100, 350 - offset))]
        return httpx.Response(200, json={"data": rows, "recordsFiltered": 350},
                              request=httpx.Request(method, url))

    first = discover_page("oge", fetch)
    assert first == {"entries": [], "cursor": {"offset": 300}, "complete": False}
    # A parsed index with no public transaction documents is distinct from a changed index.
    assert discover_page("oge", fetch, cursor=first["cursor"]) == {
        "entries": [], "cursor": None, "complete": True}
    assert offsets == [0, 100, 200, 300]


def test_senate_accepts_terms_with_session_csrf_and_paginates():
    home = "https://efdsearch.senate.gov/search/home/"
    calls = []
    terms = """<form action='' method='POST' id='agreement_form'>
       <input name='csrfmiddlewaretoken' type='hidden' value='session-token'>
       <input name='prohibition_agreement' type='checkbox' value='1'></form>"""
    search = """<input name='csrfmiddlewaretoken' value='new-token'>
       <script>var endpoint = '/search/report/data/';</script>"""

    def fetch(method, url, data=None):
        calls.append((method, url, data))
        if url == SENATE_INDEX:
            return response(url, status=302, headers={"location": "/search/home/"})
        if url == home and method == "GET":
            return response(url, terms)
        if url == home:
            assert data == {"csrfmiddlewaretoken": "session-token", "prohibition_agreement": "1"}
            return response(url, search)
        assert data["csrfmiddlewaretoken"] == "new-token"
        row = ["Jane", "Example", "Senator", "<a href='/search/view/ptr/abc/'>PTR</a>", "01/25/2025"]
        return httpx.Response(200, json={"data": [row], "recordsFiltered": 1},
                              request=httpx.Request(method, url))

    reports = discover("senate", fetch, today=date(2026, 10, 12))
    assert len(calls) == 4
    assert reports[0]["name"] == "Jane Example"
    assert reports[0]["url"] == "https://efdsearch.senate.gov/search/view/ptr/abc/"
    assert reports[0]["filed_date"] == "2025-01-25"
    assert calls[-1][2]["submitted_start_date"] == "01/01/2025"


@pytest.mark.parametrize("source", ["house", "senate", "oge"])
def test_blocked_source_is_explicitly_unavailable(source):
    with pytest.raises(SourceUnavailable) as caught:
        discover(source, lambda method, url, data=None: response(url, status=403))
    assert caught.value.reason == "blocked"
    assert caught.value.status_code == 403


def test_redirect_to_unofficial_host_is_rejected_before_following():
    calls = []

    def fetch(method, url, data=None):
        calls.append(url)
        return response(url, status=302, headers={"location": "https://example.net/challenge/"})

    with pytest.raises(SourceUnavailable, match="unofficial_link"):
        discover("senate", fetch)
    assert calls == [SENATE_INDEX]
