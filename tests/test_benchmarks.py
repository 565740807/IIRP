"""Disposable DB tests: version fences, shared demands, capacities and exports."""

import csv
import io
import json
import uuid
from datetime import date

from iirp import lifecycle
from iirp.business_models import (
    AnalysisResult,
    Security,
)
from iirp.business_worker import _persist, prepare_target
from iirp.contracts import AnalysisInput
from iirp.db import session
from iirp.market_data import resolve_metadata
from iirp.operations import operation
from iirp.queue import claim, fenced
from iirp.storage import save_object
from sqlalchemy import select
from test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    refetch_prices,
    seed_prices,
    seed_security,
)


def baseline(symbol="^IXIC", prices=True):
    identifier = seed_security(symbol)
    with session() as s, s.begin():
        security = s.get(Security, identifier)
        security.instrument = "INDEX" if symbol.startswith("^") else "ETF"
    if prices:
        seed_prices(identifier, date(2022, 12, 1), date(2025, 1, 1), wide=True)
    return identifier


def research(**extra):
    return lifecycle.create_analysis(AnalysisInput(request_id=str(uuid.uuid4()), tickers=["AAPL"], kind="interval", current_year=2024,
        historical_years=1, start_mmdd="01-03", end_mmdd="01-10", benchmark="^IXIC", **extra).model_dump(mode="json"))


def publish(lease):
    payload = operation("research_compute", prepare_target(lease))
    source = save_object(json.dumps(payload).encode())
    assert fenced(lease, source=source, business_write=lambda s, j: _persist(s, j, payload, {}, source))
    return payload


def test_index_identity_contract_is_specific_to_composite_and_sp500():
    for symbol in ("^IXIC", "^GSPC", "^VIX"):
        identifier = seed_security(symbol)
        with session() as s, s.begin():
            security = s.get(Security, identifier)
            resolve_metadata(s, security, {"metadata": {"symbol": symbol, "quoteType": "INDEX", "currency": "USD", "exchange": "NIM"}})
            assert security.calendar == (None if symbol == "^VIX" else "XNYS")
            assert security.status == ("VERIFIED_MARKET" if symbol == "^VIX" else "VERIFIED")


def test_benchmark_refetch_rejects_old_worker_and_keeps_earlier_result_readable():
    stock = seed_security()
    seed_prices(stock, date(2022, 12, 1), date(2024, 1, 31), wide=True)
    other = baseline()
    created = research()
    lifecycle.plan_tick()
    first = claim({"research_compute"})
    publish(first)
    original = lifecycle.get_analysis(created["id"])["results"][0]
    assert original["is_current"] and original["expires_at"]
    assert claim({"research_compute"}) is None
    # A new benchmark fetch is a new input; the running computation is stale.
    b2 = refetch_prices(other, date(2024, 1, 4), date(2024, 1, 4), close=101)
    lifecycle.plan_tick()
    second = claim({"research_compute"})
    assert second.target["benchmark"]["dataset_id"] == b2
    prepared = operation("research_compute", prepare_target(second))
    b3 = refetch_prices(other, date(2024, 1, 5), date(2024, 1, 5), close=102)
    source = save_object(json.dumps(prepared).encode())
    assert fenced(second, source=source, business_write=lambda s, j: _persist(s, j, prepared, {}, source))
    with session() as s:
        assert not s.scalar(select(AnalysisResult.id).where(AnalysisResult.input_key == second.target["input_key"]))
    lifecycle.plan_tick()
    third = claim({"research_compute"})
    assert third.target["benchmark"]["dataset_id"] == b3
    publish(third)
    current = lifecycle.get_analysis(created["id"])
    assert current["results"][0]["input_version"] != original["input_version"]
    earlier = lifecycle.get_analysis(created["id"], original["result_id"])["results"][0]
    assert earlier["input_version"] == original["input_version"]
    exported = list(csv.DictReader(io.StringIO(lifecycle.export_analysis(created["id"], original["result_id"]).lstrip("\ufeff"))))
    assert {r["result_id"] for r in exported} == {original["result_id"]}
    assert "distribution" in {r["record_type"] for r in exported}
    assert json.loads(next(r for r in exported if r["record_type"] == "benchmark")["data"])["dataset_id"] == original["data"]["benchmark"]["dataset_id"]
