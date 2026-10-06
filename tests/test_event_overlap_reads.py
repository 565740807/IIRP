"""Frozen paging, compatibility and exports with real PostgreSQL and pool."""

import base64
import copy
import csv
import io
import json
import uuid
from datetime import date
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from iirp import event_service as service
from iirp.api import app
from iirp.business_models import AnalysisResult
from iirp.business_worker import execute_business
from iirp.db import session
from iirp.event_models import EventSet, EventSetVersion
from iirp.models import Job
from iirp.operation_pool import OperationPool
from iirp.queue import claim
from sqlalchemy import select
from test_event_dependencies import revise_dataset
from test_event_pipeline import create, enqueue
from test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401


def published(count=123):
    created, dataset = create()
    with session() as s, s.begin():
        revision = s.scalar(select(EventSetVersion))
        template = revision.events[0]
        revision.events = [
            {
                **copy.deepcopy(template),
                "client_event_id": f"event-{n:03}",
                "event_name": f"Event {n}",
            }
            for n in range(count)
        ]
    enqueue(created["analysis_id"])
    job = claim({"event_compute"})
    pool = OperationPool()
    try:
        execute_business(job, runner=pool)
    finally:
        pool.close()
    with session() as s:
        assert s.get(Job, job.id).status == "SUCCEEDED"
    view = service.get_analysis(created["analysis_id"])
    return created, dataset, view, job


def page_url(view, key="event-000"):
    return (
        f"/api/v1/analyses/{view['id']}/results/{view['result_id']}/event-overlaps?event_key={key}"
    )


def all_pages(client, view, key="event-000", limit=17):
    output = []
    cursor = None
    while True:
        response = client.get(
            page_url(view, key),
            params={"event_key": key, "limit": limit, **({"cursor": cursor} if cursor else {})},
        )
        assert response.status_code == 200, response.text
        page = response.json()
        output.extend(x["event_key"] for x in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            return output


@pytest.mark.slow
def test_real_pool_paging_exports_and_frozen_revisions():
    created, dataset, view, job = published()
    with TestClient(app) as client:
        url = page_url(view)
        initial = client.get(url).json()
        assert initial["total"] == 122 and len(initial["items"]) == 50
        ids = [f"event-{n:03}" for n in range(1, 123)]
        assert all_pages(client, view) == ids
        assert view["data"]["rows"][0]["overlap"] == initial["summary"]
        assert initial["summary"]["preview_event_ids"] == ids[:10]
        assert all("overlapping_event_ids" not in row for row in view["data"]["rows"])
        original = service.export_analysis(view["id"], view["result_id"], "json")
        csv_text = service.export_analysis(view["id"], view["result_id"], "csv")
        evidence = [
            json.loads(x["statistics_or_window"])
            for x in csv.DictReader(io.StringIO(csv_text))
            if x["record_type"] == "event_evidence"
        ]
        assert evidence[0]["overlap"] == initial["summary"]
        assert json.loads(original)["data"] == view["data"]
        revise_dataset(dataset, date(2024, 6, 11))
        with session() as s, s.begin():
            old = s.get(EventSetVersion, job.target["inputs"]["event_version_id"])
            # A new immutable version, not an in-place edit of the old inputs.
            newer = EventSetVersion(
                set_id=old.set_id,
                version=old.version + 1,
                document=copy.deepcopy(old.document),
                events=[{**e, "event_date": "2024-07-10"} for e in old.events],
                content_hash="e2-new-source-version",
                preview_id=old.preview_id,
                reviews=old.reviews,
                revision_note="synthetic E2 revision",
            )
            s.add(newer)
            s.get(EventSet, old.set_id).version = newer.version
        assert client.get(url).json() == initial
        assert service.export_analysis(view["id"], view["result_id"], "json") == original
        assert service.export_analysis(view["id"], view["result_id"], "csv") == csv_text
        assert service.get_analysis(view["id"], view["result_id"])["data"] == view["data"]
        for params in (
            {"limit": 0},
            {"limit": 101},
            {"cursor": "invalid"},
        ):
            assert client.get(url, params={"event_key": "event-000", **params}).status_code == 422
        for offset in (122, 123, -1):
            cursor = base64.urlsafe_b64encode(
                json.dumps([view["result_id"], sha256(b"event-000").hexdigest(), offset]).encode()
            ).decode()
            response = client.get(url, params={"event_key": "event-000", "cursor": cursor})
            assert response.status_code == 422
            assert ("超出范围" if offset >= 122 else "无效") in response.json()["detail"]
        assert client.get(page_url(view, "absent")).status_code == 404
        assert (
            client.get(
                page_url(view, "event-001"),
                params={"event_key": "event-001", "cursor": initial["next_cursor"]},
            ).status_code
            == 422
        )
        wrong = {**view, "id": str(uuid.uuid4())}
        assert client.get(page_url(wrong)).status_code == 404
        wrong = {**view, "result_id": str(uuid.uuid4())}
        assert client.get(page_url(wrong)).status_code == 404


def test_legacy_frozen_read_and_native_container():
    _, _, view, _ = published(13)
    with session() as s, s.begin():
        result = s.get(AnalysisResult, view["result_id"])
        data = copy.deepcopy(result.data)
        data["metadata"].pop("representation_version")
        for row in data["rows"]:
            row.pop("overlap")
            row["overlapping_event_ids"] = [
                x["key"] for x in data["rows"] if x["key"] != row["key"]
            ]
        result.data = data
    before = service.export_analysis(view["id"], view["result_id"], "json")
    with TestClient(app) as client:
        page = client.get(page_url(view)).json()
        assert page["representation_version"] == "legacy-full-list-v1"
        assert all_pages(client, view, limit=3) == data["rows"][0]["overlapping_event_ids"]
        assert service.export_analysis(view["id"], view["result_id"], "json") == before
        # Native earnings stores exactly the same observer beneath this key.
        with session() as s, s.begin():
            s.get(AnalysisResult, view["result_id"]).data = {
                "kind": "earnings",
                "date_observation": data,
            }
        assert client.get(page_url(view)).json() == page


def test_frozen_interval_endpoint_and_missing_window_pages():
    from iirp.analytics.event_overlaps import summarize_overlaps
    from test_event_overlaps import oracle

    _, _, view, _ = published(8)
    spans = [(1, 3), (3, 5), (1, 3), (2, 2), (1, 9), (10, 11), None, (3, 3)]
    with session() as s, s.begin():
        result = s.get(AnalysisResult, view["result_id"])
        data = copy.deepcopy(result.data)
        for row, span in zip(data["rows"], spans, strict=True):
            row["points"] = [] if span is None else [{"date": f"2024-06-{day:02}"} for day in span]
        summarize_overlaps(data["rows"])
        result.data = data
    with TestClient(app) as client:
        for row in data["rows"]:
            assert all_pages(client, view, row["key"], limit=2) == oracle(data["rows"], row)


def test_representation_identity_does_not_reuse_legacy_cache(monkeypatch):
    from iirp.business_models import AnalysisRequest, PriceCache, Security
    from iirp.event_service import event_input_key

    _, _, view, job = published(13)
    with session() as s, s.begin():
        request = s.get(AnalysisRequest, view["id"])
        dataset = s.get(PriceCache, job.target["inputs"]["dataset_id"])
        security = s.get(Security, job.target["security_id"])
        current = event_input_key(request, dataset, security)
        with monkeypatch.context() as patch:
            patch.setattr(service, "REPRESENTATION_VERSION", "synthetic-other-representation")
            assert event_input_key(request, dataset, security) != current
        params = dict(request.params)
        result = s.get(AnalysisResult, view["result_id"])
        result.input_key = "synthetic-legacy-input-key"
    new = service.create_analysis(
        params["event_set_id"],
        {"request_id": str(uuid.uuid4()), "version": 1, "cutoff_date": params["cutoff_date"]},
    )
    assert service.get_analysis(new["analysis_id"])["data"] is None
    assert service.get_analysis(view["id"], view["result_id"])["data"] == view["data"]


def test_native_earnings_cache_requires_current_overlap_representation(monkeypatch):
    from iirp.analytics.research import CALCULATION_VERSION
    from iirp.business_models import AnalysisRequest, Batch, PriceCache, Security
    from iirp.research_pipeline import reuse_result

    _, _, view, job = published(13)
    params = {"kind": "earnings", "current_fiscal_year": 2025, "years": [2024]}
    with session() as s, s.begin():
        old_request = s.get(AnalysisRequest, view["id"])
        old_request.params = params
        old_result = s.get(AnalysisResult, view["result_id"])
        observer = copy.deepcopy(old_result.data)
        observer["metadata"].pop("representation_version")
        old_result.data = {"metadata": {"params": params}, "date_observation": observer}
        old_result.inputs = {**old_result.inputs, "calculation_version": CALCULATION_VERSION}
        old_result.input_key = "legacy-native-key"
        batch = Batch(
            request_id=str(uuid.uuid4()),
            scope_key=str(uuid.uuid4()),
            kind="earnings",
            title="E2 cache",
            params=params,
        )
        s.add(batch)
        s.flush()
        request = AnalysisRequest(batch_id=batch.id, params=params)
        s.add(request)
        s.flush()
        security = s.get(Security, job.target["security_id"])
        dataset = s.get(PriceCache, job.target["inputs"]["dataset_id"])
        # Even a legacy-key compatibility match cannot authorize copying a huge
        # old representation into a new result. Exercise the fallback cache path.
        monkeypatch.setattr("iirp.lifecycle.research_input_key", lambda *a: "legacy-native-key")
        assert reuse_result(s, request, security, dataset, [], {}, "new-key") is None
