"""S3: pasted AI-JSON events, D23 windows, templates and the one-fetch analysis flow."""

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal, localcontext

import pytest
from fastapi.testclient import TestClient
from iirp import event_prompts, event_service, lifecycle
from iirp.analytics.event_windows import analyze_events, reaction_day
from iirp.api import app
from iirp.business_worker import execute_business
from iirp.event_input import EventInputError, parse_events
from iirp.models import Job
from iirp.queue import claim
from sqlalchemy import func, select
from test_lifecycle import clean_lifecycle, lifecycle_database, seed_security  # noqa: F401
from test_performance_pipeline import CountingYahoo

HEADERS = {"x-iirp-client": "web"}


def earnings(**overrides):
    return {"ticker": "AAPL", "date": "2024-05-02", "session": "after_close",
            "name": "FY2024 Q2 earnings", "fiscal_year": 2024, "fiscal_quarter": 2, **overrides}


def text(*events):
    return json.dumps({"events": list(events)})


def errors(raw, kind="earnings"):
    with pytest.raises(EventInputError) as caught:
        parse_events(raw, kind)
    return [(e["index"], e["field"]) for e in caught.value.errors]


def test_short_json_is_normalized_and_sorted():
    raw = "```json\n" + text(earnings(ticker="msft", date="2024-04-25"), earnings(note=" 新闻稿 ")) + "\n```"
    events = parse_events(raw, "earnings")
    assert [e["ticker"] for e in events] == ["MSFT", "AAPL"]
    assert events[1]["note"] == "新闻稿"
    custom = parse_events(text({"ticker": "AAPL", "date": "2024-06-10", "session": "during",
                                "name": "WWDC 2024 keynote"}), "custom")
    assert custom == [{"ticker": "AAPL", "date": "2024-06-10", "session": "during",
                       "name": "WWDC 2024 keynote"}]


def test_errors_name_the_item_field_and_reason():
    assert errors(text(earnings(), earnings(date="2024-13-01", session="morning"))) == [
        (2, "date"), (2, "session")]
    assert errors(text(earnings(fiscal_quarter=5))) == [(1, "fiscal_quarter")]
    assert errors(text({k: v for k, v in earnings().items() if k != "fiscal_year"})) == [(1, "fiscal_year")]
    assert errors(text(earnings(extra=1))) == [(1, "extra")]
    assert errors(text(earnings(), earnings(date="2024-05-03"))) == [(2, "fiscal_quarter")]
    assert errors("{\"events\": [") == [(None, None)]
    assert errors(json.dumps({"schema_version": "iirp.custom-events.v1", "events": []})) == [
        (None, "schema_version")]
    # Custom events do not need fiscal fields; a misspelled ticker is still caught.
    assert errors(text({"ticker": "A A", "date": "2024-06-10", "session": "during", "name": "x"}),
                  "custom") == [(1, "ticker")]


def bar(day, close, open_=None):
    value = str(close)
    return {"date": day, "open": str(open_ or close), "high": value, "low": value, "close": value,
            "status": "VALID"}


BARS = [bar("2024-06-05", 100), bar("2024-06-06", 102), bar("2024-06-07", 104),
        bar("2024-06-10", 110, 106), bar("2024-06-11", 111), bar("2024-06-12", 99)]


def change(first, last):
    with localcontext() as context:
        context.prec = 34
        return Decimal(last) / Decimal(first) - 1


def test_reaction_day_and_windows_follow_d23():
    assert reaction_day("2024-06-10", "during") == date(2024, 6, 10)
    assert reaction_day("2024-06-07", "after_close") == date(2024, 6, 10)
    assert reaction_day("2024-06-08", "before_open") == date(2024, 6, 10)
    events = [
        {"ticker": "AAPL", "date": "2024-06-10", "session": "during", "name": "same day"},
        {"ticker": "AAPL", "date": "2024-06-07", "session": "after_close", "name": "after close"},
        {"ticker": "AAPL", "date": "2024-06-08", "session": "unknown", "name": "weekend"},
    ]
    result = analyze_events(events, BARS, n=2, cutoff=date(2024, 6, 12))
    for row in result["rows"]:
        assert row["reaction_date"] == "2024-06-10" and row["baseline_date"] == "2024-06-07"
        windows = {k: Decimal(v["value"]) for k, v in row["windows"].items()}
        assert windows["before"] == change(100, 104)
        assert windows["reaction"] == change(104, 110)
        assert windows["gap"] == change(104, 106)
        assert windows["after"] == change(110, 99)
        assert [c["date"] for c in row["candles"]] == [
            "2024-06-06", "2024-06-07", "2024-06-10", "2024-06-11", "2024-06-12"]
    weekend = next(row for row in result["rows"] if row["name"] == "weekend")
    assert weekend["notes"] == ["session_unknown", "market_closed_on_date"]
    summary = result["summary"]
    assert summary["after"]["n"] == 3 and summary["after"]["up"] == 0
    assert Decimal(summary["reaction"]["up_ratio"]) == 1


def test_unformed_and_missing_prices_stay_empty_and_quarters_group_earnings():
    events = [earnings(date="2024-06-07", fiscal_quarter=2),
              earnings(date="2024-06-10", session="before_open", fiscal_quarter=3, name="Q3")]
    result = analyze_events(events, BARS[:-1], n=2, cutoff=date(2024, 6, 11), kind="earnings")
    after = result["rows"][0]["windows"]["after"]
    assert after == {"value": None, "start_date": "2024-06-10", "end_date": "2024-06-12",
                     "status": "pending"}
    assert result["rows"][0]["candles"][-1]["close"] is None
    missing = analyze_events(events, BARS[1:], n=2, cutoff=date(2024, 6, 12), kind="earnings")
    assert missing["rows"][0]["windows"]["before"]["status"] == "missing_price"
    assert [(q["fiscal_quarter"], q["event_count"]) for q in result["quarters"]] == [(2, 1), (3, 1)]
    assert result["summary"]["after"]["n"] == 0 and result["summary"]["after"]["up_ratio"] is None


def test_prompt_templates_are_saved_and_restored():
    default = event_prompts.get_prompt("earnings", "en")
    assert default["is_default"] and "{{tickers}}" in default["text"]
    saved = event_prompts.save_prompt("earnings", "en", "My prompt for {{tickers}}")
    assert not saved["is_default"] and event_prompts.get_prompt("earnings", "en")["text"] == saved["text"]
    assert event_prompts.get_prompt("earnings", "zh")["is_default"]
    assert event_prompts.reset_prompt("earnings", "en")["text"] == default["text"]
    with pytest.raises(LookupError):
        event_prompts.get_prompt("monthly", "zh")


def run_market(yahoo):
    while job := claim({"market_history"}):
        execute_business(job, runner=yahoo)
    lifecycle.plan_tick()


def test_each_ticker_is_fetched_once_and_results_publish_per_ticker():
    for symbol in ("AAPL", "MSFT"):
        seed_security(symbol)
    from iirp.price_cache import _today

    today = _today()
    events = [earnings(date="2024-05-02"), earnings(date="2024-08-01", fiscal_quarter=3, name="Q3"),
              earnings(ticker="MSFT", date="2024-04-25", session="after_close")]
    created = event_service.create_set({"kind": "earnings", "text": text(*events), "title": None,
                                        "request_id": "s3-test", "analyze": True, "n": 3})
    assert created["set"]["tickers"] == ["AAPL", "MSFT"]
    lifecycle.plan_tick()
    yahoo = CountingYahoo()
    run_market(yahoo)
    assert sorted(call[1] for call in yahoo.calls) == ["AAPL", "MSFT"]
    aapl = next(call for call in yahoo.calls if call[1] == "AAPL")
    # [R-1-n of the earliest event, through today] plus the month of buffer.
    assert aapl[2] == str(date(2024, 4, 29) - timedelta(days=31)) and aapl[3] == str(today)
    view = event_service.get_analysis(created["analysis"]["analysis_id"])
    assert view["status"] == "SUCCEEDED" and view["n"] == 3
    results = {t["symbol"]: t["result"] for t in view["tickers"]}
    assert results["AAPL"]["event_count"] == 2 and results["MSFT"]["event_count"] == 1
    assert results["AAPL"]["rows"][0]["reaction_date"] == "2024-05-03"
    assert [q["fiscal_quarter"] for q in results["AAPL"]["quarters"]] == [2, 3]
    # Within 24 hours another analysis of the same set downloads nothing.
    again = event_service.create_analysis(created["set"]["id"], {"request_id": str(uuid.uuid4())})
    lifecycle.plan_tick()
    run_market(yahoo)
    assert len(yahoo.calls) == 2
    assert event_service.get_analysis(again["analysis_id"])["status"] == "SUCCEEDED"
    from iirp.db import session
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "market_history")) == 2
    # Saved sets survive edits; the analysis keeps its frozen events.
    event_service.update_set(created["set"]["id"], {"title": "Renamed", "text": text(events[0])})
    assert event_service.get_set(created["set"]["id"])["event_count"] == 1
    assert event_service.get_analysis(again["analysis_id"])["event_count"] == 3
    event_service.delete_set(created["set"]["id"])
    assert event_service.get_analysis(again["analysis_id"])["tickers"]


def test_api_validates_saves_and_reports_field_errors():
    with TestClient(app) as client:
        bad = client.post("/api/v1/events/validate", headers=HEADERS,
                          json={"kind": "earnings", "text": text(earnings(session="night"))}).json()
        assert not bad["valid"] and bad["errors"][0] == {
            "index": 1, "field": "session", "message": bad["errors"][0]["message"]}
        good = client.post("/api/v1/events/validate", headers=HEADERS,
                           json={"kind": "earnings", "text": text(earnings())}).json()
        assert good["valid"] and good["events"][0]["reaction_date"] == "2024-05-03"
        rejected = client.post("/api/v1/events/sets", headers=HEADERS,
                               json={"kind": "earnings", "text": text(earnings(session="night"))})
        assert rejected.status_code == 422 and "第 1 条 session" in rejected.json()["detail"]
        saved = client.post("/api/v1/events/sets", headers=HEADERS,
                            json={"kind": "custom", "text": text({"ticker": "AAPL", "date": "2024-06-10",
                                  "session": "during", "name": "WWDC 2024"})})
        assert saved.status_code == 201
        listed = client.get("/api/v1/events/sets", params={"kind": "custom"}).json()["items"]
        assert [item["id"] for item in listed] == [saved.json()["set"]["id"]]
        prompt = client.put("/api/v1/events/prompts/custom", headers=HEADERS, params={"language": "zh"},
                            json={"text": "我的模板"}).json()
        assert prompt["text"] == "我的模板" and not prompt["is_default"]
        assert client.get("/api/v1/events/defaults").json()["window_sessions"]["earnings"] == 5


def test_migration_converts_saved_sets_and_sec_earnings_dates():
    from alembic import command
    from alembic.config import Config
    from iirp.config import ROOT
    from iirp.db import engine
    from sqlalchemy import text as sql

    security = seed_security("AAPL")
    configuration = Config(str(ROOT / "alembic.ini"))
    command.downgrade(configuration, "0025")
    try:
        old = {"client_event_id": "wwdc", "event_name": "WWDC 2024 keynote", "event_date": "2024-06-10",
               "event_time": "10:00", "timezone": "America/Los_Angeles", "notes": "官方回顾",
               "excluded": False}
        with engine().begin() as connection:
            values = {"security": security, "events": json.dumps([old, {**old, "excluded": True}])}
            for statement in (
                """INSERT INTO event_set (id, security_id, title, kind, version, created_at, updated_at)
                   VALUES ('set-1', :security, 'WWDC', 'custom', 1, now(), now())""",
                """INSERT INTO event_import_preview (id, raw_text, content_hash, document, warnings, created_at)
                   VALUES ('p-1', '{}', 'h', '{}', '[]', now())""",
                """INSERT INTO event_set_version (id, set_id, version, preview_id, content_hash, document,
                       events, reviews, revision_note, created_at)
                   VALUES ('v-1', 'set-1', 1, 'p-1', 'h', '{}', CAST(:events AS jsonb), '[]', '', now())""",
                """INSERT INTO earnings_event (id, security_id, fiscal_year, fiscal_quarter, announced_date,
                       announced_at, time_precision, verified, is_estimate, status, evidence, revision, updated_at)
                   VALUES ('e-1', :security, 2024, 2, '2024-05-02', '2024-05-02T20:30:00+00', 'exact',
                       true, false, 'DATE_VERIFIED', '[]', 1, now())""",
            ):
                connection.execute(sql(statement), values)
    finally:
        command.upgrade(configuration, "head")
    sets = {item["title"]: item for item in event_service.list_sets()["items"]}
    custom = event_service.get_set(sets["WWDC"]["id"])["events"]
    # 10:00 Pacific is 13:00 Eastern, during the session; excluded events are not carried over.
    assert [(e["ticker"], e["date"], e["session"], e["note"]) for e in custom] == [
        ("AAPL", "2024-06-10", "during", "官方回顾")]
    earnings_set = next(item for title, item in sets.items() if title.startswith("AAPL 财报日期"))
    assert earnings_set["kind"] == "earnings"
    migrated = event_service.get_set(earnings_set["id"])["events"][0]
    assert (migrated["date"], migrated["session"], migrated["fiscal_quarter"], migrated["reaction_date"]) == (
        "2024-05-02", "after_close", 2, "2024-05-03")
