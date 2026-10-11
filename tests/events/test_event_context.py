"""Two-step prompts, background context, expectations and their validation and freezing."""

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from iirp.api.app import app
from iirp.db import session
from iirp.events import prompts as event_prompts
from iirp.events import service as event_service
from iirp.events.input import EventInputError, check_events, parse_events
from iirp.messages import decode
from iirp.models import AnalysisRequest, PromptTemplate

from tests.jobs.test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
)

HEADERS = {"x-iirp-client": "web"}


def context(**overrides):
    return {"date": "2025-10-29", "category": "macro", "title": "Fed cuts rates by 25 bp",
            "summary": "The FOMC lowered the target range.", "source": "Federal Reserve",
            "url": "https://www.federalreserve.gov/", **overrides}


def earnings(**overrides):
    return {"ticker": "AAPL", "date": "2025-10-30", "session": "after_close", "name": "FY2025 Q4 earnings",
            "fiscal_year": 2025, "fiscal_quarter": 4, **overrides}


def text(*events):
    return json.dumps({"events": list(events)})


def issues(raw, kind="earnings", window=(30, 14)):
    with pytest.raises(EventInputError) as caught:
        check_events(raw, kind, window)
    return [(e["index"], e["field"]) for e in caught.value.errors]


def codes(found):
    return [(item["index"], item["field"], decode(item["message"])["code"].rsplit(".", 1)[-1]) for item in found]


@pytest.mark.parametrize("kind", ["earnings", "custom"])
def test_default_prompts_confirm_first_and_hold_the_json_until_confirmed(kind):
    english = event_prompts.DEFAULTS[(kind, "en")]
    chinese = event_prompts.DEFAULTS[(kind, "zh")]
    for template in (english, chinese):
        assert "{{context_before_days}}" in template and "{{context_after_days}}" in template
        assert "{{tickers}}" in template and "{{years}}" in template
        assert ("{{event}}" in template) == (kind == "custom")
    assert "Step 1: confirm first; do not output any JSON yet." in english
    assert "Ask; do not guess. Wait for my reply." in english
    assert "Step 2: only after I confirm, return exactly one JSON object" in english
    assert english.index("Step 1") < english.index('{"events"')
    assert "This needs web search" in english and "Leave out anything you cannot cite" in english
    assert "第一步：先确认，先不要输出 JSON。" in chinese and "有疑问就问，不要猜。等我回复。" in chinese
    assert "第二步：我确认之后，只返回一个 JSON 对象" in chinese
    assert chinese.index("第一步") < chinese.index('{"events"')
    assert "需要联网搜索" in chinese and "找不到来源和链接的信息不要写" in chinese
    # The example in each template passes the page's own validation.
    example = english[english.index('{"events"'):].split("\n", 1)[0]
    events, warnings = check_events(example, kind, (30, 14))
    assert events[0]["context"][0]["major"] is True
    assert ("earnings" in events[0]) == (kind == "earnings")


def test_saved_templates_are_kept_and_a_newer_default_is_pointed_out():
    saved = event_prompts.save_prompt("earnings", "en", "Mine: {{tickers}}")
    assert not saved["default_changed"]
    with session() as s, s.begin():
        s.get(PromptTemplate, ("earnings", "en")).updated_at = event_prompts.DEFAULTS_CHANGED - timedelta(days=1)
    older = event_prompts.get_prompt("earnings", "en")
    assert older["text"] == "Mine: {{tickers}}" and older["default_changed"] and not older["is_default"]
    restored = event_prompts.reset_prompt("earnings", "en")
    assert restored["is_default"] and not restored["default_changed"]
    assert "Step 1" in restored["text"]


def test_context_expectations_and_doubt_are_normalized():
    raw = text(earnings(
        uncertain=True, note=" Estimate from one provider ",
        earnings={"eps_actual": 1.85, "eps_estimate": 1.77, "revenue_actual": 102466,
                  "revenue_estimate": None, "guidance": "none"},
        context=[context(date="2025-11-03", category="company", major=None, summary=None),
                 context(major=True, title="  Fed cuts rates by 25 bp ")]))
    events, warnings = check_events(raw, "earnings", (30, 14))
    event = events[0]
    assert event["uncertain"] is True and event["note"] == "Estimate from one provider"
    assert event["earnings"] == {"eps_actual": 1.85, "eps_estimate": 1.77, "revenue_actual": 102466,
                                 "guidance": "none"}
    # Context is sorted by date; empty optional fields are left out.
    assert [item["date"] for item in event["context"]] == ["2025-10-29", "2025-11-03"]
    assert event["context"][0] == {**context(), "major": True}
    assert event["context"][1] == {k: v for k, v in context(date="2025-11-03", category="company").items()
                                   if k != "summary"}
    assert codes(warnings) == [(1, "uncertain", "uncertain")]


def test_each_new_field_error_names_item_and_field():
    assert issues(text(earnings(context=[context(category="rumor")]))) == [(1, "context[1].category")]
    assert issues(text(earnings(context=[context(url="javascript:alert(1)")]))) == [(1, "context[1].url")]
    assert issues(text(earnings(context=[context(major="yes")]))) == [(1, "context[1].major")]
    assert issues(text(earnings(context=[context(title="")]))) == [(1, "context[1].title")]
    assert issues(text(earnings(context=[context(date="Oct 29")]))) == [(1, "context[1].date")]
    assert issues(text(earnings(context=[context(extra=1)]))) == [(1, "context[1].extra")]
    assert issues(text(earnings(context="none"))) == [(1, "context")]
    assert issues(text(earnings(context=[1]))) == [(1, "context[1]")]
    assert issues(text(earnings(context=[context()] * 31))) == [(1, "context")]
    assert issues(text(earnings(uncertain="no"))) == [(1, "uncertain")]
    assert issues(text(earnings(), earnings(date="2025-07-31", fiscal_quarter=3,
                                            earnings={"eps_actual": "1.2", "guidance": "up", "revenue_actual": -1,
                                                      "margin": 0.4}))) == [
        (2, "earnings.eps_actual"), (2, "earnings.guidance"), (2, "earnings.revenue_actual"),
        (2, "earnings.margin")]
    assert issues(text(earnings(earnings=[1.2]))) == [(1, "earnings")]
    custom = {"ticker": "AAPL", "date": "2025-06-09", "session": "during", "name": "WWDC",
              "earnings": {"eps_actual": 1.0}}
    assert issues(text(custom), "custom") == [(1, "earnings")]


def test_warnings_name_window_count_sources_and_length():
    raw = text(earnings(context=[context(date="2025-10-20"), context(date="2025-11-05", source=None),
                                 context(date="2025-10-31", url=""), context(summary="x" * 301)]),
               earnings(date="2025-07-31", fiscal_quarter=3, context=[context(date="2025-07-30")] * 9))
    events, warnings = check_events(raw, "earnings", (7, 7))
    found = codes(warnings)
    # Items are numbered as pasted; context items as listed in their event.
    assert (1, "context[1].date", "context_outside") in found
    assert (1, "context[2].source", "context_source_missing") in found
    assert (1, "context[2].date", "context_outside") not in found
    assert (1, "context[3].url", "context_url_missing") in found
    assert (1, "context[4].summary", "summary_long") in found
    assert (2, "context", "context_too_many") in found
    # Warnings do not block: every item is kept, and the widest window accepts all dates.
    assert len(events[1]["context"]) == 4 and len(events[0]["context"]) == 9
    _, wide = check_events(raw, "earnings", (90, 30))
    assert not [item for item in codes(wide) if item[2] == "context_outside"]


def test_lists_without_the_new_fields_stay_valid():
    old = [{"ticker": "AAPL", "date": "2024-05-02", "session": "after_close", "name": "FY2024 Q2 earnings",
            "fiscal_year": 2024, "fiscal_quarter": 2, "note": "press release"}]
    events, warnings = check_events(json.dumps(old), "earnings", (30, 14))
    assert events == old and warnings == []
    assert parse_events(text(*old), "earnings") == old


def test_validation_api_uses_the_chosen_window_and_saved_context_is_frozen():
    raw = text(earnings(earnings={"eps_actual": 1.85, "eps_estimate": 1.77, "guidance": "raised"},
                        context=[context(date="2025-09-30", major=True)]))
    with TestClient(app) as client:
        default = client.post("/api/v1/events/validate", headers=HEADERS,
                              json={"kind": "earnings", "text": raw}).json()
        assert default["valid"] and default["warnings"] == []
        assert default["events"][0]["context"][0]["major"] is True
        assert default["events"][0]["earnings"]["guidance"] == "raised"
        narrow = client.post("/api/v1/events/validate", headers=HEADERS,
                             json={"kind": "earnings", "text": raw, "context_before_days": 7,
                                   "context_after_days": 7}).json()
        assert codes(narrow["warnings"]) == [(1, "context[1].date", "context_outside")]
        assert client.post("/api/v1/events/validate", headers=HEADERS,
                           json={"kind": "earnings", "text": raw, "context_before_days": 400}).status_code == 422
        defaults = client.get("/api/v1/events/defaults").json()["context"]
        assert defaults == {"before_days": 30, "after_days": 14, "presets": [[7, 7], [30, 14], [90, 30]],
                            "max_days": 366}
    created = event_service.create_set({"kind": "earnings", "text": raw, "title": None, "request_id": "context",
                                        "analyze": True, "n": 3, "benchmark": None})
    saved = event_service.get_set(created["set"]["id"])
    assert saved["events"][0]["context"] == [{**context(date="2025-09-30"), "major": True}]
    # Editing the set later does not change what the analysis froze.
    event_service.update_set(created["set"]["id"], {"text": text(earnings())})
    assert "context" not in event_service.get_set(created["set"]["id"])["events"][0]
    with session() as s:
        frozen = s.get(AnalysisRequest, created["analysis"]["analysis_id"]).params["events"][0]
    assert frozen["context"][0]["major"] is True and frozen["earnings"]["eps_estimate"] == 1.77
