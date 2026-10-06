"""Disposable provider/compute process. Only coordination budgets may write here."""

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
from iirp.market.yahoo import fetch_market, plain
from iirp.models import SourceBudget, now


class ProviderFailure(Exception):
    def __init__(self, message, retry_seconds=900, status_code=None, source_wait=False):
        super().__init__(message)
        self.retry_seconds = retry_seconds
        self.status_code = status_code
        self.source_wait = source_wait


# Callers claim a slot up to LEAD seconds ahead and never closer than FLOOR, so
# commit latency does not delay the request; one that still misses its slot by
# more than GRACE takes a fresh one.
SEC_SLOT_LEAD = 0.25
SEC_SLOT_FLOOR = 0.1
SEC_SLOT_GRACE = 0.025
SEC_STALE_SLOTS = 20


def sec_request_interval():
    """Seconds between SEC request slots across every worker, lane and process.

    A request starts within SEC_SLOT_GRACE of its slot, so slots are spaced by
    (1 + 2 * grace) / rate: no one-second window can then hold more than
    ``sec_requests_per_second`` request starts, even when one start is late.
    """
    from iirp.jobs.profiles import profiles

    rate = float(profiles()["normal_usage"]["sec_requests_per_second"])
    if not 0 < rate <= 10:
        raise ValueError("sec_requests_per_second 必须在 (0, 10] 内（SEC 上限每秒 10 次）")
    return (1 + 2 * SEC_SLOT_GRACE) / rate


def reserve_sec():
    """Claim the next shared SEC slot (a near-future instant) and return it."""
    interval = timedelta(seconds=sec_request_interval())
    floor = timedelta(seconds=SEC_SLOT_FLOOR)
    while True:
        with session() as s, s.begin():
            s.execute(
                insert(SourceBudget)
                .values(provider="sec", next_allowed_at=now(), failures=0)
                .on_conflict_do_nothing()
            )
            budget = s.get(SourceBudget, "sec", with_for_update=True)
            current = now()
            wait = (budget.next_allowed_at - current).total_seconds()
            if wait <= SEC_SLOT_LEAD:
                slot = max(budget.next_allowed_at, current + floor)
                budget.next_allowed_at = slot + interval
                return slot
            if wait > 2:
                raise ProviderFailure("SEC 共享来源冷却中", wait, source_wait=True)
        time.sleep(max(0.01, wait - SEC_SLOT_LEAD))


def wait_for_sec_slot():
    """Return once a request may start now: at its own slot, never late."""
    for _ in range(SEC_STALE_SLOTS):
        slot = reserve_sec()
        delay = (slot - now()).total_seconds()
        if delay > 0:
            time.sleep(delay)
        # A late start could bunch with the next caller's slot; take a fresh one.
        if (now() - slot).total_seconds() <= SEC_SLOT_GRACE:
            return
    raise ProviderFailure("SEC 请求时隙连续过期，稍后重试", 60, source_wait=True)


def fetch_sec(url):
    from iirp.sec.parse import validate_sec_url

    url = validate_sec_url(url)
    from iirp.jobs.providers import sec_configured

    if not sec_configured():
        raise ProviderFailure("SEC 联系信息未配置", 900)
    with httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False) as client:
        # Build everything first so the request starts right at its reserved slot.
        request = client.build_request(
            "GET",
            url,
            headers={"User-Agent": settings().sec_user_agent, "Accept-Encoding": "gzip, deflate"},
        )
        wait_for_sec_slot()
        response = client.send(request, stream=True)
        try:
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
        finally:
            response.close()
    return b"".join(chunks)


def operation(kind, target):
    if kind in ("market_identity", "market_history", "market_quote"):
        return fetch_market(kind, target)
    if kind in ("sec_discover", "sec_document", "sec_identity"):
        from iirp.sec.fetch import run_sec_operation

        return run_sec_operation(kind, target, fetch=fetch_sec)
    if kind == "research_compute":
        from iirp.analysis.research import compute_research

        cutoff = target["params"].get("cutoff_date")
        result = compute_research(
            target["params"],
            target["bars"],
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
