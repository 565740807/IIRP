"""Bounded input fingerprints retain frozen event results through unrelated updates."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from uuid import uuid4

from iirp import event_service as service
from iirp.business_models import DatasetBar, MarketBar, PriceDataset, Security
from iirp.db import session
from iirp.models import now
from sqlalchemy import select
from test_event_service import analysis, plan, saved
from test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    seed_prices,
)


def revise_dataset(identifier, changed_day=None, *, changed_basis=False):
    with session() as s, s.begin():
        previous = s.get(PriceDataset, identifier)
        current = PriceDataset(security_id=previous.security_id, basis=previous.basis,
            basis_key="new-split-basis" if changed_basis else previous.basis_key,
            status="PUBLISHED", manifest=previous.manifest, published_at=now())
        s.add(current)
        s.flush()
        for link in s.scalars(select(DatasetBar).where(DatasetBar.dataset_id == previous.id)):
            bar = s.get(MarketBar, link.bar_id)
            if link.session_date == changed_day:
                revised = MarketBar(security_id=bar.security_id, session_date=bar.session_date,
                    provider=bar.provider, source_hash=bar.source_hash, record_hash=uuid4().hex,
                    open=Decimal(120), high=Decimal(120), low=Decimal(120), close=Decimal(120),
                    volume=bar.volume, status="VALID")
                s.add(revised)
                s.flush()
                bar = revised
            s.add(DatasetBar(dataset_id=current.id, session_date=link.session_date, bar_id=bar.id))
        return current.id


def test_outside_event_window_reuses_result_inside_revision_and_split_basis_invalidate():
    collection = saved()
    security_id = service.get_set(collection["set_id"])["security"]["id"]
    original_dataset = seed_prices(security_id, date(2024, 6, 1), date(2024, 7, 1))
    request = analysis(collection)
    original = service.get_analysis(request["analysis_id"])
    frozen = deepcopy(original["data"])
    unrelated = revise_dataset(original_dataset, date(2024, 7, 1))
    plan(request["analysis_id"])
    reused = service.get_analysis(request["analysis_id"])
    assert reused["result_id"] == original["result_id"]
    assert reused["data"]["metadata"]["dataset_id"] == original_dataset
    with session() as s, s.begin():
        s.get(Security, security_id).metadata_json = {"unrelated_provider_metadata": "new value"}
    plan(request["analysis_id"])
    assert service.get_analysis(request["analysis_id"])["result_id"] == original["result_id"]
    relevant = revise_dataset(unrelated, date(2024, 6, 11))
    plan(request["analysis_id"])
    changed = service.get_analysis(request["analysis_id"])
    assert changed["result_id"] != original["result_id"]
    assert changed["data"]["rows"][0]["points"] != original["data"]["rows"][0]["points"]
    revise_dataset(relevant, changed_basis=True)
    plan(request["analysis_id"])
    assert service.get_analysis(request["analysis_id"])["result_id"] != changed["result_id"]
    assert service.get_analysis(request["analysis_id"], original["result_id"])["data"] == frozen


def test_event_freshness_is_live_but_export_and_research_inputs_are_fixed(monkeypatch):
    from iirp import research_freshness
    from iirp.business_models import AnalysisRequest, Batch

    collection = saved()
    request = analysis(collection, retry_generation="acceptance-safe-retry")
    identifier = request["analysis_id"]
    view = service.get_analysis(identifier)
    assert view["freshness"]["origin_id"] == identifier
    assert view["result_cutoff"] == view["params"]["cutoff_date"]
    exported = service.export_analysis(identifier, view["result_id"])
    with session() as s:
        stored = s.get(AnalysisRequest, identifier)
        assert "retry_generation" not in stored.params
        assert s.get(Batch, stored.batch_id).params["retry_generation"] == "acceptance-safe-retry"
    monkeypatch.setattr(research_freshness, "freshness", lambda *_: {"refresh_checked_at": "a later live check"})
    assert service.get_analysis(identifier)["freshness"]["refresh_checked_at"] == "a later live check"
    assert service.export_analysis(identifier, view["result_id"]) == exported
    assert '"freshness"' not in exported


def test_late_carried_result_never_hides_current_or_newer_price_version():
    from iirp.business_models import AnalysisResult

    collection = saved()
    security_id = service.get_set(collection["set_id"])["security"]["id"]
    original_dataset = seed_prices(security_id, date(2024, 6, 1), date(2024, 7, 1))
    request = analysis(collection)
    identifier = request["analysis_id"]
    old = service.get_analysis(identifier)
    revised_dataset = revise_dataset(original_dataset, date(2024, 6, 11))
    plan(identifier)
    new = service.get_analysis(identifier)
    assert new["result_id"] != old["result_id"]
    with session() as s, s.begin():
        original = s.get(AnalysisResult, old["result_id"])
        carried = AnalysisResult(analysis_id=identifier, security_id=security_id,
            input_key="carry:" + original.id,
            inputs={**deepcopy(original.inputs), "carried_from": original.id},
            data=deepcopy(original.data))
        s.add(carried)
        s.flush()
        carried_id = carried.id
        # Actual persistence order: no backdating or fabricated timestamps.
        assert carried.created_at > s.get(AnalysisResult, new["result_id"]).created_at
    assert service.get_analysis(identifier)["result_id"] == new["result_id"]
    assert service.get_analysis(identifier, carried_id)["data"] == old["data"]
    assert service.get_analysis(identifier, old["result_id"])["data"] == old["data"]
    # An uncomputed newer input leaves no exact match. The actual newer dataset
    # is still preferable to a later-arriving carry of an earlier dataset.
    revise_dataset(revised_dataset, date(2024, 6, 12))
    assert service.get_analysis(identifier)["result_id"] == new["result_id"]


def test_event_refresh_http_receipt_accepts_event_dates_and_new_document_is_readable():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from iirp.event_api import router as event_router
    from iirp.research_api import router as research_router

    collection = saved()
    security_id = service.get_set(collection["set_id"])["security"]["id"]
    seed_prices(security_id, date(2024, 6, 1), date(2024, 7, 1))
    original = analysis(collection)
    original_id = original["analysis_id"]
    frozen = service.get_analysis(original_id)
    app = FastAPI()
    app.include_router(research_router)
    app.include_router(event_router)
    # Both production response models execute here. In particular, a custom
    # event payload must never be validated as a native ResearchResult.
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(f"/api/v1/analyses/{original_id}/refresh")
        assert response.status_code == 202, response.text
        receipt = response.json()
        assert receipt["id"] != original_id
        assert receipt["batch_id"] and receipt["status"]
        assert receipt["freshness"]["origin_id"] == original_id
        assert receipt["freshness"]["latest_id"] == receipt["id"]
        assert "results" not in receipt, "refresh returns a typed command receipt"
        viewed = client.get(f"/api/v1/events/analyses/{receipt['id']}")
        assert viewed.status_code == 200, viewed.text
        result = viewed.json()
        assert result["params"]["kind"] == "event_dates"
        assert result["params"]["event_version_id"] == frozen["params"]["event_version_id"]
        assert result["result_id"]
        assert result["data"]["metadata"]["research_kind"] == "custom"
        assert result["data"]["rows"][0]["anchor"]["original_date"] == "2024-06-10"
        assert result["data"]["rows"][0]["points"]
        old = client.get(f"/api/v1/events/analyses/{original_id}",
            params={"result_id": frozen["result_id"]})
        assert old.status_code == 200, old.text
        assert old.json()["data"] == frozen["data"]


def test_carried_event_exports_use_frozen_conditions_and_ignore_live_task_changes():
    import csv
    import io
    import json

    from iirp.business_models import AnalysisRequest, AnalysisResult, Batch, RequestScope

    collection = saved()
    security_id = service.get_set(collection["set_id"])["security"]["id"]
    seed_prices(security_id, date(2024, 6, 1), date(2024, 7, 1))
    original = analysis(collection, cutoff_date="2024-06-11", date_window="before5")
    frozen = service.get_analysis(original["analysis_id"])
    newer = analysis(collection, cutoff_date="2025-01-01", date_window="after5")
    identifier = newer["analysis_id"]
    with session() as s, s.begin():
        source = s.get(AnalysisResult, frozen["result_id"])
        carried = AnalysisResult(analysis_id=identifier, security_id=security_id,
            input_key="carry:" + source.id,
            inputs={**deepcopy(source.inputs), "carried_from": source.id},
            data=deepcopy(source.data))
        s.add(carried)
        s.flush()
        carried_id = carried.id
    # The active GET retains the new request's target; only the export freezes
    # the selected result's parameters, cutoff, and version metadata.
    active = service.get_analysis(identifier, carried_id)
    assert active["params"]["cutoff_date"] == "2024-12-31"
    assert active["params"]["date_window"] == "after5"
    exported_json = service.export_analysis(identifier, carried_id)
    exported_csv = service.export_analysis(identifier, carried_id, "csv")
    exported = json.loads(exported_json)
    assert exported["params"]["cutoff_date"] == "2024-06-11"
    assert exported["params"]["date_window"] == "before5"
    assert exported["result_cutoff"] == "2024-06-11"
    assert exported["data"] == frozen["data"]
    assert exported["inputs"]["params"] == exported["params"]
    assert exported["parameter_basis"] == "result_inputs"
    assert not {"freshness", "status", "requested_action", "progress"}.intersection(exported)
    assert [row["id"] for row in exported["results"]] == [carried_id]
    csv_rows = list(csv.DictReader(io.StringIO(exported_csv)))
    assert csv_rows
    for row in csv_rows:
        assert row["result_id"] == carried_id
        assert row["cutoff_date"] == "2024-06-11"
        assert json.loads(row["parameters"]) == exported["params"]
    with session() as s, s.begin():
        request = s.get(AnalysisRequest, identifier)
        batch = s.get(Batch, request.batch_id)
        batch.status, batch.requested_action = "RUNNING", "pause"
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id))
        scope.status, scope.wait_reason = "RUNNING", "Later background progress"
        scope.checkpoint = {"ready_events": 0, "event_count": 1}
        selected = s.get(AnalysisResult, carried_id)
        s.add(AnalysisResult(analysis_id=identifier, security_id=security_id,
            input_key="next:" + uuid4().hex, inputs=deepcopy(selected.inputs),
            data=deepcopy(selected.data)))
    assert service.get_analysis(identifier, carried_id)["status"] == "RUNNING"
    assert service.export_analysis(identifier, carried_id) == exported_json
    assert service.export_analysis(identifier, carried_id, "csv") == exported_csv


def test_legacy_carried_export_does_not_guess_new_request_parameters():
    import json

    from iirp.business_models import AnalysisResult

    collection = saved()
    source_request = analysis(collection, cutoff_date="2024-06-11")
    frozen = service.get_analysis(source_request["analysis_id"])
    new_request = analysis(collection, cutoff_date="2025-01-01")
    with session() as s, s.begin():
        source = s.get(AnalysisResult, frozen["result_id"])
        inputs = {key: value for key, value in source.inputs.items() if key != "params"}
        data = deepcopy(source.data)
        data["metadata"].pop("params", None)
        carried = AnalysisResult(analysis_id=new_request["analysis_id"], security_id=source.security_id,
            input_key="legacy-carry:" + source.id,
            inputs={**inputs, "carried_from": source.id}, data=data)
        s.add(carried)
        s.flush()
        carried_id = carried.id
    exported = json.loads(service.export_analysis(new_request["analysis_id"], carried_id))
    assert exported["params"] == {}
    assert exported["parameter_basis"] == "unknown"
    assert exported["result_cutoff"] == "2024-06-11"
    assert exported["data"] == data
