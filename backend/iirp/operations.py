"""Disposable provider/compute process. Only coordination budgets may write here."""

import hashlib
import json
import os
import sys
import threading
import time
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
from sqlalchemy.dialects.postgresql import insert

from iirp.config import settings
from iirp.db import session
from iirp.market_data import fetch_market, plain
from iirp.models import SourceBudget, now


class ProviderFailure(Exception):
    def __init__(self, message, retry_seconds=900, status_code=None, source_wait=False):
        super().__init__(message)
        self.retry_seconds = retry_seconds
        self.status_code = status_code
        self.source_wait = source_wait


def reserve_sec():
    while True:
        with session() as s, s.begin():
            s.execute(
                insert(SourceBudget)
                .values(provider="sec", next_allowed_at=now(), failures=0)
                .on_conflict_do_nothing()
            )
            budget = s.get(SourceBudget, "sec", with_for_update=True)
            wait = (budget.next_allowed_at - now()).total_seconds()
            if wait <= 0:
                budget.next_allowed_at = now() + timedelta(seconds=0.5)
                return
            if wait > 2:
                raise ProviderFailure("SEC 共享来源冷却中", wait, source_wait=True)
        time.sleep(max(0.01, wait))


def fetch_sec(url):
    from iirp.sec_sources import validate_sec_url

    url = validate_sec_url(url)
    from iirp.providers import sec_configured

    if not sec_configured():
        raise ProviderFailure("SEC 联系信息未配置", 900)
    reserve_sec()
    with httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False) as client:
        with client.stream(
            "GET",
            url,
            headers={"User-Agent": settings().sec_user_agent, "Accept-Encoding": "gzip, deflate"},
        ) as response:
            if response.status_code in (403, 429):
                value = response.headers.get("Retry-After", "")
                try:
                    retry = (
                        float(value)
                        if value.isdigit()
                        else max(0, (parsedate_to_datetime(value) - now()).total_seconds())
                    )
                except (TypeError, ValueError):
                    retry = 900
                retry = max(60, retry)
                with session() as s, s.begin():
                    budget = s.get(SourceBudget, "sec", with_for_update=True)
                    budget.next_allowed_at = max(
                        budget.next_allowed_at, now() + timedelta(seconds=retry)
                    )
                    budget.failures += 1
                raise ProviderFailure(
                    f"SEC HTTP {response.status_code}，共享冷却", retry, response.status_code, source_wait=True
                )
            response.raise_for_status()
            chunks = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 64 * 1024**2:
                    raise ProviderFailure("SEC 单文件超过 64 MiB 操作预算，需拆分清单", 900)
                chunks.append(chunk)
    return b"".join(chunks)


def operation(kind, target):
    if kind in ("market_identity", "market_history", "market_quote"):
        return fetch_market(kind, target)
    if kind in ("sec_discover", "sec_document", "sec_identity"):
        from iirp.sec_sources import run_sec_operation

        return run_sec_operation(kind, target, fetch=fetch_sec)
    if kind == "earnings_candidates":
        from iirp.earnings_data import fetch_candidates

        return fetch_candidates(target)
    if kind == "earnings_evidence":
        from iirp.earnings_data import fetch_evidence, reparse_evidence

        cached_hash = target.get("cached_source_hash")
        if cached_hash:
            if len(cached_hash) != 64 or any(
                char not in "0123456789abcdef" for char in cached_hash
            ):
                raise ValueError("已保存来源标识无效")
            path = settings().runtime_dir / "objects" / cached_hash[:2] / cached_hash
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != cached_hash:
                raise ValueError("已保存来源哈希不一致，不能重解析")
            from iirp.storage import hydrate_response_evidence
            return reparse_evidence(target, hydrate_response_evidence(payload, target.get("cached_observation")))

        return fetch_evidence(target, fetch_sec)
    if kind == "event_compute":
        from iirp.analytics.event_dates import analyze_event_dates

        return analyze_event_dates(
            target["events"], target["bars"],
            observed_event_ids=set(target["observed_event_ids"]),
            cutoff=date.fromisoformat(target["cutoff"]),
            current_year=target["current_year"],
            current_fiscal_year=target["current_fiscal_year"],
            calendar=target["calendar"], benchmark=target["benchmark"],
            metadata=target["metadata"],
        )
    if kind == "research_compute":
        from iirp.analytics.research import compute_research

        cutoff = target["params"].get("cutoff_date")
        result = compute_research(
            target["params"],
            target["bars"],
            events=target.get("events"),
            today=date.fromisoformat(cutoff) if cutoff else None,
            benchmark=target.get("benchmark"),
        )
        result["metadata"]["dependencies"] = target.get("dependency_manifest")
        return result
    raise ValueError("未知工作类型")


def run_request(request):
    started = time.perf_counter()
    try:
        result = operation(request["kind"], request["target"])
        output = {"ok": True, "data": plain(result)}
    except Exception as exc:
        output = {
            "ok": False,
            "error": str(exc) if isinstance(exc, (ProviderFailure, ValueError))
            else f"来源操作失败（{type(exc).__name__}）",
            "retry_seconds": getattr(exc, "retry_seconds", 60),
            "error_type": type(exc).__name__,
            "status_code": getattr(exc, "status_code", None),
            "source_wait": getattr(exc, "source_wait", False),
        }
    output["operation_seconds"] = time.perf_counter() - started
    return output


def main():
    # The child never publishes facts. If its supervising worker disappears,
    # terminate even when a provider library has blocked a Python thread.
    parent = os.getppid()
    def parent_watch():
        while True:
            time.sleep(1)
            if os.getppid() != parent:
                os._exit(125)
    threading.Thread(target=parent_watch, daemon=True).start()
    if "--serve" in sys.argv:
        for line in sys.stdin.buffer:
            command = json.loads(line)
            watchdog = threading.Timer(120, lambda: os._exit(124))
            watchdog.daemon = True
            watchdog.start()
            try:
                source, dest = Path(command["input"]), Path(command["output"])
                if source.stat().st_size > 16 * 1024**2:
                    raise ValueError("操作输入超过16MiB预算")
                output = run_request(json.loads(source.read_bytes()))
                data = json.dumps(output, ensure_ascii=False, allow_nan=False).encode()
                if len(data) > 80 * 1024**2:
                    data = json.dumps({"ok": False, "error": "来源响应超过80MiB预算",
                                       "error_type": "RequestTooLarge"}).encode()
                temporary = dest.with_suffix(".tmp")
                temporary.write_bytes(data)
                temporary.replace(dest)
                # The warm child must not retain the result tree and encoded
                # response while its parent restores evidence and publishes it.
                # Dense event results otherwise pressure the shared memory
                # budget during JSONB insertion, even after bounded overlaps.
                del output, data
            finally:
                watchdog.cancel()
        return
    watchdog = threading.Timer(120, lambda: os._exit(124))
    watchdog.daemon = True
    watchdog.start()
    request = json.loads(sys.stdin.buffer.read(16 * 1024**2))
    sys.stdout.write(json.dumps(run_request(request), ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
