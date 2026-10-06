"""Bounded P0 source probes. These limits are test budgets, not production coverage."""

import json
import os
import re
import sys
import threading
from datetime import datetime, timezone
from importlib.metadata import version

import httpx
from defusedxml import ElementTree

from iirp.config import settings
from iirp.jobs.profiles import development_budget

SEC_URL = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&owner=only&count={development_budget().sec_items_per_page}&start=0&output=atom"

# The placeholder shipped in deploy/.env.example; a test keeps the two in sync.
SEC_TEMPLATE_USER_AGENTS = frozenset({"IIRP contact@example.invalid"})
SEC_USER_AGENT_HINT = (
    "需配置 SEC User-Agent：在 deploy/.env 把 IIRP_SEC_USER_AGENT 设为含真实联系邮箱的值，"
    "然后重启服务。未配置时不向 SEC 发请求。"
)

# RFC 2606 names (and subdomains) can never reach a real mailbox.
RESERVED_TLDS = frozenset({"test", "invalid", "localhost", "example"})
RESERVED_DOMAINS = frozenset({"example.com", "example.org", "example.net"})


def _reserved_domain(domain):
    labels = domain.lower().rstrip(".").split(".")
    return labels[-1] in RESERVED_TLDS or ".".join(labels[-2:]) in RESERVED_DOMAINS


def sec_contact_ok(value):
    """True when the User-Agent carries a contact address SEC can actually reach."""
    value = (value or "").strip()
    if not value or value in SEC_TEMPLATE_USER_AGENTS:
        return False
    return any(
        not _reserved_domain(match.group(1))
        for match in re.finditer(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,})", value)
    )


def sec_configured():
    return sec_contact_ok(settings().sec_user_agent)


def sec_user_agent_state():
    configured = sec_configured()
    return {
        "configured": configured,
        "status": "CONFIGURED" if configured else "NEEDS_CONFIG",
        "message": "SEC User-Agent 已配置，自动更新可访问 SEC。" if configured else SEC_USER_AGENT_HINT,
    }


def sec_probe():
    if not sec_configured():
        return {"ok": False, "message": SEC_USER_AGENT_HINT}
    with httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False) as client:
        with client.stream(
            "GET",
            SEC_URL,
            headers={"User-Agent": settings().sec_user_agent, "Accept": "application/atom+xml"},
        ) as response:
            if response.status_code != 200:
                # Never expose upstream text, credentials, or arbitrary headers.
                retry_after = response.headers.get("Retry-After", "")
                retry_seconds = int(retry_after) if retry_after.isdigit() else 900
                return {
                    "ok": False,
                    "message": f"SEC 返回 HTTP {response.status_code}；已保留来源缺口。",
                    "cooldown": max(900, retry_seconds),
                }
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 5 * 1024**2:
                    raise ValueError("SEC 样本超过 5 MiB 测试预算。")
                chunks.append(chunk)
    payload = b"".join(chunks)
    tree = ElementTree.fromstring(payload, forbid_dtd=True)
    if tree.tag != "{http://www.w3.org/2005/Atom}feed":
        raise ValueError("SEC 响应不是预期 Atom 文档。")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = [
        {
            "id": x.findtext("a:id", namespaces=ns),
            "title": x.findtext("a:title", namespaces=ns),
            "updated": x.findtext("a:updated", namespaces=ns),
        }
        for x in tree.findall("a:entry", ns)
    ]
    return {
        "ok": True,
        "message": f"已读取 {len(entries)} 条 SEC 发现入口样本；尚未下载交易 XML，也不代表历史覆盖。",
        "payload": payload.decode("utf-8"),
        "media_type": "application/atom+xml",
        "entries": entries,
        "source_url": SEC_URL,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def market_probe(ticker):
    if not re.fullmatch(r"[A-Z0-9^][A-Z0-9.^=-]{0,14}", ticker):
        raise ValueError("无效证券标识。")
    import yfinance as yf

    # Third-party caches are project-local and not source evidence.
    yf.set_tz_cache_location(str(settings().runtime_dir / "provider-cache"))
    frame = yf.Ticker(ticker).history(
        period=development_budget().market_period,
        interval="1d",
        auto_adjust=False,
        back_adjust=False,
        actions=True,
        repair=False,
        timeout=10,
        raise_errors=True,
    )
    if frame.empty:
        return {"ok": False, "message": f"{ticker} 未返回日线；不能认定该证券不存在或没有交易。"}
    records = json.loads(frame.reset_index().to_json(orient="records", date_format="iso"))
    data = {
        "provider": "yfinance",
        "library_version": version("yfinance"),
        "ticker": ticker,
        "request": {
            "period": development_budget().market_period,
            "interval": "1d",
            "auto_adjust": False,
            "back_adjust": False,
            "repair": False,
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "records": records,
        "price_basis": "UNVERIFIED_PROVIDER_RECORDS",
        "eligible_for_analysis": False,
        "note": "采集返回记录，非供应商原始 HTTP；仅拆股口径与日线完整性尚未核验。",
    }
    return {
        "ok": True,
        "message": f"{ticker} 已取得 {len(records)} 条日线样本；拆股、股息与完整覆盖待 P2/P3 核验。",
        "payload": json.dumps(data, ensure_ascii=False),
        "media_type": "application/json",
        "record_count": len(records),
    }


if __name__ == "__main__":
    # The bound survives a hard-killed parent worker; child never writes business data.
    watchdog = threading.Timer(
        development_budget().provider_deadline_seconds, lambda: os._exit(124)
    )
    watchdog.daemon = True
    watchdog.start()
    try:
        result = sec_probe() if sys.argv[1] == "sec_probe" else market_probe(sys.argv[2])
    except Exception as exc:
        result = {
            "ok": False,
            "message": f"来源验证失败（{type(exc).__name__}）；已有数据保留。",
            "cooldown": 900,
        }
    print(json.dumps(result, ensure_ascii=False))
