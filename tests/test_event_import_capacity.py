"""Full transport, review and revision capacity; synthetic evidence only."""

import copy
import json

import pytest
from fastapi.testclient import TestClient
from iirp.api import app
from iirp.event_service import get_set
from iirp.import_limits import EVENT_REQUEST_BYTES, EVENT_TEXT_CHARACTERS, validate_event_text
from test_event_contracts import document
from test_event_service import confirmation, count
from test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401

pytestmark = pytest.mark.slow


def many_years(years=8):
    result = document(earnings=True)
    result["company"]["name"] = "开发夹具公司（非真实财务事实）"
    result["scope"].update(fiscal_year_start=2024 - years, fiscal_year_end=2023, fiscal_quarters=[1, 2, 3, 4])
    result["events"], result["coverage"] = [], []
    for year in range(2024 - years, 2024):
        for quarter in range(1, 5):
            event = copy.deepcopy(document(True)["events"][0])
            identifier = f"fixture-{year}-q{quarter}"
            event.update(client_event_id=identifier, event_name=f"开发夹具 FY{year} Q{quarter}",
                         fiscal_year=year, fiscal_quarter=quarter, event_year=year,
                         event_date=f"{year}-{quarter * 3:02}-20", period_end=f"{year}-{quarter * 3:02}-01",
                         period_start=f"{year}-{quarter * 3 - 2:02}-01",
                         notes='中文备注、来源与“引号”\\换行\n😀' * 35)
            event["sources"][0].update(url=f"https://example.com/{identifier}",
                evidence_note="仅开发夹具，不代表公司真实公告。" * 40,
                published_date=event["event_date"], supports=["event_date", "event_status", "fiscal_year", "fiscal_quarter", "period_start", "period_end", "period_kind"])
            coverage = copy.deepcopy(document(True)["coverage"][0])
            coverage.update(fiscal_year=year, fiscal_quarter=quarter, event_ids=[identifier], source_urls=[event["sources"][0]["url"]])
            result["events"].append(event)
            result["coverage"].append(coverage)
    return result


@pytest.mark.parametrize("years,escaped", [(8, False), (20, True)])
def test_multiyear_preview_confirm_revision_and_idempotent_retry(years, escaped):
    from iirp.event_models import EventSetVersion
    from iirp.models import Job

    with TestClient(app, headers={"X-IIRP-Client": "web"}) as client:
        payload = many_years(years)
        raw = json.dumps(payload, ensure_ascii=False)
        transport = json.dumps({"text": raw}, ensure_ascii=escaped).encode()
        assert len(transport) > 16384
        preview = client.post("/api/v1/events/preview", content=transport, headers={"Content-Type": "application/json"})
        assert preview.status_code == 200, preview.text[:500]
        item = preview.json()
        assert len(item["events"]) == years * 4
        command = confirmation(item)
        for review in command["reviews"]:
            review.update(date_verified=True, period_verified=True, note="已查看开发夹具来源；不是真实事实核验。" * 50)
        assert len(json.dumps(command).encode()) > 16384
        saved = client.post("/api/v1/events/confirm", json=command)
        assert saved.status_code == 200, saved.text[:500]
        result = saved.json()
        assert client.post("/api/v1/events/confirm", json=command).json()["version_id"] == result["version_id"]
        detail = get_set(result["set_id"])
        assert detail["raw_text"] == raw
        assert detail["document"] == payload
        assert detail["events"][0]["review"]["note"] == command["reviews"][0]["note"]
        payload["events"][0]["notes"] += "修订，仍保留完整来源。"
        changed = client.post("/api/v1/events/preview", json={"text": json.dumps(payload), "set_id": result["set_id"], "expected_version": 1})
        assert changed.status_code == 200
        revision = confirmation(changed.json(), expected_version=1, revision_note="开发夹具修订")
        assert client.post("/api/v1/events/confirm", json=revision).json()["version"] == 2
        assert count(EventSetVersion) == 2
        assert count(Job) == 0


def test_unicode_character_utf8_and_json_escape_limits_are_distinct():
    text = "😀" * EVENT_TEXT_CHARACTERS
    assert len(text.encode()) == EVENT_TEXT_CHARACTERS * 4
    assert len(json.dumps({"text": text}).encode()) < EVENT_REQUEST_BYTES
    assert validate_event_text(text) == text
    with pytest.raises(ValueError, match="字符"):
        validate_event_text(text + "中")


@pytest.mark.parametrize("endpoint", ["preview", "confirm"])
def test_actual_event_json_byte_boundary_and_no_partial_save(endpoint):
    from iirp.event_models import EventImportPreview, EventSet

    with TestClient(app, headers={"X-IIRP-Client": "web"}) as client:
        # Invalid documents reach schema validation at the exact transport limit;
        # an extra UTF-8 byte is stopped before any preview, version or job exists.
        body = b"{}" + b" " * (EVENT_REQUEST_BYTES - 2)
        assert client.post(f"/api/v1/events/{endpoint}", content=body).status_code == 422
        rejected = client.post(f"/api/v1/events/{endpoint}", content=body + b" ")
        assert rejected.status_code == 413
        assert rejected.json()["limit_bytes"] == EVENT_REQUEST_BYTES
        assert count(EventImportPreview) == count(EventSet) == 0


def test_streaming_small_commands_remain_bounded_without_content_length():
    with TestClient(app, headers={"X-IIRP-Client": "web"}) as client:
        response = client.post("/api/v1/events/sets/absent/analyses", content=iter([b" " * 8192, b" " * 8193]))
        assert response.status_code == 413
        assert response.json()["limit_bytes"] == 16384
        limits = client.get("/api/v1/events/limits").json()
        assert limits["request_bytes"] == EVENT_REQUEST_BYTES
        assert limits["text_characters"] == EVENT_TEXT_CHARACTERS
