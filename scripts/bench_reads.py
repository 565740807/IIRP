"""Read-path latency benchmark for one running IIRP instance (GET requests only).

Measures home, the Insider feed (first page and pages 2/3 for all, buy and sell
filters), the task list, company and person history and a transaction detail.
The company and person pages are also measured as the browser loads them:
six months of history, the before/after-5 windows of its rows and the cached
daily bars of its ticker (``*_page`` adds the three request times).
Requests run strictly one at a time with a pause in between, so the benchmark
never becomes a load test. Results are JSON: p50 (median), p95 (nearest rank),
max and response bytes per endpoint.

Usage:
    python3 scripts/bench_reads.py --base-url http://127.0.0.1:18081 \\
        --runs 10 --output ../bench/live.json
    # Reuse the same company/person/transaction on another instance:
    python3 scripts/bench_reads.py --base-url http://127.0.0.1:18092 \\
        --ids-from ../bench/live.json --output ../bench/drill.json

Standard library only; it never creates collections or other write commands.
"""

import argparse
import json
import math
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_RUNS = 20
FEED_FILTERS = (("all", "feed"), ("buy", "feed_buy"), ("sell", "feed_sell"))
GROUPS = ("home", "feed", "feed_buy", "feed_sell", "overview", "jobs", "company", "person", "transaction")


def nearest_rank(values, percent):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percent / 100 * len(ordered)) - 1)]


class Client:
    def __init__(self, base_url, timeout, pause):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.pause = pause
        self.samples = {}

    def get(self, name, path, params=None):
        """Timed GET; returns parsed JSON or None. Records status, time and bytes."""
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        started = time.perf_counter()
        status, body = None, b""
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status, body = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            status = type(exc).__name__
        elapsed = (time.perf_counter() - started) * 1000
        self.samples.setdefault(name, []).append(
            {"status": status, "ms": round(elapsed, 2), "bytes": len(body)}
        )
        time.sleep(self.pause)
        if status != 200:
            return None
        try:
            return json.loads(body)
        except ValueError:
            return None


def discover_ids(client):
    """First company, person and transaction on the newest feed page."""
    page = client.get("discovery", "/api/v1/feed", {"type": "all", "order": "transaction"}) or {}
    ids = {}
    for group in page.get("groups", []):
        ids.setdefault("company", group.get("issuer_id"))
        for row in group.get("transactions", []):
            ids.setdefault("transaction", row.get("id"))
            if row.get("owner_ids"):
                ids.setdefault("person", row["owner_ids"][0])
        if len(ids) == 3:
            break
    return ids


def feed_pages(client, kind, prefix):
    first = client.get(prefix + "_first", "/api/v1/feed", {"type": kind, "order": "transaction"})
    page, session = first, (first or {}).get("session_id")
    for number in (2, 3):
        cursor = (page or {}).get("next_cursor")
        if not session or not cursor:
            break
        page = client.get(
            f"{prefix}_page{number}",
            "/api/v1/feed",
            {"type": kind, "order": "transaction", "session_id": session, "cursor": cursor},
        )
    return first


def entity_page(client, kind, identifier):
    """The requests of one company/person page; records their summed time as ``<kind>_page``."""
    today = datetime.now(timezone.utc).date()
    start = (today.replace(day=1) - timedelta(days=150)).isoformat()
    before = {name: len(values) for name, values in client.samples.items()}
    path = f"/api/v1/{'companies' if kind == 'company' else 'people'}/{identifier}"
    history = client.get(f"{kind}_page_history", path, {"start_date": start, "end_date": today.isoformat(), "limit": 200})
    rows = ((history or {}).get("data") or {}).get("items") or []
    items = ",".join(sorted({f"{row['ticker']}|{row['transaction_date']}|{row['issuer_id']}" for row in rows
                             if row.get("ticker") and row.get("transaction_date") and not row.get("date_anomaly")}))
    if items:
        client.get(f"{kind}_page_windows", "/api/v1/insider/windows", {"items": items, "n": 5})
    tickers = sorted({row["ticker"] for row in rows if row.get("ticker")})
    if tickers:
        client.get(f"{kind}_page_bars", "/api/v1/insider/bars", {"ticker": tickers[0], "start_date": start})
    added = [values[before.get(name, 0):] for name, values in client.samples.items() if name.startswith(f"{kind}_page_")]
    total = sum(sample["ms"] for group in added for sample in group)
    ok = all(sample["status"] == 200 for group in added for sample in group)
    client.samples.setdefault(f"{kind}_page", []).append({"status": 200 if ok else "error", "ms": round(total, 2),
                                                          "bytes": sum(sample["bytes"] for group in added for sample in group)})


def summarize(samples):
    ok = [x for x in samples if x["status"] == 200]
    errors = {}
    for sample in samples:
        if sample["status"] != 200:
            errors[str(sample["status"])] = errors.get(str(sample["status"]), 0) + 1
    times = [x["ms"] for x in ok]
    sizes = [x["bytes"] for x in ok]
    return {
        "count": len(samples),
        "ok": len(ok),
        "errors": errors,
        "p50_ms": round(statistics.median(times), 1) if times else None,
        "p95_ms": round(nearest_rank(times, 95), 1) if times else None,
        "max_ms": round(max(times), 1) if times else None,
        "bytes_p50": int(statistics.median(sizes)) if sizes else None,
        "bytes_max": max(sizes) if sizes else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--runs", type=int, default=10, help=f"每个接口的次数，最多 {MAX_RUNS}")
    parser.add_argument("--pause", type=float, default=0.5, help="相邻请求之间的间隔秒数")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--jobs-limit", type=int, default=20)
    parser.add_argument("--ids-from", type=Path, help="复用另一份结果中的公司、人员和交易 ID")
    parser.add_argument("--company")
    parser.add_argument("--person")
    parser.add_argument("--transaction")
    parser.add_argument("--only", default=",".join(GROUPS),
                        help="逗号分隔的接口组：" + ",".join(GROUPS))
    parser.add_argument("--label", default="")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.runs <= MAX_RUNS:
        parser.error(f"--runs 必须在 1..{MAX_RUNS}")
    if args.output.exists():
        parser.error("输出文件已存在；请使用新路径")
    only = set(args.only.split(","))
    if not only <= set(GROUPS):
        parser.error("--only 含未知接口组：" + ",".join(sorted(only - set(GROUPS))))

    client = Client(args.base_url, args.timeout, args.pause)
    started = datetime.now(timezone.utc)
    system = client.get("system", "/api/v1/system") or {}
    explicit = {k: v for k, v in (("company", args.company), ("person", args.person),
                                  ("transaction", args.transaction)) if v}
    if args.ids_from:
        ids = json.loads(args.ids_from.read_text())["ids"]
    elif {"company", "person", "transaction"} & only and len(explicit) < 3:
        ids = discover_ids(client)
    else:
        ids = {}
    ids.update(explicit)
    totals = {}
    for run in range(args.runs):
        if "home" in only:
            client.get("home", "/api/v1/home")
        if "overview" in only:
            client.get("insider_overview", "/api/v1/insider/overview")
        for kind, prefix in FEED_FILTERS:
            if prefix not in only:
                continue
            first = feed_pages(client, kind, prefix)
            if first and run == 0:
                totals[prefix] = first.get("total_groups")
        if "jobs" in only:
            jobs = client.get("jobs", "/api/v1/jobs", {"limit": args.jobs_limit})
            if jobs and run == 0:
                totals["jobs_items"] = len(jobs.get("items", []))
        if "company" in only and ids.get("company"):
            client.get("company_history", f"/api/v1/companies/{ids['company']}")
            entity_page(client, "company", ids["company"])
        if "person" in only and ids.get("person"):
            client.get("person_history", f"/api/v1/people/{ids['person']}")
            entity_page(client, "person", ids["person"])
        if "transaction" in only and ids.get("transaction"):
            client.get("transaction_detail", f"/api/v1/transactions/{ids['transaction']}")
        print(f"run {run + 1}/{args.runs} done", file=sys.stderr)

    results = {name: summarize(samples) for name, samples in client.samples.items()
               if name not in ("system", "discovery")}
    report = {
        "label": args.label,
        "base_url": args.base_url,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "runs": args.runs,
        "pause_seconds": args.pause,
        "server": {k: system.get(k) for k in ("version", "migration")},
        "ids": ids,
        "totals": totals,
        "results": results,
        "samples": client.samples,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    width = max(map(len, results))
    print(f"{'endpoint':<{width}}  {'ok':>5}  {'p50 ms':>9}  {'p95 ms':>9}  {'bytes':>9}  errors")
    for name, row in results.items():
        print(f"{name:<{width}}  {row['ok']:>2}/{row['count']:<2}  {row['p50_ms']!s:>9}  "
              f"{row['p95_ms']!s:>9}  {row['bytes_p50']!s:>9}  {row['errors'] or ''}")


if __name__ == "__main__":
    main()
