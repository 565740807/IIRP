"""Coalesce queued computation and reuse results of identical inputs within a cache."""


from sqlalchemy import select

from iirp.business_models import AnalysisResult, RequestScope
from iirp.models import now
from iirp.price_cache import coverage_for


def semantic_params(params):
    # price_range records when the request was made; mathematical bounds are
    # already frozen in cutoff/current-year/selected-year/window parameters.
    return {k: v for k, v in params.items() if k != "price_range"}


def effective_input_params(request, security):
    return semantic_params(request.params)


def owned_result_data(data, params):
    """Rebind only per-request envelopes; immutable numerical arrays are shared."""
    return {**data, "metadata": {**data.get("metadata", {}),
        "params": {**data.get("metadata", {}).get("params", {}), **params}}}


def frozen_coverage(s, request, security, dataset):
    scope = s.scalar(
        select(RequestScope).where(
            RequestScope.batch_id == request.batch_id, RequestScope.security_id == security.id
        )
    )
    if not scope:
        return None
    coverage = coverage_for(s, security, scope.start_date, scope.end_date, cache_id=dataset.id)
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


def reuse_result(s, request, security, dataset, benchmark, input_key):
    from iirp.analytics.research import CALCULATION_VERSION
    from iirp.maintenance import lock_analysis_references
    from iirp.result_reuse import result_identity
    exists = result_identity(s, request.id, security.id, input_key)
    if exists:
        return exists
    candidates = s.scalars(
        select(AnalysisResult.id).where(
            AnalysisResult.security_id == security.id,
            AnalysisResult.input_key == input_key,
            AnalysisResult.inputs["calculation_version"].astext == CALCULATION_VERSION,
            AnalysisResult.expires_at > now(),
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
            expires_at=cached.expires_at,
        )
        s.add(result)
        s.flush()
        return result
    return None


def coalesce_compute(s, scope, target):
    from iirp.shared_compute import enqueue
    return enqueue(s, scope, "research_compute", target)


def safe_publication(s, request, security, target, latest):
    """Publish only a result computed from the current cache and benchmark."""
    from iirp.benchmarks import benchmark_snapshot
    from iirp.lifecycle import research_input_key

    if security.status != "VERIFIED" or latest is None or latest.id != target["dataset_id"]:
        return False
    benchmark = benchmark_snapshot(request.params, security, s)
    return research_input_key(request, latest, security, benchmark) == target["input_key"]

