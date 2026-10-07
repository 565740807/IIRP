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
from iirp.messages import UserError, msg

SEC_URL = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&owner=only&count={development_budget().sec_items_per_page}&start=0&output=atom"

# The placeholder shipped in deploy/.env.example; a test keeps the two in sync.
SEC_TEMPLATE_USER_AGENTS = frozenset({"IIRP contact@example.invalid"})
SEC_USER_AGENT_HINT = msg("sec.user_agent_hint")

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
        "message": msg("sec.user_agent_configured") if configured else SEC_USER_AGENT_HINT,
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
                    "message": msg("probe.sec_http", status=response.status_code),
                    "cooldown": max(900, retry_seconds),
                }
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 5 * 1024**2:
                    raise UserError("probe.sec_too_large")
                chunks.append(chunk)
    payload = b"".join(chunks)
    tree = ElementTree.fromstring(payload, forbid_dtd=True)
    if tree.tag != "{http://www.w3.org/2005/Atom}feed":
        raise UserError("probe.sec_not_atom")
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
        "message": msg("probe.sec_ok", count=len(entries)),
        "payload": payload.decode("utf-8"),
        "media_type": "application/atom+xml",
        "entries": entries,
        "source_url": SEC_URL,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def market_probe(ticker):
    if not re.fullmatch(r"[A-Z0-9^][A-Z0-9.^=-]{0,14}", ticker):
        raise UserError("probe.ticker_invalid")
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
        return {"ok": False, "message": msg("probe.market_empty", ticker=ticker)}
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
        "note": msg("probe.market_note"),
    }
    return {
        "ok": True,
        "message": msg("probe.market_ok", ticker=ticker, count=len(records)),
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
            "message": msg("probe.failed", error=type(exc).__name__),
            "cooldown": 900,
        }
    print(json.dumps(result, ensure_ascii=False))
