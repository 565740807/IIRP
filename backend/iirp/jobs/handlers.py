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

from iirp.config import ROOT
from iirp.db import session
from iirp.jobs.batches import add_job
from iirp.jobs.operation_pool import OperationInterrupted
from iirp.jobs.queue import ManualPriorityYield, fenced, should_yield_to_manual
from iirp.market.cache import persist_prices, price_bars
from iirp.market.yahoo import quote_from_history, resolve_metadata
from iirp.messages import UserError, msg
from iirp.models import (
    AnalysisRequest,
    BatchJob,
    Filing,
    Issuer,
    MarketQuote,
    RequestScope,
    Security,
    SourceBudget,
    SourceObservation,
    now,
)
from iirp.storage.objects import discovery_expiry, register_object, response_expiry, save_object


def scope_links(s, job_id):
    return s.scalars(
        select(RequestScope)
        .join(BatchJob, BatchJob.scope_id == RequestScope.id)
        .where(BatchJob.job_id == job_id, BatchJob.active.is_(True))
    ).all()


def prepare_target(job):
    if job.kind != "research_compute":
        return job.target
    with session() as s:
        request = s.get(AnalysisRequest, job.target["analysis_id"])
        security = s.get(Security, job.target["security_id"])
        params = {**job.target.get("params", request.params),
                  "calendar": job.target.get("calendar", security.calendar or "XNYS")}
        from iirp.analysis.dependencies import (
            benchmark_dependency,
            dataset_dependency,
            research_ranges,
        )
        ranges = research_ranges(params, security.calendar or "XNYS")
        bars, dataset = price_bars(s, security.id, job.target["dataset_id"], ranges=ranges)
        if dataset is None:
            raise UserError("job.cache_replaced")
        from iirp.analysis.benchmarks import benchmark_data
        return {"params": params, "bars": bars, "dataset_id": dataset.id,
                "benchmark": benchmark_data(s, job.target.get("benchmark"), ranges=ranges),
                "dependency_manifest": {"ranges": [[str(a), str(b)] for a, b in ranges],
                    "prices": dataset_dependency(s, dataset, ranges),
                    "benchmark": benchmark_dependency(s, job.target.get("benchmark"), ranges)}}


def _persist(s, current, response, sources, source, observation_metadata=None):
    kind = current.kind
    result = {"message": msg("job.saved"), "source_hash": source["sha256"]}
    status = "SUCCEEDED"
    error = None
    for item in sources.values():
        register_object(s, item)
    s.add(
        SourceObservation(
            job_id=current.id,
            source_hash=source["sha256"],
            provider="sec"
            if kind.startswith("sec")
            else "yfinance"
            if kind.startswith("market")
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
            status, error = "PARTIAL", result.get("reason") or msg("market.no_data")
    elif kind == "market_quote":
        quote = quote_from_history(current.target["symbol"], response)
        if quote and current.target.get("history"):
            from iirp.market.quote_publication import history_snapshot
            quote["history"] = history_snapshot(response)
        if quote:
            existing = s.get(MarketQuote, current.target["symbol"], with_for_update=True)
            if existing:
                from iirp.market.quote_publication import merge_quote
                quote, retained = merge_quote(existing.data, quote)
                existing.data, existing.fetched_at = quote, now()
                if retained:
                    status, error = "PARTIAL", quote["refresh_notice"]
                    result["retained_previous_quote"] = True
            else:
                s.add(MarketQuote(symbol=current.target["symbol"], data=quote))
        else:
            status, error = "PARTIAL", msg("market.no_quote")
    elif kind == "market_stock_quotes":
        from iirp.market.stock_quotes import persist_stock_quotes
        result.update(persist_stock_quotes(s, current, response))
    elif kind == "index_constituents":
        from iirp.market.constituents import persist_failure, persist_success
        if response.get("error"):
            persist_failure(s, response["index_name"], response["error"])
            status, error = "PARTIAL", response["error"]
        else:
            persist_success(s, response["index_name"], response["rows"])
            result["constituents"] = len(response["rows"])
    elif kind == "sec_discover":
        from iirp.insider.facts import persist_discovery

        entries = persist_discovery(s, current, response, source_hashes)
        result["discovered"] = len(entries)
        if response.get("entity"):
            result["entity"] = response["entity"]
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
            status, error = "PARTIAL", msg("sec.discovery_open",
                                             reason=scan.get("reason") or msg("sec.needs_more_checks"))
        elif scan.get("reason") in (
            "index_not_ready",
            "repeated_page",
            "source_gap",
            "source_unavailable",
        ):
            status, error = "PARTIAL", msg("sec.index_gap", reason=scan["reason"])
    elif kind == "sec_document":
        from iirp.insider.facts import persist_document

        if not response.get("filing"):
            status, error = "PARTIAL", str(response.get("scan", {}).get("reason")
                                           or msg("sec.document_missing"))
        else:
            persist_document(s, current, response, source_hashes)
            filing = s.get(Filing, current.target["accession"])
            result["accession"] = filing.accession
    elif kind == "sec_identity":
        if "candidates" in response and current.target.get("query"):
            result["candidates"] = response["candidates"][:20]
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
    elif kind == "research_compute":
        from iirp.analysis.shared_compute import publish
        result = publish(s, current, response)
    current.result = result
    current.status = status
    current.error = error
    current.progress_done = 1
    current.finished_at = now()
    current.lease_token = current.lease_until = None
    current.checkpoint = {**current.checkpoint, "committed_source": source["sha256"]}


# Errors that are the source's answer rather than a fault: Yahoo has no such ticker.
DEFINITE_ANSWERS = {"market_identity": {"market.identity_fields_missing"}}


def definite_answer(kind, error):
    from iirp.messages import decode

    return (decode(error) or {}).get("code") in DEFINITE_ANSWERS.get(kind, set())


def skip_obsolete_compute(job):
    """Only skip when every fenced subscriber has a cache or changed inputs."""
    skipped = []

    def check(s, current):
        from iirp.analysis.shared_compute import pending_subscribers, reuse_pending
        reuse_pending(s, current)
        if next(pending_subscribers(s, current), None) is None:
            current.status, current.finished_at = "SUCCEEDED", now()
            current.progress_done = 1
            current.lease_token = current.lease_until = None
            current.result = {"message": msg("job.compute_skipped"),
                              "skipped_before_compute": True}
            skipped.append(True)
    return not fenced(job, business_write=check) or bool(skipped)


def execute_business(job, stopping=lambda: False, runner=None):
    try:
        if stopping():
            return
        if job.kind.startswith("maintenance_"):
            from iirp.storage.maintenance import execute_maintenance

            return execute_maintenance(job, stopping=stopping)
        if job.kind.startswith("market_"):
            with session() as s, s.begin():
                s.execute(
                    insert(SourceBudget)
                    .values(provider="yfinance", next_allowed_at=now(), failures=0)
                    .on_conflict_do_nothing()
                )
                budget = s.get(SourceBudget, "yfinance", with_for_update=True)
                delay = (budget.next_allowed_at - now()).total_seconds()
            if delay > 0:
                fenced(job, status="RETRY_WAIT", error=msg("source.yahoo.cooldown"), retry_seconds=delay,
                       business_write=lambda s, current: setattr(current, "attempts", max(0, current.attempts - 1)))
                return
        if job.kind == "research_compute" and skip_obsolete_compute(job):
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
                    [sys.executable, "-m", "iirp.jobs.operations"],
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
                            raise TimeoutError("source unit exceeded its time limit")
                        time.sleep(0.5)
                    if proc.returncode != 0:
                        raise RuntimeError(f"source subprocess exited {proc.returncode}")
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
            error = response.get("error") or msg("source.no_result")
            if definite_answer(job.kind, error):
                # The source answered (no such ticker); nothing to retry or cool down.
                fenced(job, status="FAILED", error=error)
                return
            delay = min(
                3600, max(float(response.get("retry_seconds", 60)), 15 * 2 ** min(job.attempts, 6))
            ) + random.uniform(0, 3)
            if job.kind.startswith("market_"):
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
        expiry = {"expires_at": discovery_expiry()} if job.kind in {"sec_discover", "sec_identity"} else {}
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
        from iirp.storage.objects import response_evidence
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
            "compute_seconds": response.get("operation_seconds") if job.kind == "research_compute" else None,
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
        fenced(job, status="QUEUED", error=msg("job.yield_to_manual"),
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
            error=msg("job.database_busy") if transient else
                str(exc) if isinstance(exc, ValueError) else
                msg("job.unit_failed", error=type(exc).__name__),
            business_write=(lambda s, current: setattr(current, "attempts", max(0, current.attempts - 1))) if transient else None,
        )
