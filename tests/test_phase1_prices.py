"""Price revisions exercised through real PostgreSQL, source objects and lease fencing."""

from decimal import Decimal

import pytest
from iirp.business_models import CorporateAction, CoverageSegment, PriceDataset
from iirp.db import session
from iirp.market_data import price_bars
from iirp.models import SourceObject
from sqlalchemy import select
from test_market_lifecycle import (  # noqa: F401
    bar,
    clean_market,
    commit_prices,
    market_database,
    published,
    response,
)
from test_market_lifecycle import (
    security_id as security_fixture,
)

security_id = security_fixture


def split_history(security_id):
    commit_prices(security_id, response([
        bar("2023-01-03", "80"), bar("2023-01-04", "80"),
        bar("2023-01-05", "100", splits="1.25"),
    ]))
    return published(security_id)


@pytest.mark.parametrize("replacement", ["0", "1", "2"])
def test_split_revision_rebuilds_full_history_and_preserves_frozen_version(security_id, replacement):
    old = split_history(security_id)
    result, source = commit_prices(security_id, response([
        bar("2023-01-04", "100"), bar("2023-01-05", "100", splits=replacement),
    ]), start="2023-01-04")
    assert result["rebase"] and not result["published"] and not result["eligible"]
    assert published(security_id).id == old.id
    with session() as s:
        pending = s.scalar(select(PriceDataset).where(PriceDataset.status == "BUILDING"))
        assert pending.manifest["verified_splits"] == (
            {"2023-01-05": "2"} if replacement == "2" else {}
        )
        assert s.get(SourceObject, source["sha256"])
        assert s.scalar(select(CorporateAction).where(CorporateAction.value == Decimal("1.25")))
    result, _ = commit_prices(security_id, response([bar("2023-01-03", "100")]), end="2023-01-03")
    assert result["published"] and result["eligible"]
    with session() as s:
        current, _ = price_bars(s, security_id)
        frozen, frozen_dataset = price_bars(s, security_id, old.id)
        assert [Decimal(x["close"]) for x in current] == [100, 100, 100]
        assert [Decimal(x["close"]) for x in frozen] == [80, 80, 100]
        assert frozen_dataset.manifest == old.manifest


@pytest.mark.parametrize("metadata", [
    {"symbol": "OTHER"}, {"currency": "EUR"}, {"instrumentType": "ETF"},
])
def test_explicit_identity_conflict_retains_source_but_blocks_publication(security_id, metadata):
    old = split_history(security_id)
    payload = response([bar("2023-01-03", "99"), bar("2023-01-04", "99"), bar("2023-01-05", "99")])
    payload["metadata"] = metadata
    result, source = commit_prices(security_id, payload)
    assert not result["published"] and not result["eligible"]
    assert published(security_id).id == old.id
    with session() as s:
        assert s.get(SourceObject, source["sha256"])
        segment = s.scalar(select(CoverageSegment).where(CoverageSegment.source_hash == source["sha256"]))
        assert segment.status == "PARTIAL" and "冲突" in segment.details["reason"]


@pytest.mark.parametrize("mode", ["field_absent", "null", "truncated", "unreliable"])
def test_unknown_action_evidence_cannot_revoke_split_or_mix_prices(security_id, mode):
    old = split_history(security_id)
    payload = response([bar("2023-01-03", "100"), bar("2023-01-04", "100"), bar("2023-01-05", "100")])
    if mode == "field_absent":
        payload["records"][-1].pop("splits")
    elif mode == "null":
        payload["records"][-1]["splits"] = None
    elif mode == "truncated":
        payload["records"].pop(0)
    else:
        payload["actions_complete"] = False
    result, source = commit_prices(security_id, payload)
    assert not result["published"] and not result["eligible"]
    assert published(security_id).id == old.id
    assert published(security_id).manifest["verified_splits"] == {"2023-01-05": "1.25"}
    with session() as s:
        assert s.get(SourceObject, source["sha256"])
        assert not s.scalar(select(PriceDataset).where(PriceDataset.status == "BUILDING"))


def test_split_date_move_replaces_both_dates_and_rebuilds_earlier_prices(security_id):
    old = split_history(security_id)
    result, _ = commit_prices(security_id, response([
        bar("2023-01-04", "100", splits="1.25"), bar("2023-01-05", "100"),
    ]), start="2023-01-04")
    assert result["rebase"] and not result["published"]
    with session() as s:
        pending = s.scalar(select(PriceDataset).where(PriceDataset.status == "BUILDING"))
        assert pending.manifest["verified_splits"] == {"2023-01-04": "1.25"}
        assert pending.manifest["split_changes"] == [
            {"date": "2023-01-04", "before": None, "after": "1.25"},
            {"date": "2023-01-05", "before": "1.25", "after": None},
        ]
    commit_prices(security_id, response([bar("2023-01-03", "100")]), end="2023-01-03")
    assert published(security_id).id != old.id
    assert published(security_id).manifest["verified_splits"] == {"2023-01-04": "1.25"}


def test_cancellation_of_pending_split_discards_already_observed_old_epoch(security_id):
    old = split_history(security_id)
    commit_prices(security_id, response([bar("2023-01-05", "90", splits="2")]), start="2023-01-05")
    with session() as s:
        pending_id = s.scalar(select(PriceDataset.id).where(PriceDataset.status == "BUILDING"))
    commit_prices(security_id, response([bar("2023-01-05", "100")]), start="2023-01-05")
    with session() as s:
        assert s.get(PriceDataset, pending_id).status == "SUPERSEDED"
        pending = s.scalar(select(PriceDataset).where(PriceDataset.status == "BUILDING"))
        assert pending.id != pending_id and pending.manifest["verified_splits"] == {}
    assert published(security_id).id == old.id
    commit_prices(security_id, response([bar("2023-01-03", "100"), bar("2023-01-04", "100")]), end="2023-01-04")
    with session() as s:
        bars, _ = price_bars(s, security_id)
        assert [Decimal(row["close"]) for row in bars] == [100, 100, 100]


@pytest.mark.parametrize("metadata", [{}, {"symbol": None, "currency": None},
                                          {"symbol": " synth ", "currency": "USD", "instrumentType": "equity"}])
def test_missing_identity_and_canonical_case_are_compatible(security_id, metadata):
    payload = response([bar("2023-01-03"), bar("2023-01-04"), bar("2023-01-05")])
    payload["metadata"] = metadata
    result, _ = commit_prices(security_id, payload)
    assert result["published"] and result["eligible"]


def test_provider_alias_requires_same_security_mapping(security_id):
    from datetime import date

    from iirp.business_models import SecurityIdentifier

    with session() as s, s.begin():
        s.add(SecurityIdentifier(security_id=security_id, provider="synthetic", symbol="SYNTH-A", valid_from=date(2020, 1, 1)))
    payload = response([bar("2023-01-03"), bar("2023-01-04"), bar("2023-01-05")])
    payload["metadata"] = {"symbol": "SYNTH-A", "currency": "USD", "instrumentType": "EQUITY"}
    payload["symbol"] = "SYNTH"
    result, _ = commit_prices(security_id, payload)
    assert result["published"] and result["eligible"]
    payload["symbol"] = "WRONG"
    result, _ = commit_prices(security_id, payload)
    assert not result["published"] and not result["eligible"]


def test_action_only_revision_still_creates_a_new_manifest(security_id):
    commit_prices(security_id, response([bar("2023-01-03", splits="2")]), end="2023-01-03")
    old = published(security_id)
    result, _ = commit_prices(security_id, response([bar("2023-01-03")]), end="2023-01-03")
    assert result["published"]
    assert published(security_id).id != old.id
    assert published(security_id).manifest["verified_splits"] == {}


def test_split_cancellation_invalidates_dependencies_and_keeps_saved_exports(security_id):
    from datetime import date

    from iirp.analytics.research import compute_research
    from iirp.business_models import AnalysisResult
    from iirp.business_worker import prepare_target
    from iirp.lifecycle import export_analysis
    from iirp.research_dependencies import dataset_dependency
    from test_market_lifecycle import publish_research, research_lease

    old = split_history(security_id)
    request, lease = research_lease()
    target = prepare_target(lease)
    calculated = compute_research(target["params"], target["bars"], today=date(2024, 2, 1))
    assert publish_research(lease, calculated)
    with session() as s:
        result_id = s.scalar(select(AnalysisResult.id).where(AnalysisResult.analysis_id == request["id"]))
        before = dataset_dependency(s, s.get(PriceDataset, old.id), [(date(2023, 1, 5), date(2023, 1, 5))])
    frozen_csv = export_analysis(request["id"], result_id)
    frozen_json = export_analysis(request["id"], result_id, "json")
    result, _ = commit_prices(security_id, response([
        bar("2023-01-03", "100"), bar("2023-01-04", "100"), bar("2023-01-05", "100"),
    ]), batch_id=request["batch_id"])
    assert result["published"] and result["rebase"]
    with session() as s:
        # Jan 5's number is unchanged, but the cancelled action changes basis
        # dependencies even for research whose numeric window is unchanged.
        after = dataset_dependency(s, s.get(PriceDataset, published(security_id).id), [(date(2023, 1, 5), date(2023, 1, 5))])
    assert before["fingerprint"] != after["fingerprint"]
    assert export_analysis(request["id"], result_id) == frozen_csv
    assert export_analysis(request["id"], result_id, "json") == frozen_json


def test_currency_unit_case_is_not_a_legal_alias(security_id):
    from iirp.business_models import Security

    with session() as s, s.begin():
        s.get(Security, security_id).currency = "GBP"
    payload = response([bar("2023-01-03"), bar("2023-01-04"), bar("2023-01-05")])
    payload["metadata"] = {"currency": "GBp"}
    result, _ = commit_prices(security_id, payload)
    assert not result["published"] and not result["eligible"]
    assert "冲突" in result["reason"]
