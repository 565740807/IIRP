"""S3: pasted AI-JSON events, D23 windows, templates and the one-fetch analysis flow."""

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal, localcontext

import pytest
from fastapi.testclient import TestClient
from iirp.analysis.event_windows import analyze_events, reaction_day
from iirp.api.app import app
from iirp.events import prompts as event_prompts
from iirp.events import service as event_service
from iirp.events.input import EventInputError, parse_events
from iirp.jobs import planner
from iirp.jobs.handlers import execute_business
from iirp.jobs.queue import claim
from iirp.models import Job
from sqlalchemy import func, select

from tests.analysis.test_performance_pipeline import CountingYahoo
from tests.jobs.test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    seed_security,
)
from tests.zh import zh

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
    assert summary["reaction"]["up"] == 3 and Decimal(summary["reaction"]["up_high"]) == 1
    # Without a benchmark nothing is paired.
    assert summary["reaction"]["paired_n"] == 0 and result["benchmark"] is None


def test_unformed_and_missing_prices_stay_empty_and_quarters_group_earnings():
    events = [earnings(date="2024-06-07", fiscal_quarter=2),
              earnings(date="2024-06-10", session="before_open", fiscal_quarter=3, name="Q3")]
    result = analyze_events(events, BARS[:-1], n=2, cutoff=date(2024, 6, 11), kind="earnings")
    after = result["rows"][0]["windows"]["after"]
    assert after == {"value": None, "start_date": "2024-06-10", "end_date": "2024-06-12",
                     "status": "pending", "benchmark": None, "excess": None}
    assert result["rows"][0]["path"][-1]["value"] is None
    assert result["rows"][0]["candles"][-1]["close"] is None
    missing = analyze_events(events, BARS[1:], n=2, cutoff=date(2024, 6, 12), kind="earnings")
    assert missing["rows"][0]["windows"]["before"]["status"] == "missing_price"
    assert [(q["fiscal_quarter"], q["event_count"]) for q in result["quarters"]] == [(2, 1), (3, 1)]
    assert result["summary"]["after"]["n"] == 0 and result["summary"]["after"]["up_low"] is None


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
    planner.plan_tick()


def test_each_ticker_is_fetched_once_and_results_publish_per_ticker():
    for symbol in ("AAPL", "MSFT"):
        seed_security(symbol)
    from iirp.market.cache import _today

    today = _today()
    events = [earnings(date="2024-05-02"), earnings(date="2024-08-01", fiscal_quarter=3, name="Q3"),
              earnings(ticker="MSFT", date="2024-04-25", session="after_close")]
    created = event_service.create_set({"kind": "earnings", "text": text(*events), "title": None,
                                        "request_id": "s3-test", "analyze": True, "n": 3,
                                        "benchmark": None})
    assert created["set"]["tickers"] == ["AAPL", "MSFT"]
    planner.plan_tick()
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
    filtered = TestClient(app).get(f"/api/v1/events/analyses/{view['id']}?quarter=3")
    assert filtered.status_code == 200
    selected = next(item["result"] for item in filtered.json()["tickers"] if item["symbol"] == "AAPL")
    assert selected["event_count"] == 1 and selected["rows"][0]["fiscal_quarter"] == 3
    assert Decimal(selected["summary"]["reaction"]["median"]) == Decimal(selected["rows"][0]["windows"]["reaction"]["value"])
    assert len(yahoo.calls) == 2
    assert TestClient(app).get(f"/api/v1/events/analyses/{view['id']}?quarter=5").status_code == 422
    # Within 24 hours another analysis of the same set downloads nothing.
    again = event_service.create_analysis(created["set"]["id"], {"request_id": str(uuid.uuid4()),
                                                                 "benchmark": None})
    planner.plan_tick()
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


def test_benchmark_is_fetched_once_and_quick_switches_reuse_analyses():
    from iirp.db import session
    from iirp.models import Security

    for symbol in ("AAPL", "MSFT"):
        seed_security(symbol)
    index = seed_security("^GSPC")
    with session() as s, s.begin():
        s.get(Security, index).instrument = "INDEX"
    events = [earnings(date="2024-05-02"), earnings(ticker="MSFT", date="2024-04-25")]
    created = event_service.create_set({"kind": "earnings", "text": text(*events), "title": None,
                                        "request_id": "s6b-test", "analyze": True, "n": 3})
    analysis_id = created["analysis"]["analysis_id"]
    planner.plan_tick()
    yahoo = CountingYahoo()
    run_market(yahoo)
    # One request per ticker and one for the benchmark, which covers both tickers' dates.
    assert sorted(call[1] for call in yahoo.calls) == ["AAPL", "MSFT", "^GSPC"]
    view = event_service.get_analysis(analysis_id)
    assert view["status"] == "SUCCEEDED" and view["benchmark"] == "^GSPC"
    assert [item["step"] for item in view["progress"]] == ["done", "done"]
    for item in view["tickers"]:
        assert item["result"]["benchmark"] == {"symbol": "^GSPC", "status": "available"}
        assert item["result"]["summary"]["reaction"]["paired_n"] == 1
    # Reuse the already fetched caches with different acquisition times. Results
    # must expire with the oldest input and report each input separately.
    from iirp.models import AnalysisResult, PriceCache, now
    with session() as s, s.begin():
        benchmark_cache = s.scalar(select(PriceCache).where(PriceCache.security_id == index))
        benchmark_cache.fetched_at = now() - timedelta(hours=18)
        benchmark_cache.expires_at = benchmark_cache.fetched_at + timedelta(hours=24)
        early = benchmark_cache.expires_at.isoformat()
        fetched = benchmark_cache.fetched_at.isoformat()
    older_input = event_service.analysis_variant(analysis_id, {"request_id": "older-input", "n": 4})
    planner.plan_tick()
    run_market(yahoo)
    old_view = event_service.get_analysis(older_input["analysis_id"])
    assert len(yahoo.calls) == 3
    assert old_view["freshness"]["price_expires_at"].isoformat() == early
    assert old_view["freshness"]["sources"][0] == {"symbol": "^GSPC", "fetched_at": fetched, "expires_at": early}
    assert {source["symbol"] for source in old_view["freshness"]["sources"]} == {"AAPL", "MSFT", "^GSPC"}
    with session() as s:
        assert all(row.expires_at.isoformat() == early for row in s.scalars(
            select(AnalysisResult).where(AnalysisResult.analysis_id == older_input["analysis_id"])))
    # Another n, then back: the second switch reopens the first analysis.
    wider = event_service.analysis_variant(analysis_id, {"request_id": "v1", "n": 5})
    assert not wider["reused"]
    back = event_service.analysis_variant(wider["analysis_id"], {"request_id": "v2", "n": 3})
    assert back["reused"] and back["analysis_id"] == analysis_id
    plain = event_service.analysis_variant(analysis_id, {"request_id": "v3", "benchmark": None})
    planner.plan_tick()
    run_market(yahoo)
    assert len(yahoo.calls) == 3
    assert event_service.get_analysis(plain["analysis_id"])["benchmark"] is None
    assert event_service.get_analysis(wider["analysis_id"])["n"] == 5
    stats = event_service.export_analysis(analysis_id, "stats").lstrip("\ufeff").splitlines()
    # Each ticker: all events and its one quarter, four windows each.
    assert stats[0].startswith("ticker,group,window,n_sessions,benchmark,n,median") and len(stats) == 1 + 2 * 2 * 4
    detail = event_service.export_analysis(analysis_id, "detail").lstrip("\ufeff").splitlines()
    assert len(detail) == 3 and ",^GSPC," in detail[1]


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
        assert rejected.status_code == 422 and "第 1 条 session" in zh(rejected.json()["detail"])
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
