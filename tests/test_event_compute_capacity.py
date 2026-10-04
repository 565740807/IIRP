"""Accepted event text survives the real bounded compute channel and publication."""

import copy
import csv
import hashlib
import io
import json
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from iirp.api import app
from iirp.business_models import AnalysisResult
from iirp.business_worker import execute_business, prepare_target
from iirp.db import session
from iirp.event_models import EventSetVersion
from iirp.models import Job
from iirp.operation_pool import OperationPool
from iirp.queue import claim
from sqlalchemy import select, text
from test_event_contracts import document
from test_event_pipeline import create, enqueue, results
from test_event_service import confirmation
from test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    seed_prices,
    seed_security,
)

pytestmark = pytest.mark.slow


def fingerprint(value):
    raw = value.encode() if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str
    ).encode()
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def post(client, path, body, status=200):
    response = client.post("/api/v1/events/" + path, json=body)
    assert response.status_code == status, response.text[:300]
    return response.json()


@pytest.mark.parametrize("case", ["ordinary", "ascii_17mib", "escaped_combination", "fiscal_evidence"])
def test_accepted_notes_survive_real_pool_frozen_reads_and_exports(case):
    security = seed_security()
    seed_prices(security, date(2024, 5, 30), date(2024, 6, 17))
    payload = document(earnings=case == "fiscal_evidence")
    note = "Synthetic review 中文 \\\"\n"
    if case == "fiscal_evidence":
        payload["events"][0]["sources"][0]["supports"] += ["fiscal_year", "fiscal_quarter", "period_kind", "period_end"]
        payload["events"][0]["sources"][0]["evidence_note"] = "Frozen fiscal source 中文" * 1000
        note *= 10_000
    if case == "ascii_17mib":
        note = "x" * (17 * 1024**2)
    if case == "escaped_combination":
        # Raw UTF-8 is below 16 MiB, but JSON escapes and the second event's
        # excluded-events metadata push the old transport above its budget.
        note = '核验"\\\x01\n' * (600 * 1024)
        second = copy.deepcopy(payload["events"][0])
        second.update(client_event_id="excluded-event", event_name="Excluded fixture")
        payload["events"].append(second)
        payload["coverage"][0]["event_ids"].append(second["client_event_id"])
        for event in payload["events"]:
            event["sources"][0]["evidence_note"] = '来源"\\\n😀' * 1000
            event["notes"] = "Original document notes 中文" * 1000
        payload["coverage"][0]["notes"] = "Frozen coverage text 中文" * 1000
        payload["scope"]["include_keywords"] = ["Frozen scope guidance 中文"]

    with session() as s:
        database = s.scalar(text("select current_database()"))
        assert database.startswith("iirp_v1_test_")
    with TestClient(app, headers={"X-IIRP-Client": "web"}) as client:
        preview = post(client, "preview", {"text": json.dumps(payload, ensure_ascii=False)})
        command = confirmation(preview)
        for index, review in enumerate(command["reviews"]):
            review.update(note=note + str(index), date_verified=True, selected=index == 0,
                          period_verified=case == "fiscal_evidence")
        if case == "ascii_17mib":
            command["reviews"][0]["note"] = note
        saved = post(client, "confirm", command)
        created = post(client, f"sets/{saved['set_id']}/analyses", {
            "request_id": str(uuid.uuid4()), "version": 1, "cutoff_date": "2025-01-01",
            "current_fiscal_year": 2024,
        }, 202)
        enqueue(created["analysis_id"])
        job = claim({"event_compute"})
        assert job is not None
        with session() as s:
            revision = s.get(EventSetVersion, job.target["inputs"]["event_version_id"])
            frozen_events = copy.deepcopy(revision.events)
        metrics = {"case": case, "database": database,
                   "confirm_request_bytes": len(json.dumps(command, ensure_ascii=False).encode()),
                   "notes": [fingerprint(e["review"]["note"]) for e in frozen_events]}
        newer = None

        class MeasuredPool(OperationPool):
            def run(self, kind, target, checkpoint):
                nonlocal newer
                metrics["operation_input_bytes"] = len(json.dumps(
                    {"kind": kind, "target": target}, ensure_ascii=False, default=str
                ).encode())
                response = super().run(kind, target, checkpoint)
                metrics["operation_output_bytes"] = len(json.dumps(response, ensure_ascii=False).encode())
                if case == "escaped_combination":
                    with session() as s:
                        assert s.get(Job, job.id).status == "RUNNING"
                    updated = copy.deepcopy(payload)
                    for event in updated["events"]:
                        event["sources"][0]["evidence_note"] = "NEW source; must not replace frozen source"
                    item = post(client, "preview", {"text": json.dumps(updated),
                        "set_id": saved["set_id"], "expected_version": 1})
                    reviews = confirmation(item, expected_version=1, revision_note="New verification")
                    for review in reviews["reviews"]:
                        review.update(note="NEW note; must not replace frozen note", date_verified=False)
                    newer = post(client, "confirm", reviews)
                return response

        pool = MeasuredPool()
        try:
            execute_business(job, runner=pool)
        finally:
            pool.close()
        with session() as s:
            completed = s.get(Job, job.id)
            metrics.update(status=completed.status, error=completed.error)
            print(json.dumps(metrics, ensure_ascii=False))
            assert completed.status == "SUCCEEDED", completed.error
            result = s.scalar(select(AnalysisResult).where(AnalysisResult.analysis_id == created["analysis_id"]))
            assert result is not None
            result_id = result.id
            stored = copy.deepcopy(result.data)
            assert result.inputs == job.target["inputs"]
        expected = {event["client_event_id"]: event for event in frozen_events}

        def check_data(data):
            rows = [*data["rows"], *data["coverage_rows"]]
            assert len(rows) == len(expected)
            for row in rows:
                event = expected[row["key"]]
                assert fingerprint(row["review_note"]) == fingerprint(event["review"]["note"])
                assert fingerprint(row["sources"]) == fingerprint(event["sources"])
                assert row["date_verified"] is True
                assert row["date_verified_at"] == event["review"]["confirmed_at"]
            assert fingerprint(data["metadata"]["coverage"]) == fingerprint(payload["coverage"])
            assert data["metadata"]["inclusion_rule"] == payload["scope"]
            for excluded in data["metadata"]["excluded_events"]:
                assert fingerprint(excluded["note"]) == fingerprint(expected[excluded["client_event_id"]]["review"]["note"])
            assert data["metadata"]["event_version_id"] == job.target["inputs"]["event_version_id"]
            if case == "fiscal_evidence":
                entries = [entry for quarter in data["fiscal_coverage"]["quarter_calendar"]
                           for entry in quarter["entries"] if entry["key"] is not None]
                assert len(entries) == 1
                entry = entries[0]
                assert entry["announcement_status"] == entry["period_end_status"] == "confirmed"
                assert fingerprint(entry["review_note"]) == fingerprint(frozen_events[0]["review"]["note"])
                assert fingerprint(entry["sources"]) == fingerprint(frozen_events[0]["sources"])

        check_data(stored)
        path = f"/api/v1/events/analyses/{created['analysis_id']}"
        response = client.get(path, params={"result_id": result_id})
        assert response.status_code == 200
        check_data(response.json()["data"])
        exported = client.get(path + "/export", params={"result_id": result_id, "format": "json"})
        assert exported.status_code == 200
        check_data(exported.json()["data"])
        exported = client.get(path + "/export", params={"result_id": result_id, "format": "csv"})
        assert exported.status_code == 200
        old_limit = csv.field_size_limit(128 * 1024**2)
        try:
            records = list(csv.DictReader(io.StringIO(exported.text)))
        finally:
            csv.field_size_limit(old_limit)
        details = [json.loads(row["statistics_or_window"]) for row in records
                   if row["record_type"] in {"event_evidence", "coverage_event"}]
        assert len(details) == len(expected)
        for row in details:
            event = expected[row["key"]]
            assert fingerprint(row["review_note"]) == fingerprint(event["review"]["note"])
            assert fingerprint(row["sources"]) == fingerprint(event["sources"])
        if case == "fiscal_evidence":
            fiscal = next(row for row in records if row["record_type"] == "fiscal_coverage")
            assert fingerprint(json.loads(fiscal["statistics_or_window"])) == fingerprint(stored["fiscal_coverage"])
        if case == "escaped_combination":
            assert newer["version"] == 2
            assert sum(item["bytes"] for item in metrics["notes"]) < 16 * 1024**2
        metrics.update(frozen_result_sha256=fingerprint(stored)["sha256"],
                       json_and_csv_notes_and_sources_verified=True,
                       newer_version=newer["version"] if newer else None)
        print(json.dumps(metrics, ensure_ascii=False))


@pytest.mark.parametrize("stage", ["prepare", "publish"])
def test_frozen_text_integrity_is_checked_before_compute_and_publication(stage):
    created, _ = create()
    enqueue(created["analysis_id"])
    job = claim({"event_compute"})

    def corrupt():
        # Simulate damage to the referenced immutable row, not a valid revision.
        with session() as s, s.begin():
            revision = s.get(EventSetVersion, job.target["inputs"]["event_version_id"])
            events = copy.deepcopy(revision.events)
            events[0]["review"]["note"] = "Unexpected change to frozen text"
            revision.events = events

    if stage == "prepare":
        corrupt()
        with pytest.raises(ValueError, match="冻结事件内容与任务摘要不一致"):
            prepare_target(job)
    else:
        class CorruptAfterCompute(OperationPool):
            def run(self, *args):
                response = super().run(*args)
                corrupt()
                return response
        pool = CorruptAfterCompute()
        try:
            execute_business(job, runner=pool)
        finally:
            pool.close()
        with session() as s:
            assert s.get(Job, job.id).result["result_id"] is None
    assert results() == 0


@pytest.mark.parametrize("kind", ["research_compute", "market_history"])
def test_other_operation_inputs_still_enforce_the_same_budget(kind):
    pool = OperationPool()
    try:
        with pytest.raises(ValueError, match="操作输入超过16MiB预算"):
            pool.run(kind, {"text": "x" * (16 * 1024**2)})
    finally:
        pool.close()
