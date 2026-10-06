"""Bounded external operations with independent heartbeats and fenced fact commits."""

import base64
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from iirp.business_models import (
    AnalysisRequest,
    BatchJob,
    Filing,
    Issuer,
    MarketQuote,
    RequestScope,
    Security,
    SourceObservation,
)
from iirp.config import ROOT
from iirp.db import session
from iirp.lifecycle import add_job
from iirp.market_data import quote_from_history, resolve_metadata
from iirp.models import SourceBudget, now
from iirp.operation_pool import OperationInterrupted
from iirp.price_cache import persist_prices, price_bars
from iirp.queue import ManualPriorityYield, fenced, should_yield_to_manual
from iirp.storage import discovery_expiry, register_object, response_expiry, save_object


def scope_links(s, job_id):
    return s.scalars(
        select(RequestScope)
        .join(BatchJob, BatchJob.scope_id == RequestScope.id)
        .where(BatchJob.job_id == job_id, BatchJob.active.is_(True))
    ).all()


def prepare_target(job):
    if job.kind == "earnings_evidence" and (job.result or {}).get("source_hash"):
        from iirp.earnings_data import EARNINGS_PARSER_VERSION

        if job.result.get("parser_version") != EARNINGS_PARSER_VERSION:
            with session() as s:
                observation = s.scalar(select(SourceObservation).where(
                    SourceObservation.job_id == getattr(job, "id", None),
                    SourceObservation.source_hash == job.result["source_hash"]
                ).order_by(SourceObservation.observed_at.desc()).limit(1))
                metadata = observation.params.get("observation", {}) if observation else {}
            return {**job.target, "cached_source_hash": job.result["source_hash"], "cached_observation": metadata}
    if job.kind == "event_compute":
        from iirp.event_pipeline import prepare_event_target
        with session() as s:
            return prepare_event_target(s, job.target)
    if job.kind != "research_compute":
        return job.target
    with session() as s:
        request = s.get(AnalysisRequest, job.target["analysis_id"])
        security = s.get(Security, job.target["security_id"])
        events = job.target.get("events", [])
        params = {**job.target.get("params", request.params),
                  "calendar": job.target.get("calendar", security.calendar or "XNYS")}
        # SEC-verified fiscal metadata is distinct from quote-provider estimates.
        fiscal_year_end = job.target.get("fiscal_year_end", security.metadata_json.get("verified_fiscal_year_end"))
        if fiscal_year_end:
            params["fiscal_year_end_mmdd"] = fiscal_year_end
        if "params" not in job.target and request.params["kind"] == "earnings":
            from iirp.research_pipeline import effective_input_params
            params["current_fiscal_year"] = effective_input_params(request, security).get("current_fiscal_year")
        from iirp.research_dependencies import (
            benchmark_dependency,
            dataset_dependency,
            research_ranges,
        )
        ranges = research_ranges(params, security.calendar or "XNYS", events)
        bars, dataset = price_bars(s, security.id, job.target["dataset_id"], ranges=ranges)
        if dataset is None:
            raise ValueError("行情缓存已过期或已更新，等待按新缓存重新计算")
        from iirp.benchmarks import benchmark_data
        return {"params": params, "bars": bars, "events": events, "dataset_id": dataset.id,
                "benchmark": benchmark_data(s, job.target.get("benchmark"), ranges=ranges),
                "dependency_manifest": {"ranges": [[str(a), str(b)] for a, b in ranges],
                    "prices": dataset_dependency(s, dataset, ranges),
                    "benchmark": benchmark_dependency(s, job.target.get("benchmark"), ranges)}}


def _persist(s, current, response, sources, source, observation_metadata=None):
    kind = current.kind
    result = {"message": "已保存来源与业务结果", "source_hash": source["sha256"]}
    status = "SUCCEEDED"
    error = None
    for item in sources.values():
        register_object(s, item)
    s.add(
        SourceObservation(
            job_id=current.id,
            source_hash=source["sha256"],
            provider="sec"
            if kind.startswith("sec") or kind == "earnings_evidence"
            else "yfinance"
            if kind.startswith(("market", "earnings"))
            else "local",
            params={**current.target, "observation": observation_metadata or {}},
        )
    )
    scopes = scope_links(s, current.id)
    source_hashes = {url: item["sha256"] for url, item in sources.items()}
    if kind == "market_identity":
        resolve_metadata(s, s.get(Security, current.target["security_id"]), response)
    elif kind == "market_history":
        result.update(persist_prices(s, current, response))
        if not result["cached"] and not result.get("superseded_by"):
            status, error = "PARTIAL", result.get("reason") or "行情来源未返回可用数据"
    elif kind == "market_quote":
        quote = quote_from_history(current.target["symbol"], response)
        if quote:
            existing = s.get(MarketQuote, current.target["symbol"], with_for_update=True)
            if existing:
                from iirp.quote_publication import merge_quote
                quote, retained = merge_quote(existing.data, quote)
                existing.data, existing.fetched_at = quote, now()
                if retained:
                    status, error = "PARTIAL", quote["refresh_notice"]
                    result["retained_previous_quote"] = True
            else:
                s.add(MarketQuote(symbol=current.target["symbol"], data=quote))
        else:
            status, error = "PARTIAL", "行情来源未返回可用报价，保留最后可用值"
    elif kind == "sec_discover":
        from iirp.sec_facts import persist_discovery

        entries = persist_discovery(s, current, response, source_hashes)
        result["discovered"] = len(entries)
        cursor = response.get("cursor")
        scan = response.get("scan", {})
        for scope in scopes:
            scope.checkpoint = {
                **scope.checkpoint,
                "scan": scan,
                "scan_done": bool(scan.get("complete")),
                "last_scan_job": current.id,
            }
            if cursor and scan.get("reason") not in (
                "index_not_ready",
                "repeated_page",
                "source_gap",
                "source_unavailable",
            ):
                add_job(
                    s, scope, "sec_discover", {**current.target, "cursor": cursor}, current.priority
                )
        if not scan.get("complete") and not cursor:
            status, error = "PARTIAL", f"发现范围未闭合：{scan.get('reason', '需继续核对')}"
        elif scan.get("reason") in (
            "index_not_ready",
            "repeated_page",
            "source_gap",
            "source_unavailable",
        ):
            status, error = "PARTIAL", f"清单缺口：{scan['reason']}"
    elif kind == "sec_document":
        from iirp.sec_facts import persist_document

        if not response.get("filing"):
            status, error = "PARTIAL", str(response.get("scan", {}).get("reason", "申报原文未取得"))
        else:
            persist_document(s, current, response, source_hashes)
            filing = s.get(Filing, current.target["accession"])
            result["accession"] = filing.accession
    elif kind == "sec_identity":
        for entry in response.get("entries", []):
            symbol = entry.get("ticker") or entry.get("symbol")
            cik = entry.get("cik")
            if not symbol or not cik:
                continue
            security = s.scalar(
                select(Security).where(
                    Security.symbol.in_(
                        (symbol, symbol.replace(".", "-"), symbol.replace("-", "."))
                    )
                )
            )
            if not security:
                continue
            cik = str(cik).zfill(10)
            issuer = s.get(Issuer, cik)
            if not issuer:
                s.add(Issuer(id=cik, name=entry.get("name") or entry.get("title") or symbol))
                s.flush()
            if not security.issuer_id or security.issuer_id == cik:
                security.issuer_id = cik
    elif kind == "earnings_candidates":
        from iirp.earnings_data import persist_candidates

        result.update(persist_candidates(s, current, response, source["sha256"]))
        if response.get("reason"):
            status, error = "PARTIAL", response["reason"]
    elif kind == "earnings_evidence":
        from iirp.earnings_data import persist_evidence

        result.update(persist_evidence(s, current, response, source_hashes, source["sha256"]))
        if result.get("reason"):
            status, error = "PARTIAL", result["reason"]
    elif kind in {"event_compute", "research_compute"}:
        from iirp.shared_compute import publish
        result = publish(s, current, response)
    current.result = result
    current.status = status
    current.error = error
    current.progress_done = 1
    current.finished_at = now()
    current.lease_token = current.lease_until = None
    current.checkpoint = {**current.checkpoint, "committed_source": source["sha256"]}


def skip_obsolete_compute(job):
    """Only skip when every fenced subscriber has a cache or changed inputs."""
    skipped = []

    def check(s, current):
        from iirp.shared_compute import pending_subscribers, reuse_pending
        reuse_pending(s, current)
        if next(pending_subscribers(s, current), None) is None:
            current.status, current.finished_at = "SUCCEEDED", now()
            current.progress_done = 1
            current.lease_token = current.lease_until = None
            current.result = {"message": "已复用相同输入结果或输入已修订，等待最新计算",
                              "skipped_before_compute": True}
            skipped.append(True)
    return not fenced(job, business_write=check) or bool(skipped)


def execute_business(job, stopping=lambda: False, runner=None):
    try:
        if stopping():
            return
        if job.kind == "local_import":
            from iirp.imports import persist

            def commit_local(s, current):
                if stopping():
                    raise OperationInterrupted
                current.result = persist(s, current)
                current.status = "PARTIAL" if current.result.get("reason") else "SUCCEEDED"
                current.error = current.result.get("reason")
                current.progress_done = 1
                current.finished_at = now()
                current.lease_token = current.lease_until = None
                s.add(
                    SourceObservation(
                        job_id=current.id,
                        source_hash=current.result["source_hash"],
                        provider="manual_csv",
                        params=current.target,
                    )
                )

            return fenced(job, business_write=commit_local)
        if job.kind.startswith("maintenance_"):
            from iirp.maintenance import execute_maintenance

            return execute_maintenance(job, stopping=stopping)
        if job.kind.startswith(("market_", "earnings_candidates")):
            with session() as s, s.begin():
                s.execute(
                    insert(SourceBudget)
                    .values(provider="yfinance", next_allowed_at=now(), failures=0)
                    .on_conflict_do_nothing()
                )
                budget = s.get(SourceBudget, "yfinance", with_for_update=True)
                delay = (budget.next_allowed_at - now()).total_seconds()
            if delay > 0:
                fenced(job, status="RETRY_WAIT", error="Yahoo 共享冷却中", retry_seconds=delay,
                       business_write=lambda s, current: setattr(current, "attempts", max(0, current.attempts - 1)))
                return
        if job.kind in {"research_compute", "event_compute"} and skip_obsolete_compute(job):
            return
        prepare_started = time.perf_counter()
        target = prepare_target(job)
        prepare_seconds = time.perf_counter() - prepare_started
        operation_started = time.perf_counter()
        if runner:
            def checkpoint():
                if stopping() or not fenced(job, acknowledge_control=False):
                    return False
                if should_yield_to_manual(job):
                    raise ManualPriorityYield
                return True
            try:
                response = runner.run(job.kind, target, checkpoint)
            except OperationInterrupted:
                return
            finally:
                fenced(job)
        else:
            with (
                tempfile.TemporaryFile() as output,
                tempfile.TemporaryFile() as errors,
                tempfile.TemporaryFile() as input_file,
            ):
                input_file.write(
                    json.dumps(
                        {"kind": job.kind, "target": target}, ensure_ascii=False, default=str
                    ).encode()
                )
                input_file.seek(0)
                proc = subprocess.Popen(
                    [sys.executable, "-m", "iirp.operations"],
                    stdin=input_file,
                    stdout=output,
                    stderr=errors,
                    start_new_session=True,
                    env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
                )
                started = time.monotonic()
                try:
                    while proc.poll() is None:
                        if stopping() or not fenced(job, acknowledge_control=False):
                            return
                        if should_yield_to_manual(job):
                            raise ManualPriorityYield
                        if time.monotonic() - started > 110:
                            raise TimeoutError("来源单元超过期限")
                        time.sleep(0.5)
                    if proc.returncode != 0:
                        raise RuntimeError(f"来源子进程退出 {proc.returncode}")
                    output.seek(0)
                    payload = output.read(80 * 1024**2)
                    response = json.loads(payload)
                finally:
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGTERM)
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            os.killpg(proc.pid, signal.SIGKILL)
                            proc.wait(timeout=2)
                    fenced(job)
        if stopping():
            return
        operation_seconds = time.perf_counter() - operation_started
        if not response.get("ok"):
            error = response.get("error", "来源未返回可用结果")
            delay = min(
                3600, max(float(response.get("retry_seconds", 60)), 15 * 2 ** min(job.attempts, 6))
            ) + random.uniform(0, 3)
            if job.kind.startswith(("market_", "earnings_candidates")):
                with session() as s, s.begin():
                    budget = s.get(SourceBudget, "yfinance", with_for_update=True)
                    budget.failures += 1
                    budget.next_allowed_at = max(
                        budget.next_allowed_at, now() + timedelta(seconds=delay)
                    )
            source_wait = bool(response.get("source_wait"))
            fenced(
                job,
                status="FAILED" if job.attempts >= 3 and not source_wait else "RETRY_WAIT",
                error=error,
                retry_seconds=delay,
                business_write=(lambda s, current: setattr(current, "attempts", max(0, current.attempts - 1))) if source_wait else None,
            )
            return
        data = response["data"]
        storage_started = time.perf_counter()
        # Discovery list pages and index files can be fetched again; filing
        # documents and parsed facts are permanent. Responses: response_expiry.
        expiry = {"expires_at": discovery_expiry()} if job.kind == "sec_discover" else {}
        sources = {}
        for document in data.get("source_documents", []):
            content = (
                base64.b64decode(document["payload"])
                if document.get("encoding") == "base64"
                else document["payload"].encode()
            )
            sources[document["url"]] = {**save_object(
                content, document.get("media_type", "application/octet-stream")
            ), **expiry}
        filing = data.get("filing") or {}
        if filing.get("xml_payload"):
            content = (
                base64.b64decode(filing["xml_payload"])
                if filing.get("xml_encoding") == "base64"
                else filing["xml_payload"].encode()
            )
            sources[filing["document_url"]] = save_object(content, "application/xml")
        from iirp.storage import response_evidence
        payload, observation_metadata = response_evidence(data, sources)
        source = {**save_object(payload, "application/json"), "expires_at": response_expiry(job.kind)}
        del payload
        timing = {
            "queue_seconds": max(0, (job.started_at - job.available_at).total_seconds()),
            "prepare_seconds": prepare_seconds,
            "operation_seconds": operation_seconds,
            "child_operation_seconds": response.get("operation_seconds"),
            "adapter_seconds": data.get("timing", {}).get("adapter_seconds"),
            "http_seconds": data.get("timing", {}).get("http_seconds"),
            "http_requests": data.get("timing", {}).get("http_requests"),
            "compute_seconds": response.get("operation_seconds") if job.kind in {"research_compute", "event_compute"} else None,
            "storage_seconds": time.perf_counter() - storage_started,
        }
        def commit_response(s, current):
            if stopping():
                raise OperationInterrupted
            started = time.perf_counter()
            _persist(s, current, data, sources, source, observation_metadata)
            current.result = {**current.result, "timing": {**timing,
                "verification_seconds": time.perf_counter() - started}}
        fenced(job, source=source, business_write=commit_response)
    except OperationInterrupted:
        # A stop detected after row-lock acquisition must roll back the entire
        # fenced transaction, including source references and lease renewal.
        return
    except ManualPriorityYield:
        # The source child has been terminated by finally before releasing its
        # lease. Keep committed checkpoints and all subscriptions for resumption.
        fenced(job, status="QUEUED", error="为同通道手动请求让路，完成后自动继续",
               business_write=lambda s, current: setattr(current, "attempts", max(0, current.attempts - 1)))
    except Exception as exc:
        import logging

        from sqlalchemy.exc import OperationalError

        transient = isinstance(exc, OperationalError)
        logging.exception("business unit failed id=%s type=%s", job.id, type(exc).__name__)
        fenced(
            job,
            status="RETRY_WAIT" if transient else "FAILED",
            retry_seconds=5 if transient else 0,
            error="数据库提交暂时繁忙，5 秒后重试；已保存数据保留" if transient else
                str(exc) if isinstance(exc, ValueError) else
                f"工作单元失败（{type(exc).__name__}）；已保存数据保留，可重试此项",
            business_write=(lambda s, current: setattr(current, "attempts", max(0, current.attempts - 1))) if transient else None,
        )
