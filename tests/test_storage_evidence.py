"""Response evidence references saved raw sources; content identity ignores observation time."""

import base64
import copy
import hashlib

import pytest
from iirp.config import settings
from iirp.storage import response_evidence, save_object


@pytest.fixture
def runtime(tmp_path):
    previous = settings().runtime_dir
    settings().runtime_dir = tmp_path
    yield tmp_path
    settings().runtime_dir = previous


def test_unchanged_observation_reuses_content_and_changed_price_does_not(runtime):
    original = {"fetched_at": "2026-09-21T20:00:00Z", "timing": {"http_seconds": 2},
                "records": [{"date": "2026-09-21", "close": 123.45}], "contract": "split-only"}
    first, observation = response_evidence(original, {})
    changed = copy.deepcopy(original)
    changed.update(fetched_at="2026-09-21T20:01:00Z", timing={"http_seconds": 9})
    second, later = response_evidence(changed, {})
    assert first == second and observation != later
    changed["records"][0]["close"] = 124
    assert response_evidence(changed, {})[0] != first
    assert original["fetched_at"] == "2026-09-21T20:00:00Z"


def test_raw_document_is_stored_once_and_referenced(runtime):
    raw = b"<feed><entry>exact source evidence</entry></feed>"
    source = save_object(raw, "application/atom+xml")
    data = {"source_documents": [{"url": "https://www.sec.gov/example", "payload": raw.decode(),
                                  "encoding": "utf8", "media_type": "application/atom+xml"}], "entries": [1]}
    payload, _ = response_evidence(data, {"https://www.sec.gov/example": source})
    assert raw not in payload
    assert source["sha256"].encode() in payload


def test_binary_xml_is_referenced_and_mismatch_rejected(runtime):
    raw = b"<ownershipDocument>\xff</ownershipDocument>"
    source = save_object(raw, "application/xml")
    data = {"filing": {"document_url": "https://www.sec.gov/doc.xml",
                        "xml_payload": base64.b64encode(raw).decode(), "xml_encoding": "base64"}}
    payload, _ = response_evidence(data, {"https://www.sec.gov/doc.xml": source})
    assert raw not in payload and source["sha256"].encode() in payload
    wrong = {**source, "sha256": hashlib.sha256(b"other").hexdigest()}
    with pytest.raises(ValueError, match="不一致"):
        response_evidence(data, {"https://www.sec.gov/doc.xml": wrong})


def test_changed_raw_evidence_is_not_silently_deduplicated(runtime):
    url = "https://www.sec.gov/example"
    def encoded(raw):
        return response_evidence({"source_documents": [{"url": url, "payload": raw.decode()}]},
                                 {url: save_object(raw, "text/plain")})[0]
    assert encoded(b"first observed fact") != encoded(b"revised fact")
