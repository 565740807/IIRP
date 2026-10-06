"""Disposable PostgreSQL event research tests; all dates/prices/evidence synthetic."""

import copy
import csv
import io
import json
import uuid
from datetime import date

import pytest
from iirp import event_service as service
from iirp.analytics.calendar import session_window
from iirp.business_models import AnalysisRequest, Batch, RequestScope, Security
from iirp.db import session
from iirp.event_models import EventCommandReceipt, EventImportPreview, EventSet, EventSetVersion
from iirp.models import Job
from sqlalchemy import func, select
from test_event_contracts import document
from test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    refetch_prices,
    seed_prices,
    seed_security,
)


def preview(payload=None, **extras):
    return service.preview_import({"text": json.dumps(payload or document()), **extras})


def confirmation(item, **extras):
    return {
        "preview_id": item["preview_id"],
        "request_id": str(uuid.uuid4()),
        "reviews": [
            {
                "client_event_id": row["client_event_id"],
                "selected": True,
                "date_verified": False,
                "time_verified": False,
                "period_verified": False,
                "note": "",
            }
            for row in item["events"]
        ],
        **extras,
    }


def saved(verified=True):
    seed_security()
    item = preview()
    values = confirmation(item)
    values["reviews"][0]["date_verified"] = verified
    return service.confirm_import(values)


@pytest.mark.parametrize("failure_stage", ["transport", "restore"])
def test_large_review_transport_is_atomic_and_preserves_ordered_views(failure_stage):
    from iirp.db import engine
    from iirp.event_pipeline import freeze_input
    from sqlalchemy import event

    original = saved()
    payload = document()
    second = copy.deepcopy(payload["events"][0])
    second.update(client_event_id="second", event_name="Second synthetic event")
    payload["events"].append(second)
    payload["coverage"][0]["event_ids"].append("second")
    item = preview(payload, set_id=original["set_id"], expected_version=1)
    values = confirmation(item, expected_version=1, revision_note="Large review transport", analyze=True)
    notes = {"event-2024": '核验"\\\n' * 300_000, "second": "second " * 170_000}
    for review in values["reviews"]:
        review.update(note=notes[review["client_event_id"]], date_verified=True)
    values["reviews"].reverse()  # Review order need not match event order.
    restored = []
    def fail_after_restore(conn, cursor, statement, parameters, context, many):
        prefix = ("INSERT INTO pg_temp.iirp_event_note_chunks" if failure_stage == "transport"
                  else "UPDATE event_set_version SET")
        if statement.startswith(prefix):
            restored.append(True)
            if len(restored) == 3:
                raise RuntimeError("synthetic note restoration failure")
    event.listen(engine(), "after_cursor_execute", fail_after_restore)
    try:
        with pytest.raises(RuntimeError, match="note restoration failure"):
            service.confirm_import(values)
    finally:
        event.remove(engine(), "after_cursor_execute", fail_after_restore)
    assert len(restored) == 3
    with session() as s:
        assert s.get(EventSet, original["set_id"]).version == 1
        assert s.scalar(select(func.count()).select_from(EventSetVersion)) == 1
        assert s.get(EventCommandReceipt, values["request_id"]) is None
        assert s.scalar(select(func.count()).select_from(AnalysisRequest)) == 0
        assert s.get(EventSetVersion, original["version_id"]).reviews[0]["note"] == ""
        from sqlalchemy import text
        assert s.scalar(text("SELECT to_regclass('pg_temp.iirp_event_note_chunks')")) is None
    saved_version = service.confirm_import(values)
    assert service.confirm_import(values)["reused"]
    with session() as s:
        revision = s.get(EventSetVersion, saved_version["version_id"])
        assert {e["client_event_id"]: e["review"]["note"] for e in revision.events} == notes
        assert {r["client_event_id"]: r["note"] for r in revision.reviews} == notes
        request = s.get(AnalysisRequest, saved_version["analysis"]["analysis_id"])
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == request.batch_id))
        assert scope.checkpoint["compute_input_key"] == freeze_input(s, request, scope)["input_key"]


def analysis(collection, **extras):
    created = service.create_analysis(
        collection["set_id"],
        {
            "request_id": str(uuid.uuid4()),
            "version": collection["version"],
            "cutoff_date": "2025-01-01",
            **extras,
        },
    )
    complete_event_compute(created["analysis_id"])
    return created


def complete_event_compute(identifier, *, enqueue=True):
    """Explicitly execute the new asynchronous stage for result-semantic tests."""
    from iirp.business_worker import execute_business
    from iirp.event_pipeline import plan_event_compute
    from iirp.operations import run_request
    from iirp.queue import claim

    if enqueue:
        with session() as s, s.begin():
            request = s.get(AnalysisRequest, identifier)
            scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == request.batch_id))
            plan_event_compute(s, request, scope, [32])
    class LocalCompute:
        def run(self, kind, target, checkpoint):
            assert checkpoint()
            return run_request({"kind": kind, "target": target})
    while job := claim({"event_compute"}):
        execute_business(job, runner=LocalCompute())
        with session() as s:
            finished = s.get(Job, job.id)
            assert finished.status == "SUCCEEDED", finished.error



def plan(identifier, capacity=32):
    with session() as s, s.begin():
        request = s.get(AnalysisRequest, identifier)
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == request.batch_id))
        batch = s.get(Batch, request.batch_id)
        service.plan_event_scope(s, scope, batch, [capacity])
    complete_event_compute(identifier, enqueue=False)
    with session() as s, s.begin():
        request = s.get(AnalysisRequest, identifier)
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == request.batch_id))
        batch = s.get(Batch, request.batch_id)
        service.plan_event_scope(s, scope, batch, [capacity])
        return scope.status, scope.wait_reason, scope.checkpoint


def count(model):
    with session() as s:
        return s.scalar(select(func.count()).select_from(model))


def count_acquisition_jobs():
    with session() as s:
        return s.scalar(select(func.count()).select_from(Job).where(Job.kind != "event_compute"))


def test_preview_preserves_raw_but_never_creates_facts_identity_or_jobs():
    raw = " \n" + json.dumps(document(), ensure_ascii=False) + "\n "
    item = service.preview_import({"text": raw})
    assert item["events"][0]["date_verified"] is False
    assert item["events"][0]["event_time"] is None
    with session() as s:
        assert s.get(EventImportPreview, item["preview_id"]).raw_text == raw
    assert count(EventSet) == count(Security) == count(Job) == 0


def test_ai_cannot_supply_approval_flags_and_invalid_input_never_saved():
    payload = document()
    payload["events"][0]["date_verified"] = True
    with pytest.raises(ValueError):
        preview(payload)
    assert count(EventImportPreview) == 0


def test_confirmation_defaults_unverified_and_retries_are_idempotent():
    item = preview()
    command = confirmation(item)
    first = service.confirm_import(command)
    assert service.confirm_import(command)["version_id"] == first["version_id"]
    assert count(EventSet) == count(EventSetVersion) == count(Security) == 1
    detail = service.get_set(first["set_id"])
    assert detail["events"][0]["date_verified"] is False
    assert detail["events"][0]["review"]["method"] == "user_selection"
    assert count(Job) == 0
    command["title"] = "Different"
    with pytest.raises(RuntimeError, match="request_id"):
        service.confirm_import(command)


def test_simple_32_quarter_http_import_review_save_revise_and_original_text():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from iirp.event_api import router
    from test_event_simple import simple_document

    app = FastAPI()
    app.include_router(router)
    seed_security()
    with TestClient(app) as client:
        payload = simple_document()
        raw = json.dumps(payload, ensure_ascii=False)
        response = client.post("/api/v1/events/preview", json={"text": raw})
        assert response.status_code == 200, response.text
        item = response.json()
        assert len(item["events"]) == 32
        assert not any(e["date_verified"] or e["period_verified"] for e in item["events"])
        command = confirmation(item)
        saved = client.post("/api/v1/events/confirm", json=command)
        assert saved.status_code == 200, saved.text
        identifier = saved.json()["set_id"]
        detail = client.get(f"/api/v1/events/sets/{identifier}").json()
        assert detail["raw_text"] == raw and detail["verified_count"] == 0
        assert not detail["events"][0]["time_verified"]
        assert client.post("/api/v1/events/confirm", json=command).json()["version_id"] == saved.json()["version_id"]
        payload["events"][0]["sources"][0]["note"] += " 已人工打开核对。"
        revised = client.post("/api/v1/events/preview", json={
            "text": json.dumps(payload, ensure_ascii=False), "set_id": identifier, "expected_version": 1,
        }).json()
        verified = confirmation(revised, expected_version=1, revision_note="补充依据后逐项核对")
        for review in verified["reviews"]:
            review.update(date_verified=True, period_verified=True)
        second = client.post("/api/v1/events/confirm", json=verified)
        assert second.status_code == 200, second.text
        latest = client.get(f"/api/v1/events/sets/{identifier}").json()
        assert latest["version"] == 2 and latest["verified_count"] == 32
        old = client.get(f"/api/v1/events/sets/{identifier}?version=1").json()
        assert old["raw_text"] == raw and old["verified_count"] == 0
        assert not any(e["time_verified"] for e in latest["events"])


def test_same_content_different_request_reuses_version():
    item = preview()
    first = service.confirm_import(confirmation(item))
    second = service.confirm_import(confirmation(preview()))
    assert first["version_id"] == second["version_id"]
    assert count(EventSetVersion) == 1
    assert count(EventCommandReceipt) == 2


def test_excluded_rows_require_reason_and_remain_in_saved_evidence():
    item = preview()
    command = confirmation(item)
    command["reviews"][0]["selected"] = False
    with pytest.raises(ValueError, match="原因"):
        service.confirm_import(command)
    command["reviews"][0]["note"] = "来源尚待核对"
    result = service.confirm_import(command)
    detail = service.get_set(result["set_id"])
    assert detail["selected_count"] == 0
    assert detail["events"][0]["excluded"]
    assert detail["events"][0]["review"]["note"] == "来源尚待核对"


def test_schedule_minute_cannot_be_confirmed_as_actual_time():
    payload = document()
    event = payload["events"][0]
    event.update(
        event_time="10:00",
        timezone="America/Los_Angeles",
        time_precision="minute",
        time_basis="official_schedule",
    )
    event["sources"][0]["supports"] += ["event_time", "timezone"]
    item = preview(payload)
    command = confirmation(item)
    command["reviews"][0].update(date_verified=True, time_verified=True)
    with pytest.raises(ValueError, match="官方日程"):
        service.confirm_import(command)
    assert count(EventSet) == 0


def test_unknown_date_cannot_be_reviewed_as_verified():
    payload = document()
    event = payload["events"][0]
    event.update(event_date=None, time_precision="unknown", date_status="unverified")
    item = preview(payload)
    command = confirmation(item)
    command["reviews"][0]["date_verified"] = True
    with pytest.raises(ValueError, match="不能确认日期"):
        service.confirm_import(command)


def test_revision_keeps_old_raw_review_dates_and_rejects_stale_preview():
    collection = saved()
    changed = document()
    changed["events"][0]["event_date"] = "2024-06-11"
    new = preview(changed, set_id=collection["set_id"], expected_version=1)
    stale = preview(changed, set_id=collection["set_id"], expected_version=1)
    values = confirmation(new, expected_version=1, revision_note="核对后更正活动日期")
    updated = service.confirm_import(values)
    assert updated["version"] == 2
    assert service.get_set(collection["set_id"], 1)["events"][0]["event_date"] == "2024-06-10"
    assert service.get_set(collection["set_id"])["events"][0]["event_date"] == "2024-06-11"
    with pytest.raises(RuntimeError, match="版本冲突"):
        service.confirm_import(confirmation(stale, expected_version=1, revision_note="旧预览"))
    assert count(EventSetVersion) == 2


def test_identity_mismatch_rolls_back_atomic_confirmation():
    security_id = seed_security("MSFT")
    item = preview()
    with pytest.raises(ValueError, match="ticker"):
        service.confirm_import(confirmation(item, security_id=security_id))
    assert count(EventSet) == count(EventSetVersion) == 0


def test_dates_only_verified_event_uses_twelve_closes_and_preserves_freeze():
    collection = saved()
    security_id = service.get_set(collection["set_id"])["security"]["id"]
    days = session_window(date(2024, 6, 10), 6, 5)
    dataset = seed_prices(security_id, days[0], days[-1])
    created = analysis(collection)
    detail = service.get_analysis(created["analysis_id"])
    assert detail["data"]["metadata"]["dataset_id"] == dataset
    row = detail["data"]["rows"][0]
    assert len(row["points"]) == 11
    assert row["baseline_extra_date"] == str(days[0])
    assert row["windows"]["before5"]["expected_closes"] == 6
    assert row["windows"]["before5"]["value"] == "0"
    assert row["date_verified"] and not row["time_verified"]
    assert row["eligible"]
    old_id, old_data = detail["result_id"], copy.deepcopy(detail["data"])
    assert plan(created["analysis_id"])[0] == "READY"
    assert count_acquisition_jobs() == 0
    revised = document()
    revised["events"][0]["event_date"] = "2024-06-11"
    item = preview(revised, set_id=collection["set_id"], expected_version=1)
    service.confirm_import(confirmation(item, expected_version=1, revision_note="修订日期"))
    assert service.get_analysis(created["analysis_id"], old_id)["data"] == old_data
    assert (
        json.loads(service.export_analysis(created["analysis_id"], old_id, "json"))["data"]
        == old_data
    )
    exported = list(
        csv.DictReader(io.StringIO(service.export_analysis(created["analysis_id"], old_id, "csv")))
    )
    assert len([row for row in exported if row["record_type"] == "daily_price"]) == 11
    assert len([row for row in exported if row["record_type"] == "distribution"]) == 4
    assert all(row["parameters"] and row["calculation_version"] for row in exported)


def test_selected_window_versions_and_export_reuse_prices_and_preserve_old_result():
    collection = saved()
    security_id = service.get_set(collection["set_id"])["security"]["id"]
    days = session_window(date(2024, 6, 10), 6, 5)
    seed_prices(security_id, days[0], days[-1])
    first = analysis(collection, date_window="after5")
    old = service.get_analysis(first["analysis_id"])
    second = analysis(collection, date_window="before5", date_category="developer_keynote")
    new = service.get_analysis(second["analysis_id"])
    assert old["result_id"] != new["result_id"]
    assert old["params"]["date_window"] == "after5"
    assert new["params"]["date_window"] == "before5"
    assert count_acquisition_jobs() == 0
    assert service.get_analysis(first["analysis_id"], old["result_id"])["data"] == old["data"]
    exported = list(
        csv.DictReader(
            io.StringIO(service.export_analysis(second["analysis_id"], new["result_id"], "csv"))
        )
    )
    selected = [row for row in exported if row["record_type"] == "selected_window_price"]
    points = new["data"]["window_series"][0]["points"]
    assert [row["trading_date"] for row in selected] == [p["date"] for p in points]
    assert [
        json.loads(row["statistics_or_window"])["relative_to_window_start"] for row in selected
    ] == [p["value"] for p in points]
    assert all(json.loads(row["parameters"])["date_window"] == "before5" for row in exported)
    assert len(selected) == 6 and selected[0]["offset"] == "-6"


def test_resume_pre_window_contract_request_keeps_idempotency_and_rejects_changed_window():
    collection = saved()
    old = {
        "request_id": str(uuid.uuid4()),
        "version": collection["version"],
        "cutoff_date": "2025-01-01",
    }
    first = service.create_analysis(collection["set_id"], old)
    retry = service.create_analysis(
        collection["set_id"], {**old, "date_window": "after5", "date_category": None}
    )
    assert retry["analysis_id"] == first["analysis_id"]
    with pytest.raises(RuntimeError, match="不能替换参数"):
        service.create_analysis(collection["set_id"], {**old, "date_window": "before5"})


def test_pending_identity_only_schedules_identity_no_history_until_verified():
    item = preview()
    collection = service.confirm_import(confirmation(item))
    created = analysis(collection)
    plan(created["analysis_id"])
    with session() as s:
        assert list(s.scalars(select(Job.kind))) == ["market_identity"]


def test_unverified_default_does_not_request_prices_but_opt_in_separate_observation_does():
    collection = saved(verified=False)
    first = analysis(collection)
    plan(first["analysis_id"])
    assert count_acquisition_jobs() == 0
    assert service.get_analysis(first["analysis_id"])["data"]["rows"] == []
    second = analysis(collection, include_unverified=True)
    plan(second["analysis_id"])
    assert count_acquisition_jobs() == 1
    row = service.get_analysis(second["analysis_id"])["data"]["rows"][0]
    assert not row["eligible"] and not row["date_verified"]


def test_windows_outside_the_cache_are_fetched_as_one_range():
    payload = document()
    earlier = copy.deepcopy(payload["events"][0])
    earlier.update(client_event_id="earlier", event_date="2023-06-12", event_year=2023)
    payload["scope"]["year_start"] = 2023
    payload["events"].append(earlier)
    coverage = copy.deepcopy(payload["coverage"][0])
    coverage.update(year=2023, event_ids=["earlier"])
    payload["coverage"].append(coverage)
    security_id = seed_security()
    item = preview(payload)
    values = confirmation(item)
    for review in values["reviews"]:
        review["date_verified"] = True
    collection = service.confirm_import(values)
    days = session_window(date(2024, 6, 10), 6, 5)
    seed_prices(security_id, days[0], days[5])
    created = analysis(collection)
    plan(created["analysis_id"])
    with session() as s:
        jobs = s.scalars(select(Job).where(Job.kind != "event_compute").order_by(Job.created_at)).all()
        # One request for every selected event window plus a month of buffer.
        assert len(jobs) == 1
        assert jobs[0].target["start_date"] < "2023-06-01"
        assert jobs[0].target["end_date"] >= str(days[-1])
        assert jobs[0].priority == 10


def test_future_cutoff_never_requests_future_prices():
    collection = saved()
    created = analysis(collection, cutoff_date="2099-01-01")
    params = service.get_analysis(created["analysis_id"])["params"]
    from iirp.analytics.calendar import last_completed_session

    assert date.fromisoformat(params["cutoff_date"]) <= last_completed_session()
    plan(created["analysis_id"])
    from iirp.price_cache import _today

    with session() as s:
        # One fetch runs through today (D14); it never asks for a later day.
        assert all(
            date.fromisoformat(j.target["end_date"]) <= _today()
            for j in s.scalars(select(Job).where(Job.kind == "market_history"))
        )


def test_missing_prices_are_fetched_without_waiting_for_window_completion():
    seed_security()
    created = analysis(saved())
    status, reason, _ = plan(created["analysis_id"], capacity=0)
    assert status == "RUNNING" and "正在获取行情" in reason
    assert "窗口结束交易日" not in reason
    assert count_acquisition_jobs() == 1


def test_both_prompts_ask_before_browsing_and_use_contract_schema():
    for kind, version in (
        ("custom", "iirp.custom-events.v1"),
        ("earnings", "iirp.earnings-events.v1"),
    ):
        result = service.event_prompt(kind)
        assert result["schema_version"] == version
        assert "每轮最多 3 个问题" in result["prompt"]
        assert "不能联网" in result["prompt"]
        assert result["json_schema"]["additionalProperties"] is False
        assert result["input_schema_version"] in result["prompt"]
        assert "$defs" not in result["prompt"]


def test_http_preview_confirm_analysis_and_frozen_export():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from iirp.event_api import router

    app = FastAPI()
    app.include_router(router)
    seed_security()
    with TestClient(app) as client:
        item = client.post("/api/v1/events/preview", json={"text": json.dumps(document())})
        assert item.status_code == 200
        command = confirmation(item.json())
        command["reviews"][0]["date_verified"] = "true"
        assert client.post("/api/v1/events/confirm", json=command).status_code == 422
        command["reviews"][0]["date_verified"] = True
        stored = client.post("/api/v1/events/confirm", json=command)
        assert stored.status_code == 200
        result = stored.json()
        created = client.post(
            f"/api/v1/events/sets/{result['set_id']}/analyses",
            json={
                "request_id": str(uuid.uuid4()),
                "version": result["version"],
            },
        )
        assert created.status_code == 202
        identifier = created.json()["analysis_id"]
        waiting = client.get(f"/api/v1/events/analyses/{identifier}").json()
        assert waiting["result_id"] is None and waiting["data"] is None
        complete_event_compute(identifier)
        viewed = client.get(f"/api/v1/events/analyses/{identifier}").json()
        assert viewed["data"]["rows"][0]["anchor"]["original_date"] == "2024-06-10"
        assert count_acquisition_jobs() == 0
        exported = client.get(
            f"/api/v1/events/analyses/{identifier}/export",
            params={
                "result_id": viewed["result_id"],
                "format": "csv",
            },
        )
        assert exported.status_code == 200
        assert "2024-06-10" in exported.text


def test_refetch_publishes_a_new_result_and_keeps_the_old_one_readable():
    collection = saved()
    security_id = service.get_set(collection["set_id"])["security"]["id"]
    days = session_window(date(2024, 6, 10), 6, 5)
    partial_id = seed_prices(security_id, days[0], days[6], wide=True)
    created = analysis(collection)
    first = service.get_analysis(created["analysis_id"])
    assert first["data"]["metadata"]["dataset_id"] == partial_id
    assert first["expires_at"]
    row = first["data"]["rows"][0]
    assert row["windows"]["before5"]["complete"]
    assert row["windows"]["day0"]["complete"]
    assert not row["windows"]["after5"]["complete"]
    frozen = copy.deepcopy(first["data"])
    refetch_prices(security_id, days[7], days[-1])
    plan(created["analysis_id"])
    newest = service.get_analysis(created["analysis_id"])
    assert newest["result_id"] != first["result_id"]
    assert newest["data"]["rows"][0]["windows"]["after5"]["complete"]
    assert service.get_analysis(created["analysis_id"], first["result_id"])["data"] == frozen


def test_overlapping_active_history_shared_without_duplicate_work():
    from iirp import lifecycle

    collection = saved()
    created = analysis(collection)
    days = session_window(date(2024, 6, 10), 6, 5)
    existing = lifecycle.create_collection(
        {
            "request_id": str(uuid.uuid4()),
            "kind": "market_history",
            "tickers": ["AAPL"],
            "start_date": str(days[0]),
            "end_date": str(days[-1]),
        }
    )
    with session() as s, s.begin():
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == existing["batch_id"]))
        shared = lifecycle.add_job(
            s,
            scope,
            "market_history",
            {
                "security_id": scope.security_id,
                "symbol": scope.symbol,
                "start_date": str(days[0]),
                "end_date": str(days[-1]),
            },
        )
        shared_id = shared.id
    plan(created["analysis_id"])
    assert count_acquisition_jobs() == 1
    from iirp.business_models import BatchJob

    with session() as s:
        assert (
            s.scalar(select(func.count()).select_from(BatchJob).where(BatchJob.job_id == shared_id))
            == 2
        )


def test_earnings_period_review_requires_fiscal_evidence_and_groups_by_fiscal_year():
    seed_security()
    payload = document(earnings=True)
    item = preview(payload)
    command = confirmation(item)
    command["reviews"][0].update(date_verified=True, period_verified=True)
    with pytest.raises(ValueError, match="财年/财季"):
        service.confirm_import(command)
    payload["events"][0]["sources"][0]["supports"] += ["fiscal_year", "fiscal_quarter", "period_kind"]
    item = preview(payload)
    command = confirmation(item)
    command["reviews"][0].update(date_verified=True, period_verified=True)
    collection = service.confirm_import(command)
    created = analysis(collection, current_fiscal_year=2024)
    row = service.get_analysis(created["analysis_id"])["data"]["rows"][0]
    assert row["year"] == 2023 and row["event_year"] == 2024
    assert row["category"] == "Q4" and row["group"] == "historical"


def test_cancelled_analysis_continues_same_version_cutoff_and_security():
    collection = saved()
    created = analysis(collection, current_fiscal_year=2025)
    with session() as s, s.begin():
        original = s.get(AnalysisRequest, created["analysis_id"])
        batch = s.get(Batch, original.batch_id)
        batch.status = "CANCELLED"
        batch.requested_action = "cancel"
        child = service.clone_event_analysis(s, batch, "event-continue-test")
        new = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == child.id))
        scopes = s.scalars(select(RequestScope).where(RequestScope.batch_id == child.id)).all()
        assert child.parent_id == batch.id
        assert new.params == original.params
        assert len(scopes) == 1 and scopes[0].symbol == "AAPL"
        assert service.clone_event_analysis(s, batch, "event-continue-test").id == child.id
    assert count(AnalysisRequest) == 2


def test_openapi_includes_both_import_contracts_as_prompt_authority():
    from fastapi import FastAPI
    from iirp.event_api import router

    app = FastAPI()
    app.include_router(router)
    schemas = app.openapi()["components"]["schemas"]
    assert schemas["CustomEventsImport"]["additionalProperties"] is False
    assert schemas["EarningsEventsImport"]["additionalProperties"] is False
    assert schemas["CustomEvent"]["properties"]["event_time"]
    assert "date_verified" not in schemas["CustomEvent"]["properties"]


def test_fiscal_calendar_is_frozen_with_reviewed_source_revision_window_and_csv():
    seed_security()
    payload = document(earnings=True)
    event = payload["events"][0]
    event.update(period_start="2024-01-01", period_end="2024-03-30")
    event["sources"][0]["supports"] += ["fiscal_year", "fiscal_quarter", "period_start", "period_end", "period_kind"]
    def confirm(payload, **extras):
        item = preview(payload, **extras)
        command = confirmation(item, **({"expected_version": extras["expected_version"], "revision_note": "合成来源更正"} if extras else {}))
        command["reviews"][0].update(date_verified=True, period_verified=True)
        return service.confirm_import(command)
    collection = confirm(payload)
    first = analysis(collection, current_fiscal_year=2024)
    old = service.get_analysis(first["analysis_id"])
    calendar = old["data"]["fiscal_coverage"]["quarter_calendar"]
    old_csv = service.export_analysis(first["analysis_id"], old["result_id"], "csv")
    exported = list(csv.DictReader(io.StringIO(old_csv)))
    fiscal = next(row for row in exported if row["record_type"] == "fiscal_coverage")
    assert json.loads(fiscal["statistics_or_window"])["quarter_calendar"] == calendar
    other = analysis(collection, current_fiscal_year=2024, date_window="before5")
    assert service.get_analysis(other["analysis_id"])["data"]["fiscal_coverage"]["quarter_calendar"] == calendar
    event["event_date"], event["period_start"] = "2024-07-01", "2023-12-31"
    revised = confirm(payload, set_id=collection["set_id"], expected_version=1)
    newer = analysis(revised, current_fiscal_year=2024)
    latest = service.get_analysis(newer["analysis_id"])
    assert latest["data"]["fiscal_coverage"]["quarter_calendar"] != calendar
    assert latest["data"]["metadata"]["event_version_id"] != old["data"]["metadata"]["event_version_id"]
    assert service.get_analysis(first["analysis_id"], old["result_id"])["data"] == old["data"]
    assert service.export_analysis(first["analysis_id"], old["result_id"], "csv") == old_csv


def test_requested_holiday_cutoff_freezes_previous_completed_trading_day():
    collection = saved()
    created = analysis(collection, cutoff_date="2025-01-01")
    assert service.get_analysis(created["analysis_id"])["params"]["cutoff_date"] == "2024-12-31"


def test_concurrent_different_imports_share_one_new_security():
    from concurrent.futures import ThreadPoolExecutor

    other = document()
    other["events"][0]["event_date"] = "2024-06-11"
    commands = [confirmation(preview()), confirmation(preview(other))]
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(service.confirm_import, commands))
    assert len({row["set_id"] for row in results}) == 2
    assert count(Security) == 1


def test_unknown_event_date_waits_for_fact_correction_not_price_refresh():
    seed_security()
    payload = document()
    payload["events"][0].update(event_date=None, time_precision="unknown", date_status="unverified")
    item = preview(payload)
    collection = service.confirm_import(confirmation(item))
    created = analysis(collection, include_unverified=True)
    status, reason, progress = plan(created["analysis_id"])
    assert status == "PARTIAL"
    assert "更新事件资料" in reason
    assert progress["ready_events"] == 0
    assert count_acquisition_jobs() == 0


def test_imported_q2_scope_limits_default_analysis_and_rejects_expanded_year():
    """An imported two-year Q2 query cannot become an eight-year, four-quarter study."""
    seed_security()
    payload = document(earnings=True)
    payload["scope"].update(fiscal_year_start=2024, fiscal_year_end=2025, fiscal_quarters=[2])
    payload["events"][0].update(fiscal_year=2024, fiscal_quarter=2, period_end="2024-03-30")
    payload["coverage"][0].update(fiscal_year=2024, fiscal_quarter=2)
    payload["coverage"].append({
        "fiscal_year": 2025, "fiscal_quarter": 2,
        "search_status": "not_searched", "result_status": "unresolved",
        "event_ids": [], "source_urls": [], "notes": "Synthetic FY2025 Q2 not searched",
    })
    item = preview(payload)
    command = confirmation(item)
    command["reviews"][0]["date_verified"] = True
    collection = service.confirm_import(command)
    created = analysis(collection, current_fiscal_year=2026)
    saved_result = service.get_analysis(created["analysis_id"])
    data = saved_result["data"]
    assert saved_result["params"]["years"] == [2024, 2025]
    assert data["fiscal_coverage"]["target_years"] == [2024, 2025]
    assert {row["quarter"] for row in data["fiscal_coverage"]["rankings"]} == {"Q2"}
    assert {row["quarter"] for row in data["fiscal_coverage"]["gaps"]} == {"Q2"}
    assert [row["quarter"] for row in data["fiscal_coverage"]["quarter_calendar"]] == ["Q2"]
    assert all(row["year"] in [2024, 2025] for row in data["fiscal_coverage"]["gaps"])
    with pytest.raises(ValueError, match="超出导入事件范围"):
        analysis(collection, current_fiscal_year=2026, years=[2018, 2024])
    # Selecting a subset remains allowed and is not silently broadened.
    subset = analysis(collection, current_fiscal_year=2026, years=[2024])
    assert service.get_analysis(subset["analysis_id"])["params"]["years"] == [2024]


def test_regular_fiscal_kind_cannot_be_confirmed_without_matching_source_support():
    seed_security()
    payload = document(earnings=True)
    payload["events"][0]["sources"][0]["supports"] += ["fiscal_year", "fiscal_quarter"]
    item = preview(payload)
    assert any("period_kind" in warning for warning in item["warnings"])
    command = confirmation(item)
    command["reviews"][0].update(date_verified=True, period_verified=True)
    with pytest.raises(ValueError, match="常规财期缺少支持 period_kind"):
        service.confirm_import(command)
    # Date-only observations remain available when the unsupported period
    # qualification is left unchecked. No source value is silently changed.
    command["reviews"][0]["period_verified"] = False
    saved_item = service.confirm_import(command)
    result = service.get_analysis(analysis(saved_item, current_fiscal_year=2024)["analysis_id"])["data"]
    assert result["effective_n"] == 0
    assert result["rows"][0]["date_verified"] is True


def test_excluded_title_keyword_requires_explicit_candidate_exclusion_and_preserves_raw():
    seed_security()
    payload = document()
    payload["scope"]["include_keywords"] = ["keynote"]
    payload["scope"]["exclude_keywords"] = ["synthetic EVENT"]
    item = preview(payload)
    assert any("命中排除关键词" in warning for warning in item["warnings"])
    assert item["events"][0]["excluded"] is False  # A candidate, never silently removed.
    command = confirmation(item)
    command["reviews"][0]["date_verified"] = True
    with pytest.raises(ValueError, match="名称命中排除关键词"):
        service.confirm_import(command)
    command["reviews"][0].update(selected=False, note="合成关键词范围排除，原事实保留")
    saved_item = service.confirm_import(command)
    detail = service.get_set(saved_item["set_id"])
    assert len(detail["events"]) == 1 and detail["events"][0]["excluded"] is True
    assert detail["document"]["scope"]["exclude_keywords"] == ["synthetic EVENT"]
    result = service.get_analysis(analysis(saved_item, current_fiscal_year=2025)["analysis_id"])["data"]
    assert result["effective_n"] == 0
    assert "排除关键词" in result["metadata"]["keyword_scope_policy"]


def test_unknown_current_fiscal_year_keeps_imported_scope_as_observations():
    seed_security()
    payload = document(earnings=True)
    payload["scope"].update(fiscal_year_start=2024, fiscal_year_end=2025, fiscal_quarters=[2])
    payload["events"][0].update(fiscal_year=2024, fiscal_quarter=2, period_end="2024-03-30")
    payload["coverage"][0].update(fiscal_year=2024, fiscal_quarter=2)
    payload["coverage"].append({
        "fiscal_year": 2025, "fiscal_quarter": 2,
        "search_status": "not_searched", "result_status": "unresolved",
        "event_ids": [], "source_urls": [], "notes": "Synthetic FY2025 Q2 not searched",
    })
    item = preview(payload)
    command = confirmation(item)
    command["reviews"][0]["date_verified"] = True
    collection = service.confirm_import(command)
    created = analysis(collection)  # No user-supplied or verified current FY.
    result = service.get_analysis(created["analysis_id"])
    assert result["params"]["current_fiscal_year"] is None
    assert result["params"]["years"] == [2024, 2025]
    assert result["data"]["effective_n"] == 0
    assert result["data"]["rows"][0]["group"] == "observation"


def test_direct_duplicate_years_never_duplicate_saved_coverage_rows():
    collection = saved()
    with pytest.raises(ValueError, match="重复年份"):
        analysis(collection, current_fiscal_year=2025, years=[2024, 2024])


def test_old_frozen_event_export_warns_by_its_own_version_without_rewriting_data():
    from iirp.business_models import AnalysisResult

    collection = saved()
    security_id = service.get_set(collection["set_id"])["security"]["id"]
    days = session_window(date(2024, 6, 10), 6, 5)
    seed_prices(security_id, days[0], days[-1])
    created = analysis(collection)
    result_id = service.get_analysis(created["analysis_id"])["result_id"]
    with session() as s, s.begin():
        frozen = s.get(AnalysisResult, result_id)
        old_data = copy.deepcopy(frozen.data)
        old_data["metadata"]["calculation_version"] = "event-dates-v6-robustness"
        frozen.data = old_data
    exported = json.loads(service.export_analysis(created["analysis_id"], result_id, "json"))
    assert "覆盖分母" in exported["version_notice"]
    assert exported["data"]["metadata"]["calculation_version"] == "event-dates-v6-robustness"
    rows = list(csv.DictReader(io.StringIO(service.export_analysis(created["analysis_id"], result_id, "csv"))))
    notices = [row for row in rows if row["record_type"] == "version_notice"]
    assert len(notices) == 1 and "覆盖分母" in notices[0]["statistics_or_window"]
    with session() as s:
        assert s.get(AnalysisResult, result_id).data == old_data
    with session() as s, s.begin():
        frozen = s.get(AnalysisResult, result_id)
        unknown_data = copy.deepcopy(frozen.data)
        unknown_data["metadata"].pop("calculation_version")
        frozen.data = unknown_data
    unknown_json = json.loads(service.export_analysis(created["analysis_id"], result_id, "json"))
    assert "无法确认" in unknown_json["version_notice"]
    unknown_rows = list(csv.DictReader(io.StringIO(service.export_analysis(created["analysis_id"], result_id, "csv"))))
    assert any(row["record_type"] == "version_notice" and "无法确认" in row["statistics_or_window"] for row in unknown_rows)
