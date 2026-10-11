"""Background, expectations, grouped statistics and the macro filter, from frozen rows."""

import json
from datetime import date, timedelta
from decimal import Decimal, localcontext

import pytest
from fastapi.testclient import TestClient
from iirp.analysis.event_windows import CALCULATION_VERSION, analyze_events, project_result
from iirp.api.app import app
from iirp.api.events import EventTickerResult
from iirp.db import session
from iirp.events import service as event_service
from iirp.jobs import planner
from iirp.jobs.handlers import execute_business
from iirp.jobs.queue import claim
from iirp.models import Job
from sqlalchemy import func, select

from tests.jobs.test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    seed_security,
)

HEADERS = {"x-iirp-client": "web"}
# The reaction days of the sample below: Q2 jumps 100 -> 105, Q3 falls 105 -> 101.85.
Q2_R, Q3_R = date(2025, 5, 2), date(2025, 8, 1)


def surprise(actual, estimate):
    with localcontext() as context:
        context.prec = 34
        return (Decimal(actual) - Decimal(estimate)) / abs(Decimal(estimate))


def close(day):
    return Decimal(100) if day < Q2_R else Decimal(105) if day < Q3_R else Decimal("101.85")


def bars(first, last):
    from iirp.analysis.calendar import sessions

    return [{"date": str(day), "open": str({Q2_R: Decimal(103), Q3_R: Decimal(104)}.get(day, close(day))),
             "high": "110", "low": "95", "close": str(close(day)), "status": "VALID"}
            for day in sessions(first, last)]


class SampleYahoo:
    """The sample's daily prices for any requested range; records every request."""

    def __init__(self):
        self.calls = []

    def run(self, kind, target, checkpoint):
        from iirp.analysis.calendar import last_completed_session

        self.calls.append((kind, target["symbol"], target["start_date"], target["end_date"]))
        through = min(date.fromisoformat(target["end_date"]), last_completed_session())
        records = [{**bar, "adj_close": bar["close"], "volume": "10", "splits": "0", "dividends": "0"}
                   for bar in bars(date.fromisoformat(target["start_date"]), through)]
        for record in records:
            record.pop("status")
        return {"ok": True, "data": {"provider": "synthetic", "library_version": "fixture",
                "fetched_at": "2025-09-01T00:00:00+00:00", "records": records, "metadata": {}}}


def item(day, category, title, **extra):
    return {"date": day, "category": category, "title": title, "summary": "Hand-written sample.",
            "source": "Sample source", "url": "https://example.org/" + day, **extra}


# Two hand-written AAPL quarters with context and expectations (sample data, not facts).
SAMPLE = {"events": [
    {"ticker": "AAPL", "date": "2025-05-01", "session": "after_close", "name": "FY2025 Q2 earnings",
     "fiscal_year": 2025, "fiscal_quarter": 2,
     "earnings": {"eps_actual": 1.65, "eps_estimate": 1.63, "revenue_actual": 95359,
                  "revenue_estimate": 94680, "guidance": "none"},
     "context": [item("2025-04-02", "policy", "Tariff announcement", major=True),
                 item("2025-05-01", "company", "Buyback authorization")]},
    {"ticker": "AAPL", "date": "2025-07-31", "session": "after_close", "name": "FY2025 Q3 earnings",
     "fiscal_year": 2025, "fiscal_quarter": 3, "uncertain": True, "note": "Estimate from one provider",
     "earnings": {"eps_actual": 1.57, "eps_estimate": 1.56, "revenue_actual": 94036,
                  "revenue_estimate": 89300, "guidance": "raised"},
     "context": [item("2025-07-30", "macro", "Central bank holds rates", major=True)]},
]}


def run_market(yahoo):
    while job := claim({"market_history"}):
        execute_business(job, runner=yahoo)
    planner.plan_tick()


def jobs():
    with session() as s:
        return s.scalar(select(func.count()).select_from(Job))


def test_aapl_sample_end_to_end_groups_filters_and_exports_without_new_requests():
    seed_security("AAPL")
    created = event_service.create_set({"kind": "earnings", "text": json.dumps(SAMPLE), "title": None,
                                        "request_id": "context-e2e", "analyze": True, "n": 3,
                                        "benchmark": None, "sector_benchmark": None})
    planner.plan_tick()
    yahoo = SampleYahoo()
    run_market(yahoo)
    # One request; the range opens a month before the earliest context date (April 2).
    assert [call[1:3] for call in yahoo.calls] == [("AAPL", str(date(2025, 4, 2) - timedelta(days=31)))]
    analysis_id = created["analysis"]["analysis_id"]
    view = event_service.get_analysis(analysis_id)
    assert view["status"] == "SUCCEEDED"
    result = view["tickers"][0]["result"]
    assert result["calculation_version"] == CALCULATION_VERSION == "event-windows-v4-context"
    q2, q3 = result["rows"]
    assert Decimal(q2["windows"]["reaction"]["value"]) == Decimal("0.05")
    assert Decimal(q3["windows"]["reaction"]["value"]) == Decimal("-0.03")
    # Expectations: surprise = (actual - estimate) / |estimate|; 0.64% is in line.
    assert Decimal(q2["expectations"]["eps_surprise"]) == surprise("1.65", "1.63")
    assert Decimal(q2["expectations"]["revenue_surprise"]) == surprise("95359", "94680")
    assert [row["expectations"]["surprise_group"] for row in (q2, q3)] == ["beat", "inline"]
    assert q3["uncertain"] and not q2["uncertain"]
    # The chart of Q2 spans its earliest context date through R+3.
    candles = q2["context_candles"]
    assert candles[0]["date"] == "2025-04-02" and candles[-1]["date"] == "2025-05-07"
    assert next(c for c in candles if c["offset"] == 0)["date"] == "2025-05-02"
    # The policy item is a month before R; the macro item is one session before R.
    assert q2["macro_events"] == [] and [e["title"] for e in q3["macro_events"]] == ["Central bank holds rates"]
    groups = result["groups"]
    assert groups["inline_threshold"] == "0.01"
    assert [(g["key"], g["event_count"], Decimal(g["summary"]["reaction"]["median"])) for g in groups["surprise"]] == [
        ("beat", 1, Decimal("0.05")), ("inline", 1, Decimal("-0.03"))]
    assert [g["key"] for g in groups["guidance"]] == ["raised", "none"]

    with TestClient(app) as client:
        before = (len(yahoo.calls), jobs())
        excluded = client.get(f"/api/v1/events/analyses/{analysis_id}", params={"exclude_macro": "true"})
        assert excluded.status_code == 200
        body = excluded.json()
        assert body["macro_excluded"] == 1 and body["filtered_event_count"] == 1
        kept = body["tickers"][0]["result"]
        assert [row["name"] for row in kept["rows"]] == ["FY2025 Q2 earnings"]
        assert kept["summary"]["reaction"]["n"] == 1 and Decimal(kept["summary"]["reaction"]["median"]) == Decimal("0.05")
        assert [g["key"] for g in kept["groups"]["surprise"]] == ["beat"]
        both = client.get(f"/api/v1/events/analyses/{analysis_id}", params={"quarter": 3}).json()
        assert both["macro_excluded"] == 0 and both["tickers"][0]["result"]["event_count"] == 1
        detail = client.get(f"/api/v1/events/analyses/{analysis_id}/export",
                            params={"table": "detail", "exclude_macro": "true"}).text.lstrip("﻿").splitlines()
        assert len(detail) == 2 and "surprise_group" in detail[0] and ",beat," in detail[1]
        stats = client.get(f"/api/v1/events/analyses/{analysis_id}/export", params={"table": "stats"}).text
        assert "surprise_beat,reaction" in stats and "guidance_raised,reaction" in stats
        # Switching groups and filters reads the frozen result only.
        assert (len(yahoo.calls), jobs()) == before


def frozen(events, kind="earnings"):
    return analyze_events(events, bars(date(2025, 3, 3), date(2025, 9, 30)), n=2, cutoff=date(2025, 9, 30),
                          kind=kind)


def quarter(day, quarter_number, **extra):
    return {"ticker": "AAPL", "date": day, "session": "after_close", "name": f"Q{quarter_number}",
            "fiscal_year": 2025, "fiscal_quarter": quarter_number, **extra}


@pytest.mark.parametrize("actual,estimate,group", [
    ("1.009", "1", "inline"), ("0.991", "1", "inline"), ("1.01", "1", "beat"), ("0.99", "1", "miss"),
    # A negative estimate: a smaller loss is a beat.
    ("-0.10", "-0.20", "beat"), ("1", "0", "no_data"), (None, "1", "no_data"),
])
def test_surprise_groups_use_the_configured_in_line_threshold(actual, estimate, group):
    figures = {key: float(value) for key, value in (("eps_actual", actual), ("eps_estimate", estimate)) if value}
    data = frozen([quarter("2025-05-01", 2, earnings=figures or None)])
    row = project_result({**data, "symbol": "AAPL"})["rows"][0]
    assert row["expectations"]["surprise_group"] == group


def test_major_macro_rule_needs_major_category_and_two_sessions():
    near = {"date": "2025-07-30", "category": "macro", "title": "x", "major": True}
    events = [
        quarter("2025-07-31", 3, context=[near]),
        quarter("2025-05-01", 2, context=[{**near, "date": "2025-05-06", "category": "geopolitical"}]),
        # Not major, not a macro category, or three sessions after R: kept.
        quarter("2025-03-20", 1, fiscal_year=2026, context=[{**near, "date": "2025-03-21", "major": False}]),
        quarter("2025-04-02", 1, context=[{**near, "date": "2025-04-03", "category": "company"}]),
        quarter("2025-06-02", 4, fiscal_year=2024, context=[{**near, "date": "2025-06-06"}]),
    ]
    data = frozen(events)
    all_rows = project_result(data)
    assert [row["name"] for row in all_rows["rows"] if row["macro_events"]] == ["Q2", "Q3"]
    kept = project_result(data, exclude_macro=True)
    assert kept["macro_excluded"] == 2 and kept["event_count"] == 3
    assert kept["summary"]["reaction"]["n"] == 3
    assert all_rows["macro_sessions"] == 2


def test_results_of_the_previous_version_stay_readable():
    data = frozen([quarter("2025-05-01", 2)])
    legacy = {**data, "calculation_version": "event-windows-v3-sector", "symbol": "AAPL", "price_fetched_at": None,
              "rows": [{key: value for key, value in row.items()
                        if key not in ("uncertain", "earnings", "context", "context_candles", "macro_window")}
                       for row in data["rows"]]}
    shown = project_result(legacy, exclude_macro=True)
    assert shown["macro_excluded"] == 0 and shown["event_count"] == 1
    assert shown["groups"]["surprise"][0]["key"] == "no_data"
    parsed = EventTickerResult.model_validate(shown)
    assert parsed.rows[0].context == [] and parsed.rows[0].context_candles is None
