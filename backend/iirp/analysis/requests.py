"""Analysis requests: create, reuse cached results, read, export and refetch."""

import csv
import io

from sqlalchemy import select

from iirp.db import session
from iirp.jobs.batch_views import batch_view, linked_jobs
from iirp.jobs.batches import ET, _create
from iirp.jobs.planner import _plan_compute
from iirp.messages import NotFoundError, UserError
from iirp.models import (
    ACTIVE,
    AnalysisRequest,
    AnalysisResult,
    Batch,
    RequestReceipt,
    RequestScope,
    Security,
    now,
)


def create_analysis(params, *, retry_generation=None):
    from iirp.analysis.calendar import last_completed_session
    from iirp.analysis.research import _interval_rules, plan_scope

    # A month needs no interval dates; dropping them keeps equal questions equal.
    ignored = {"request_id"} | ({"start_mmdd", "end_mmdd"} if params["kind"] == "monthly" else set())
    submitted = {k: v for k, v in params.items() if k not in ignored}
    with session() as s:
        receipt = s.get(RequestReceipt, params["request_id"])
        if receipt:
            request = s.scalar(
                select(AnalysisRequest).where(AnalysisRequest.batch_id == receipt.batch_id)
            )
            saved_batch = s.get(Batch, receipt.batch_id)
            if not request or saved_batch.params.get("analysis_input", request.params) != submitted:
                raise UserError("analysis.request_id_reused")
            return analysis_view(s, request)
    stamp = now()
    completed = last_completed_session(as_of=stamp)
    as_of_date = stamp.astimezone(ET).date()
    # Conditions are frozen with their cutoff and current year, so a refetch
    # after 24 hours recomputes the same question up to the newest close.
    effective = {**submitted, "cutoff_date": completed.isoformat(),
                 "current_year": _interval_rules(submitted, as_of_date)[3]
                 if params["kind"] == "interval" else params.get("current_year") or as_of_date.year}
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
            raise NotFoundError("analysis.not_found")
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


MARKET_KINDS = {"market_identity", "market_history"}


def ticker_progress(s, batch, done_ids):
    """Each ticker's step, in the order the tickers were entered."""
    output = []
    for scope in s.scalars(select(RequestScope).where(RequestScope.batch_id == batch.id)):
        if scope.security_id in done_ids:
            output.append({"symbol": scope.symbol, "step": "done", "reason": None})
            continue
        active = [job for job in linked_jobs(s, scope.id) if job.status in ACTIVE]
        if any(job.kind == "research_compute" for job in active):
            step = "compute"
        elif any(job.kind in MARKET_KINDS for job in active):
            step = "download"
        elif scope.status in ("FAILED", "PARTIAL", "CANCELLED") or batch.status in ("FAILED", "CANCELLED"):
            step = "failed"
        elif scope.status == "READY":
            step = "compute"
        else:
            step = "queued"
        output.append({"symbol": scope.symbol, "step": step,
                       "reason": scope.wait_reason if step in ("failed", "queued") else None})
    order = batch.params.get("tickers", [])
    return sorted(output, key=lambda item: order.index(item["symbol"]) if item["symbol"] in order else len(order))


def analysis_view(s, request):
    from iirp.analysis.freshness import freshness
    from iirp.analysis.research import period_stats

    # Results live as long as the price caches they used; show the
    # newest unexpired result of each security.
    results = s.execute(
        select(AnalysisResult.id, AnalysisResult.security_id, AnalysisResult.created_at)
        .where(AnalysisResult.analysis_id == request.id,
               AnalysisResult.expires_at.is_(None) | (AnalysisResult.expires_at > now()))
        .order_by(AnalysisResult.created_at.desc(), AnalysisResult.id.desc())
    ).all()
    chosen = {}
    for row in results:
        if row.security_id in chosen:
            continue
        security = s.get(Security, row.security_id)
        result = s.get(AnalysisResult, row.id)
        data = {**result.data, "periods": [
            {**p, "stats": period_stats(p["years"], p["stats"]["target_n"])}
            for p in result.data["periods"]]} if "periods" in result.data else result.data
        chosen[row.security_id] = {
            "symbol": security.symbol,
            "security_id": security.id,
            "result_id": result.id,
            "created_at": result.created_at,
            "data_published_at": result.inputs.get("price_fetched_at"),
            "expires_at": result.expires_at,
            "data": data,
        }
    batch = s.get(Batch, request.batch_id)
    order = request.params.get("tickers", [])
    from iirp.analysis.distributions import comparison
    keys = {p["key"] for item in chosen.values() for p in item["data"].get("periods", [])}
    comparisons = {key: comparison((item["symbol"], p["stats"]["median"])
                                   for item in chosen.values() for p in item["data"].get("periods", [])
                                   if p["key"] == key) for key in keys}
    return {"comparisons": comparisons,
        "id": request.id,
        "batch_id": batch.id,
        "status": batch.status,
        "params": request.params,
        "results": sorted(chosen.values(), key=lambda r: order.index(r["symbol"])
                          if r["symbol"] in order else len(order)),
        "progress": ticker_progress(s, batch, set(chosen)),
        "batch": batch_view(s, batch),
        "freshness": freshness(s, request, list(chosen.values())),
    }


def recent_analyses(kind="monthly", ticker=""):
    if kind not in {"monthly", "interval"}:
        raise UserError("analysis.kind_invalid")
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


def get_analysis(analysis_id):
    with session() as s:
        request = s.get(AnalysisRequest, analysis_id)
        if not request:
            raise NotFoundError("analysis.not_found")
        return analysis_view(s, request)


STATS_COLUMNS = ("target_n", "n", "median", "mean", "q25", "q75", "best", "best_year", "worst",
                 "worst_year", "up", "flat", "up_low", "up_high", "coin_flip", "paired_n", "beat",
                 "median_excess", "mean_excess", "benchmark_median")
DETAIL_COLUMNS = ("year", "current", "status", "period_start", "period_end", "start_date", "end_date",
                  "sessions", "expected_sessions", "open", "high", "low", "close", "change",
                  "high_change", "low_change",
                  "benchmark_open", "benchmark_close", "benchmark_change", "excess", "missing_dates")


def export_analysis(analysis_id, table="detail"):
    """The shown results as CSV: per-period statistics, or every year's candle.

    Prices are deleted after 24 hours; an export is how a result is kept.
    Ratios are decimals (0.0123 = +1.23%).
    """
    if table not in {"stats", "detail"}:
        raise UserError("analysis.export_format_invalid")
    with session() as s:
        request = s.get(AnalysisRequest, analysis_id)
        if not request:
            raise NotFoundError("analysis.not_found")
        view = analysis_view(s, request)
    if not view["results"]:
        raise UserError("analysis.result_missing")
    benchmark = request.params.get("benchmark") or ""
    stream = io.StringIO()
    writer = csv.writer(stream)
    columns = STATS_COLUMNS if table == "stats" else DETAIL_COLUMNS
    writer.writerow(["ticker", "kind", "period", "start_mmdd", "end_mmdd", "benchmark", *columns])
    for item in view["results"]:
        data = item["data"]
        for period in data["periods"]:
            head = [item["symbol"], data["kind"], period["key"], period["start_mmdd"],
                    period["end_mmdd"], benchmark]
            rows = [period["stats"]] if table == "stats" else period["years"]
            for row in rows:
                writer.writerow([*head, *(" ".join(row[c]) if c == "missing_dates" else row.get(c)
                                          for c in columns)])
    return "﻿" + stream.getvalue()


def refresh_analysis(analysis_id, force=False):
    from iirp.analysis.freshness import refresh_analysis as refresh
    return refresh(analysis_id, force=force)
