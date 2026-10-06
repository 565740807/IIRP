"""Event results follow the price cache they used; a refetch is a new input."""

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal

from iirp import event_service as service
from iirp.business_models import PriceCache, PriceCacheBar, Security
from iirp.db import session
from iirp.models import now
from sqlalchemy import insert, select
from test_event_service import analysis, plan, saved
from test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    seed_prices,
)


def revise_dataset(identifier, changed_day=None, *, changed_basis=False):
    """Simulate a refetch: a new cache (new id) with the same bars, one optionally revised."""
    with session() as s, s.begin():
        previous = s.get(PriceCache, identifier)
        values = {column: getattr(previous, column) for column in (
            "security_id", "start_date", "end_date", "complete_through", "provider", "details")}
        bars = [{column.name: getattr(bar, column.name) for column in PriceCacheBar.__table__.columns}
                for bar in s.scalars(select(PriceCacheBar).where(PriceCacheBar.cache_id == identifier))]
        s.delete(previous)
        s.flush()
        stamp = now()
        current = PriceCache(**values, fetched_at=stamp, expires_at=stamp + timedelta(hours=24))
        s.add(current)
        s.flush()
        for bar in bars:
            bar["cache_id"] = current.id
            if bar["session_date"] == changed_day:
                bar.update(open=Decimal(120), high=Decimal(120), low=Decimal(120), close=Decimal(120))
        if bars:
            s.execute(insert(PriceCacheBar), bars)
        return current.id


def test_same_cache_reuses_the_result_and_a_refetch_computes_again():
    collection = saved()
    security_id = service.get_set(collection["set_id"])["security"]["id"]
    original_dataset = seed_prices(security_id, date(2024, 6, 1), date(2024, 7, 1), wide=True)
    request = analysis(collection)
    original = service.get_analysis(request["analysis_id"])
    frozen = deepcopy(original["data"])
    plan(request["analysis_id"])
    assert service.get_analysis(request["analysis_id"])["result_id"] == original["result_id"]
    with session() as s, s.begin():
        s.get(Security, security_id).metadata_json = {"unrelated_provider_metadata": "new value"}
    plan(request["analysis_id"])
    assert service.get_analysis(request["analysis_id"])["result_id"] == original["result_id"]
    revise_dataset(original_dataset, date(2024, 6, 11))
    plan(request["analysis_id"])
    changed = service.get_analysis(request["analysis_id"])
    assert changed["result_id"] != original["result_id"]
    assert changed["data"]["rows"][0]["points"] != original["data"]["rows"][0]["points"]
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


def test_event_refresh_http_receipt_accepts_event_dates_and_new_document_is_readable():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from iirp.event_api import router as event_router
    from iirp.research_api import router as research_router

    collection = saved()
    security_id = service.get_set(collection["set_id"])["security"]["id"]
    seed_prices(security_id, date(2024, 6, 1), date(2024, 7, 1), wide=True)
    original = analysis(collection)
    original_id = original["analysis_id"]
    plan(original_id)
    from iirp import lifecycle
    lifecycle.plan_tick()  # The analysis has finished before it is fetched again.
    frozen = service.get_analysis(original_id)
    app = FastAPI()
    app.include_router(research_router)
    app.include_router(event_router)
    # Both production response models execute here. In particular, a custom
    # event payload must never be validated as a native ResearchResult.
    with TestClient(app, raise_server_exceptions=False) as client:
        unchanged = client.post(f"/api/v1/analyses/{original_id}/refresh")
        assert unchanged.status_code == 202 and unchanged.json()["id"] == original_id
        response = client.post(f"/api/v1/analyses/{original_id}/refresh?force=true")
        assert response.status_code == 202, response.text
        receipt = response.json()
        assert receipt["id"] != original_id
        assert receipt["batch_id"] and receipt["status"]
        assert receipt["freshness"]["origin_id"] == original_id
        assert receipt["freshness"]["latest_id"] == receipt["id"]
        assert "results" not in receipt, "refresh returns a typed command receipt"
        plan(receipt["id"])  # The reused cache answers it without a download.
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
