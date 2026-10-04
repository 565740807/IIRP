"""Frozen versions stay exact while latest pointers advance with actual inputs."""
import copy
import json
from datetime import date, datetime, timezone

from iirp import lifecycle
from iirp.business_models import AnalysisRequest, AnalysisResult, Batch
from iirp.db import session
from sqlalchemy import select
from test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    seed_prices,
    seed_security,
)
from test_performance_pipeline import params


def _saved(monkeypatch):
    monkeypatch.setattr(lifecycle, "now", lambda: datetime(2023, 1, 5, 23, tzinfo=timezone.utc))
    security = seed_security()
    dataset = seed_prices(security, date(2023, 1, 3), date(2023, 1, 5))
    view = lifecycle.create_analysis(params(kind="interval", start_mmdd="01-03", end_mmdd="01-05"))
    with session() as s, s.begin():
        s.get(Batch, view["batch_id"]).status = "SUCCEEDED"
        result = AnalysisResult(analysis_id=view["id"], security_id=security, input_key="old-key", inputs={"dataset_id": dataset, "params": view["params"]}, data={"kind": "interval", "effective_n": 1, "metadata": {"cutoff_date": "2023-01-05"}, "series": [], "rows": [], "summary": {}})
        s.add(result)
        s.flush()
        result_id = result.id
    return view, result_id


def test_follow_latest_keeps_frozen_params_results_and_exports(monkeypatch):
    old, result_id = _saved(monkeypatch)
    before = copy.deepcopy(lifecycle.get_analysis(old["id"], result_id))
    monkeypatch.setattr(lifecycle, "now", lambda: datetime(2023, 1, 6, 23, tzinfo=timezone.utc))
    latest = lifecycle.refresh_analysis(old["id"])
    assert latest["id"] != old["id"]
    assert latest["params"]["cutoff_date"] == "2023-01-06"
    assert latest["results"][0]["data"] == before["results"][0]["data"]
    assert latest["results"][0]["result_cutoff"] == "2023-01-05"
    assert latest["results"][0]["carried_from"] == result_id
    frozen = lifecycle.get_analysis(old["id"], result_id)
    assert frozen["params"] == before["params"]
    assert frozen["results"] == before["results"]
    exported = lifecycle.export_analysis(latest["id"])
    assert "2023-01-05" in exported
    assert "2023-01-06" not in exported
    frozen_json = lifecycle.export_analysis(latest["id"], format="json")
    document = json.loads(frozen_json)
    assert document["results"][0]["params"]["cutoff_date"] == "2023-01-05"
    assert document["results"][0]["data"] == before["results"][0]["data"]
    with session() as s, s.begin():
        s.get(Batch, latest["batch_id"]).status = "FAILED"
    assert lifecycle.export_analysis(latest["id"], format="json") == frozen_json
    with session() as s, s.begin():
        s.get(Batch, latest["batch_id"]).status = "QUEUED"
    repeated = lifecycle.refresh_analysis(old["id"])
    assert repeated["id"] == latest["id"]
    with session() as s:
        assert len(s.scalars(select(AnalysisRequest)).all()) == 2


def test_legacy_coverage_rechecks_immutable_dataset_without_rewriting(monkeypatch):
    old, result_id = _saved(monkeypatch)
    view = lifecycle.get_analysis(old["id"], result_id)
    item = view["results"][0]
    assert item["coverage_basis"] == "rechecked_frozen_dataset"
    assert item["coverage"] is not None
    with session() as s:
        assert "coverage" not in s.get(AnalysisResult, result_id).inputs


def test_acceptance_label_is_not_a_financial_condition():
    seed_security()
    values = params()
    values["research_label"] = "验收20260922-隔离测试"
    view = lifecycle.create_analysis(values)
    assert view["batch"]["title"].startswith("验收20260922")
    assert "research_label" not in view["params"]


def test_concurrent_refresh_requests_share_one_child(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    old, _ = _saved(monkeypatch)
    monkeypatch.setattr(lifecycle, "now", lambda: datetime(2023, 1, 6, 23, tzinfo=timezone.utc))
    with ThreadPoolExecutor(max_workers=3) as pool:
        views = list(pool.map(lambda _: lifecycle.refresh_analysis(old["id"]), range(3)))
    assert len({view["id"] for view in views}) == 1
    with session() as s:
        assert len(s.scalars(select(AnalysisRequest)).all()) == 2
