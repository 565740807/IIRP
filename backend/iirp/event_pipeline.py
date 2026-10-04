"""Frozen event inputs and publication; numerical rules stay in analytics."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

from sqlalchemy import select

from iirp.analytics.calendar import calendar_version
from iirp.analytics.event_dates import CALCULATION_VERSION, event_price_scope
from iirp.analytics.event_overlaps import REPRESENTATION_VERSION
from iirp.benchmarks import benchmark_data, benchmark_snapshot
from iirp.business_models import AnalysisResult, PriceDataset, Security
from iirp.event_models import EventSetVersion
from iirp.event_service import _candidate, _required, _selected_events, event_input_key
from iirp.market_data import digest, latest_dataset, price_bars


def freeze_input(s, request, scope):
    security = _required(s, Security, scope.security_id)
    if security.status != "VERIFIED" or not security.calendar:
        return None
    revision = _required(s, EventSetVersion, request.params["event_version_id"])
    dataset = latest_dataset(s, security.id)
    if dataset and dataset.basis != "SPLIT_ONLY":
        dataset = None
    benchmark = benchmark_snapshot(request.params, security, s)
    from iirp.research_dependencies import benchmark_dependency, dataset_dependency
    ranges = event_price_scope(_selected_events(revision, request.params),
        date.fromisoformat(request.params["cutoff_date"]), security.calendar)
    return {
        "analysis_id": request.id, "security_id": security.id,
        "input_key": event_input_key(request, dataset, security, benchmark=benchmark, db=s),
        "params": deepcopy(request.params), "identity": _candidate(security),
        "event_snapshot_hash": digest([revision.document, revision.events]),
        "inputs": {
            "dataset_id": dataset.id if dataset else None,
            "event_version_id": revision.id, "event_content_hash": revision.content_hash,
            "params": deepcopy(request.params), "calendar_version": calendar_version(),
            "calculation_version": CALCULATION_VERSION, "benchmark": benchmark,
            "representation_version": REPRESENTATION_VERSION,
        },
        "dependencies": {"prices": dataset_dependency(s, dataset, ranges),
            "benchmark": benchmark_dependency(s, benchmark, ranges),
            "ranges": [[str(a), str(b)] for a, b in ranges]},
    }


def plan_event_compute(s, request, scope, capacity=None, *, cache_only=False):
    from iirp.lifecycle import advisory
    from iirp.maintenance import lock_analysis_references
    from iirp.research_pipeline import enqueue_frozen_compute
    from iirp.result_reuse import clone_frozen, result_identity
    from iirp.shared_compute import lock_input, restart_obsolete

    target = freeze_input(s, request, scope)
    if target is None:
        return None
    advisory(s, ["compute", request.id, scope.security_id])
    key = target["input_key"]
    scope.checkpoint = {**scope.checkpoint, "compute_input_key": key}
    if not lock_input(s, "event_compute", scope.security_id, key):
        scope.status = "QUEUED"
        return None
    old = result_identity(s, request.id, scope.security_id, key)
    if old:
        return old
    def reuse():
        cached = s.scalar(select(AnalysisResult.id).where(AnalysisResult.security_id == scope.security_id,
            AnalysisResult.input_key == key).order_by(AnalysisResult.created_at.desc()).limit(1))
        if cached:
            lock_analysis_references(s)
            return clone_frozen(s, cached, request, scope.security_id, key)
        return None

    cached = reuse()
    if cached:
        return cached
    if not cache_only:
        job = enqueue_frozen_compute(s, scope, "event_compute", target, capacity)
        if job and job.status == "SUCCEEDED":
            cached = reuse()
            if cached:
                return cached
            restart_obsolete(s, scope, "event_compute", target, capacity)
    return None


def compatible_input(s, request, scope, target):
    current = freeze_input(s, request, scope)
    return bool(current and all(current[key] == target[key] for key in
        ("input_key", "params", "identity", "event_snapshot_hash"))
        and current["inputs"]["calendar_version"] == target["inputs"]["calendar_version"]
        and current["inputs"]["calculation_version"] == target["inputs"]["calculation_version"])


def prepare_event_target(s, target):
    # All versions are chosen at planning. Never replace them with latest here.
    request = SimpleNamespace(params=target["params"])
    revision = _required(s, EventSetVersion, target["inputs"]["event_version_id"])
    if digest([revision.document, revision.events]) != target["event_snapshot_hash"]:
        raise ValueError("冻结事件内容与任务摘要不一致")
    security = SimpleNamespace(**target["identity"])
    dataset = _required(s, PriceDataset, target["inputs"]["dataset_id"]) if target["inputs"]["dataset_id"] else None
    benchmark = target["inputs"]["benchmark"]
    # Source existence is independent of observation/acquisition eligibility.
    # Retain original review flags; unverified is not a user exclusion.
    visible = _selected_events(revision, request.params)
    ranges = event_price_scope(visible, date.fromisoformat(request.params["cutoff_date"]), security.calendar)
    bars = price_bars(s, security.id, dataset.id, ranges=ranges)[0] if dataset else []
    return dict(
        # Review/source prose is preserved in the immutable revision, not sent
        # through the bounded numerical channel. Keep source qualification facts
        # (including URL validity for the fiscal calendar) exactly as recorded.
        events=[{
            **{key: value for key, value in event.items() if key not in {"review", "sources", "notes"}},
            "review": {key: value for key, value in event.get("review", {}).items() if key != "note"},
            "sources": [{key: value for key, value in source.items() if key in {"url", "supports", "rejected"}}
                        if isinstance(source, dict) else source for source in event.get("sources", [])],
        } for event in revision.events],
        bars=bars,
        observed_event_ids=[event["client_event_id"] for event in visible],
        cutoff=request.params["cutoff_date"],
        current_year=request.params["current_year"],
        current_fiscal_year=request.params.get("current_fiscal_year"),
        calendar=security.calendar,
        benchmark=benchmark_data(s, benchmark, ranges=ranges),
        metadata={
            "research_kind": "earnings"
            if revision.document["schema_version"] == "iirp.earnings-events.v1"
            else "custom",
            "params": request.params,
            "requested_fiscal_years": request.params.get("years", []),
            "requested_fiscal_quarters": revision.document["scope"].get("fiscal_quarters"),
            "historical_years": request.params.get("years", []),
            "common_years": request.params.get("common_years", False),
            "event_set_id": revision.set_id,
            "event_version_id": revision.id,
            "event_version": revision.version,
            "event_content_hash": revision.content_hash,
            "dataset_id": dataset.id if dataset else None,
            "security_id": security.id,
            "symbol": security.symbol,
            "research_as_of": revision.document["research_as_of"],
            "price_as_of": dataset.published_at.isoformat()
            if dataset and dataset.published_at
            else None,
            "price_basis": "split_only",
            "identity": _candidate(security),
            "keyword_scope_policy": "纳入关键词仅指导候选检索；排除关键词按事件名称在保存时阻止选入，原事实保留。其他语义由逐项人工选择与排除理由决定。",
            "chronology_notice": "事件发生、来源发表、首次系统观察与人工核验是不同时间；未知信息不由更新时间反推。",
        },
    )


def restore_event_text(data, revision, params):
    """Reattach frozen evidence only; never recalculate eligibility or prices."""
    events = {event["client_event_id"]: event for event in revision.events}
    rows = [*data["rows"], *data["coverage_rows"]]
    if len(rows) != len(events) or {row["key"] for row in rows} != set(events):
        raise ValueError("计算结果事件与冻结版本不一致")
    for row in rows:
        event = events[row["key"]]
        row["sources"] = event.get("sources", [])
        row["review_note"] = event.get("review", {}).get("note")
    for quarter in data.get("fiscal_coverage", {}).get("quarter_calendar", []):
        for entry in quarter["entries"]:
            if entry["key"] is None:  # Missing fiscal period, not an event.
                continue
            event = events[entry["key"]]
            # Preserve the calendar's existing display subset of valid sources.
            entry["sources"] = [source for source in event.get("sources", [])
                                if isinstance(source, dict) and not source.get("rejected")
                                and str(source.get("url", "")).startswith(("https://", "http://"))]
            entry["review_note"] = event.get("review", {}).get("note")
    visible = {event["client_event_id"] for event in _selected_events(revision, params)}
    data["metadata"].update(
        coverage=revision.document["coverage"],
        inclusion_rule=revision.document["scope"],
        excluded_events=[{
            "client_event_id": event["client_event_id"],
            "event_name": event["event_name"],
            "date_verified": event["date_verified"],
            "user_excluded": event["excluded"],
            "note": event["review"]["note"],
        } for event in revision.events if event["client_event_id"] not in visible],
    )
    return data


def publish_event_result(s, request, scope, target, data):
    """Only called by the worker inside the existing lease/control publication fence."""
    from iirp.maintenance import lock_analysis_references
    lock_analysis_references(s)
    if not compatible_input(s, request, scope, target):
        return None
    existing = s.scalar(select(AnalysisResult).where(AnalysisResult.analysis_id == request.id,
        AnalysisResult.security_id == scope.security_id,
        AnalysisResult.input_key == target["input_key"]))
    if existing:
        return existing
    # compatible_input checked the full revision hash inside this publication
    # fence. Fetch that exact task-owned version, never EventSet's latest one.
    revision = _required(s, EventSetVersion, target["inputs"]["event_version_id"])
    from iirp.event_overlap_reads import EventOverlapSummary
    if data.get("metadata", {}).get("representation_version") != REPRESENTATION_VERSION:
        raise ValueError("计算结果重叠表示版本不兼容")
    for item in [*data["rows"], *data["coverage_rows"]]:
        if "overlapping_event_ids" in item:
            raise ValueError("新计算结果不应包含完整重叠列表")
        EventOverlapSummary.model_validate(item.get("overlap"))
    data = restore_event_text(data, revision, target["params"])
    row = AnalysisResult(analysis_id=request.id, security_id=scope.security_id,
        input_key=target["input_key"], inputs=target["inputs"], data=data)
    s.add(row)
    s.flush()
    return row
