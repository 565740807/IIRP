"""Coalesce queued computation and reuse immutable, fully identified inputs."""


from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from iirp.business_models import (
    AnalysisResult,
    DatasetBar,
    MarketBar,
    PriceDataset,
    RequestScope,
)
from iirp.market_data import coverage_for


def semantic_params(params):
    # price_range records when the request was made; mathematical bounds are
    # already frozen in cutoff/current-year/selected-year/window parameters.
    return {k: v for k, v in params.items() if k != "price_range"}


def effective_input_params(request, security):
    params = semantic_params(request.params)
    if params.get("kind") == "earnings" and not params.get("current_fiscal_year"):
        from sqlalchemy.orm import object_session

        from iirp.business_models import Batch
        from iirp.earnings_planner import ET, _years

        batch = object_session(request).get(Batch, request.batch_id)
        params["current_fiscal_year"] = _years(
            params, batch.created_at.astimezone(ET).date(),
            security.metadata_json.get("verified_fiscal_year_end"),
        )[0]
    return params


def owned_result_data(data, params):
    """Rebind only per-request envelopes; immutable numerical arrays are shared."""
    result = {**data, "metadata": {**data.get("metadata", {}),
        "params": {**data.get("metadata", {}).get("params", {}), **params}}}
    if data.get("date_observation"):
        observer = data["date_observation"]
        result["date_observation"] = {**observer, "metadata": {**observer["metadata"],
            "params": {**observer["metadata"].get("params", {}), **params}}}
    return result


def frozen_coverage(s, request, security, dataset):
    scope = s.scalar(
        select(RequestScope).where(
            RequestScope.batch_id == request.batch_id, RequestScope.security_id == security.id
        )
    )
    if not scope:
        return None
    coverage = coverage_for(s, security, scope.start_date, scope.end_date, dataset_id=dataset.id)
    return {
        key: coverage.get(key)
        for key in (
            "start_date",
            "end_date",
            "expected_sessions",
            "valid_sessions",
            "missing_dates",
            "first_valid_date",
            "last_valid_date",
        )
    } | {"complete": coverage["status"] == "COMPLETE"}


def reuse_result(s, request, security, dataset, events, benchmark, input_key):
    from iirp.analytics.research import CALCULATION_VERSION
    from iirp.maintenance import lock_analysis_references
    from iirp.result_reuse import result_identity
    exists = result_identity(s, request.id, security.id, input_key)
    if exists:
        return exists
    from iirp.analytics.event_overlaps import REPRESENTATION_VERSION
    representation_filter = (
        [func.coalesce(
            AnalysisResult.overlap_projection["metadata"]["representation_version"].astext,
            AnalysisResult.legacy_data["date_observation"]["metadata"]["representation_version"].astext,
        ) == REPRESENTATION_VERSION]
        if request.params.get("kind") == "earnings" else []
    )
    candidates = s.scalars(
        select(AnalysisResult.id).where(
            AnalysisResult.security_id == security.id,
            AnalysisResult.input_key == input_key,
            AnalysisResult.inputs["calculation_version"].astext == CALCULATION_VERSION,
            *representation_filter,
        ).order_by(AnalysisResult.created_at.desc()).limit(1)
    ).all()
    for cached_id in candidates:
        lock_analysis_references(s)
        cached = s.get(AnalysisResult, cached_id)
        if cached is None:
            continue
        if cached.data.get("metadata", {}).get("params", {}).get("calendar", "XNYS") != (security.calendar or "XNYS"):
            continue
        data = owned_result_data(cached.data, request.params)
        result = AnalysisResult(
            analysis_id=request.id,
            security_id=security.id,
            input_key=input_key,
            inputs={
                **cached.inputs,
                "params": request.params,
                "reused_from": cached.id,
                "coverage": frozen_coverage(s, request, security, dataset),
            },
            data=data,
        )
        s.add(result)
        s.flush()
        return result
    return None


def coalesce_compute(s, scope, target):
    from iirp.shared_compute import enqueue
    return enqueue(s, scope, "research_compute", target)


def is_safe_extension(s, old, new, *, ranges=None):
    """Later additions may improve N; changed qualified prices must invalidate."""
    if not old or not new or old.security_id != new.security_id or old.basis_key != new.basis_key:
        return False
    if old.id == new.id:
        return True
    if old.manifest.get("verified_splits") != new.manifest.get("verified_splits"):
        return False
    later = aliased(DatasetBar)
    query = (select(DatasetBar.bar_id)
        .join(MarketBar, MarketBar.id == DatasetBar.bar_id)
        .outerjoin(
            later, (later.dataset_id == new.id) & (later.session_date == DatasetBar.session_date)
        )
        .where(
            DatasetBar.dataset_id == old.id,
            MarketBar.status == "VALID",
            (later.bar_id.is_(None)) | (later.bar_id != DatasetBar.bar_id),
        )
        .limit(1))
    if ranges is not None:
        from iirp.research_dependencies import range_predicate
        query = query.where(range_predicate(DatasetBar.session_date, ranges))
    return s.scalar(query) is None


def safe_publication(s, request, security, target, latest, events):
    from iirp.benchmarks import benchmark_snapshot
    from iirp.lifecycle import research_input_key

    if security.status != "VERIFIED":
        return False
    original = s.get(PriceDataset, target["dataset_id"])
    snapshot = target.get("benchmark")
    if (
        not original
        or research_input_key(request, original, events, security, snapshot) != target["input_key"]
    ):
        return False
    from iirp.research_dependencies import research_ranges
    ranges = research_ranges(effective_input_params(request, security), security.calendar or "XNYS", events)
    if not is_safe_extension(s, original, latest, ranges=ranges):
        return False
    current_benchmark = benchmark_snapshot(request.params, security, s)
    if snapshot != current_benchmark:
        if not snapshot or not current_benchmark:
            return False

        if snapshot.get("dataset_id"):
            def without_data(value):
                return {k: v for k, v in value.items() if k not in ("dataset_id", "status")}

            if without_data(snapshot) != without_data(current_benchmark):
                return False
            if not is_safe_extension(
                s, s.get(PriceDataset, snapshot["dataset_id"]),
                s.get(PriceDataset, current_benchmark.get("dataset_id")),
                ranges=ranges,
            ):
                return False
        elif snapshot.get("symbol") != current_benchmark.get("symbol"):
            return False
        # With no benchmark prices the output contains stock-only statistics.
        # Resolving the benchmark identity does not invalidate those values.
    # A later published result for this ticker wins even if this worker finishes
    # afterwards. Publication is fenced and the compute lane is bounded to one.
    newer = s.scalar(
        select(AnalysisResult.id)
        .join(PriceDataset, PriceDataset.id == AnalysisResult.inputs["dataset_id"].astext)
        .where(
            AnalysisResult.analysis_id == request.id,
            AnalysisResult.security_id == security.id,
            PriceDataset.published_at > original.published_at,
        )
        .limit(1)
    )
    if newer:
        return False
    peers = s.execute(select(AnalysisResult.input_key, AnalysisResult.inputs).where(
        AnalysisResult.analysis_id == request.id,
        AnalysisResult.security_id == security.id,
        AnalysisResult.inputs["dataset_id"].astext == original.id,
        AnalysisResult.input_key != target["input_key"],
    )).all()
    for key, inputs in peers:
        peer_benchmark = inputs.get("benchmark") or {}
        peer_id = peer_benchmark.get("dataset_id")
        original_id = (snapshot or {}).get("dataset_id")
        if peer_id and not original_id:
            return False
        if peer_id and original_id and peer_id != original_id:
            old_benchmark = s.get(PriceDataset, original_id)
            peer_dataset = s.get(PriceDataset, peer_id)
            if peer_dataset and old_benchmark and peer_dataset.published_at > old_benchmark.published_at:
                return False
        if key == research_input_key(request, latest, events, security, current_benchmark):
            return False
    return True


def enqueue_frozen_compute(s, scope, kind, target, capacity):
    from iirp.shared_compute import enqueue
    return enqueue(s, scope, kind, target, capacity)
