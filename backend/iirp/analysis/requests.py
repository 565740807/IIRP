"""Analysis requests: create, reuse cached results, read, export and refetch."""

import csv
import io
import json
from datetime import timedelta

from sqlalchemy import select

from iirp.db import session
from iirp.jobs.batch_views import batch_view
from iirp.jobs.batches import ET, _create
from iirp.jobs.planner import _plan_compute
from iirp.models import (
    AnalysisRequest,
    AnalysisResult,
    Batch,
    ExportManifest,
    RequestReceipt,
    RequestScope,
    Security,
    now,
)


def create_analysis(params, *, retry_generation=None):
    from iirp.analysis.calendar import last_completed_session
    from iirp.analysis.research import _interval_rules, plan_scope

    submitted = {k: v for k, v in params.items() if k not in {"request_id", "research_label"}}

    def comparable(value):
        return value

    with session() as s:
        receipt = s.get(RequestReceipt, params["request_id"])
        if receipt:
            request = s.scalar(
                select(AnalysisRequest).where(AnalysisRequest.batch_id == receipt.batch_id)
            )
            saved_batch = s.get(Batch, receipt.batch_id)
            if not request or comparable(
                saved_batch.params.get("analysis_input", request.params)
            ) != comparable(submitted):
                raise ValueError("同一分析请求标识不能改变参数")
            return analysis_view(s, request)
    stamp = now()
    completed = last_completed_session(as_of=stamp)
    effective = {**submitted, "cutoff_date": completed.isoformat()}
    as_of_date = stamp.astimezone(ET).date()
    effective["current_year"] = (
        _interval_rules({k: v for k, v in params.items() if v is not None}, as_of_date)[3]
        if params["kind"] == "interval"
        else params.get("current_year") or as_of_date.year
    )
    start, end = plan_scope(effective, today=completed)
    collection = {
        "request_id": params["request_id"],
        "kind": "market_history",
        "tickers": params["tickers"],
        "historical_years": params["historical_years"],
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "purpose": params["kind"],
        "analysis_params": effective,
        "analysis_input": submitted,
        "intent": "fetch",
    }
    if retry_generation:
        collection["retry_generation"] = retry_generation
    from iirp.analysis.history_range import price_range
    collection["price_range"] = price_range(collection, stamp, collection=(start, end))
    effective["price_range"] = collection["price_range"]
    with session() as s, s.begin():
        batch, _ = _create(s, collection)
        if params.get("research_label"):
            batch.title = str(params["research_label"]).strip()[:100] + " · " + "、".join(params["tickers"])
        request = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == batch.id))
        if request is None:
            request = AnalysisRequest(batch_id=batch.id, params=effective)
            s.add(request)
            s.flush()
        identifier = request.id
    # Commit the durable command before cache lookup or response reads. Creation
    # locks protect request/scope/security identity, not result decoding or full
    # coverage serialization for this request's HTTP response.
    _reuse_analysis_cache(identifier)
    return get_analysis(identifier)


def _reuse_analysis_cache(identifier):
    """Optional immediate reuse, fenced like the ordinary durable planner."""
    with session() as s, s.begin():
        request = s.get(AnalysisRequest, identifier)
        if request is None:
            raise LookupError("分析不存在")
        batch = s.scalar(
            select(Batch).where(Batch.id == request.batch_id)
            .with_for_update(skip_locked=True, key_share=True)
        )
        if (batch is None or batch.requested_action
                or batch.status not in ("QUEUED", "RUNNING", "RETRY_WAIT", "PARTIAL")):
            return
        # A planner or control holder can win after command commit. Skip its
        # reservation; the command's durable planning signal remains available.
        scopes = s.scalars(
            select(RequestScope).where(RequestScope.batch_id == batch.id)
            .order_by(RequestScope.id).with_for_update(skip_locked=True)
        ).all()
        for scope in scopes:
            security = s.get(Security, scope.security_id)
            if security and security.status == "VERIFIED":
                _plan_compute(s, scope, batch, security, cache_only=True)


def analysis_view(s, request, result_ids=""):
    from iirp.analysis.freshness import freshness

    # Results live as long as the price caches they used (D14).
    results = s.execute(
        select(AnalysisResult.id, AnalysisResult.security_id, AnalysisResult.input_key,
               AnalysisResult.created_at, AnalysisResult.expires_at)
        .where(AnalysisResult.analysis_id == request.id,
               AnalysisResult.expires_at.is_(None) | (AnalysisResult.expires_at > now()))
        .order_by(AnalysisResult.created_at.desc())
    ).all()
    versions = [
        {"id": r.id, "security_id": r.security_id, "created_at": r.created_at.isoformat()}
        for r in results
    ]
    if result_ids:
        requested = set(result_ids.split(","))
        if not requested.issubset({r.id for r in results}):
            raise LookupError("所选结果已过期或不属于这项研究；请重新获取")
        results = [r for r in results if r.id in requested]
        if len({r.security_id for r in results}) != len(results):
            raise ValueError("每只证券请选择一个结果")
    chosen = {}
    for security_id in dict.fromkeys(r.security_id for r in results):
        security = s.get(Security, security_id)
        # Every unexpired result is current data; show the newest one.
        best = max((r for r in results if r.security_id == security_id),
                   key=lambda r: (r.created_at, r.id))
        result = s.get(AnalysisResult, best.id)
        chosen[security_id] = {
            "symbol": security.symbol,
            "security_id": security.id,
            "result_id": result.id,
            "input_version": result.input_key,
            "created_at": result.created_at,
            "data_published_at": result.inputs.get("price_fetched_at"),
            "expires_at": result.expires_at,
            "is_current": True,
            "coverage": result.inputs.get("coverage"),
            "coverage_basis": "recorded" if result.inputs.get("coverage") else "unknown",
            "result_cutoff": result.inputs.get("params", request.params).get("cutoff_date"),
            "data": result.data,
        }
    batch = s.get(Batch, request.batch_id)
    return {
        "id": request.id,
        "batch_id": batch.id,
        "status": batch.status,
        "params": request.params,
        "results": sorted(
            chosen.values(),
            key=lambda r: request.params.get("tickers", [r["symbol"]]).index(r["symbol"]),
        ),
        "batch": batch_view(s, batch),
        "result_versions": versions,
        "freshness": freshness(s, request, list(chosen.values())),
    }


def recent_analyses(kind="monthly", ticker=""):
    if kind not in {"monthly", "interval"}:
        raise ValueError("请选择月度或区间研究")
    with session() as s:
        query = (
            select(AnalysisRequest, Batch.status)
            .join(Batch, Batch.id == AnalysisRequest.batch_id)
            .where(AnalysisRequest.params["kind"].astext == kind)
        )
        if ticker:
            query = query.where(
                AnalysisRequest.params["tickers"].contains([ticker.strip().upper()])
            )
        return {
            "data": {},
            "items": [
                {
                    "id": request.id,
                    "params": request.params,
                    "created_at": request.created_at.isoformat(),
                    "status": status,
                }
                for request, status in s.execute(
                    query.order_by(
                        AnalysisRequest.created_at.desc(), AnalysisRequest.id.desc()
                    ).limit(24)
                )
            ],
        }


def get_analysis(analysis_id, result_ids=""):
    with session() as s:
        request = s.get(AnalysisRequest, analysis_id)
        if not request:
            raise LookupError("分析不存在")
        return analysis_view(s, request, result_ids)


def export_analysis(analysis_id, result_ids="", format="csv"):
    with session() as s, s.begin():
        from iirp.storage.maintenance import lock_analysis_references

        lock_analysis_references(s)
        request = s.get(AnalysisRequest, analysis_id)
        if not request:
            raise LookupError("分析不存在")
        selected_ids = (
            result_ids.split(",")
            if result_ids
            else [x["result_id"] for x in analysis_view(s, request)["results"]]
        )
        results = s.scalars(
            select(AnalysisResult).where(
                AnalysisResult.analysis_id == analysis_id, AnalysisResult.id.in_(selected_ids)
            )
        ).all()
        if not results or len(results) != len(set(selected_ids)):
            raise ValueError("所选结果已不存在或尚未生成")
        s.add(
            ExportManifest(
                result_ids=selected_ids, params=request.params, expires_at=now() + timedelta(days=7)
            )
        )
        results.sort(key=lambda result: (s.get(Security, result.security_id).symbol, result.id))
        if format == "json":
            frozen = [{
                "symbol": s.get(Security, result.security_id).symbol,
                "security_id": result.security_id, "result_id": result.id,
                "input_version": result.input_key,
                "created_at": result.created_at.isoformat(),
                "params": result.inputs.get("params", request.params),
                "inputs": result.inputs, "data": result.data,
            } for result in results]
            return json.dumps({
                "schema": "iirp.analysis-snapshot.v2", "id": request.id,
                "result_ids": [item["result_id"] for item in frozen],
                "results": frozen,
            }, ensure_ascii=False, indent=2)
        if format != "csv":
            raise ValueError("导出格式须为 csv 或 json")
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(
            [
                "ticker",
                "result_id",
                "input_version",
                "record_type",
                "parameters",
                "input_manifest",
                "data",
            ]
        )
        for result in results:
            security = s.get(Security, result.security_id)
            for record_type, records in (
                ("metadata", [result.data.get("metadata", {})]),
                ("summary", [result.data.get("summary", {})]),
                ("distribution", result.data.get("distributions", [])),
                ("monthly_ranking", result.data.get("monthly_rankings", [])),
                ("benchmark", [result.data["benchmark"]] if result.data.get("benchmark") else []),
                ("row", result.data.get("rows", [])),
                ("cell", result.data.get("cells", [])),
                ("series", result.data.get("series", [])),
            ):
                for row in records:
                    writer.writerow(
                        [
                            security.symbol,
                            result.id,
                            result.input_key,
                            record_type,
                            json.dumps(result.inputs.get("params", request.params), ensure_ascii=False),
                            json.dumps(result.inputs, ensure_ascii=False),
                            json.dumps(row, ensure_ascii=False),
                        ]
                    )
        return "\ufeff" + stream.getvalue()


def refresh_analysis(analysis_id, force=False):
    from iirp.analysis.freshness import refresh_analysis as refresh
    return refresh(analysis_id, force=force)
