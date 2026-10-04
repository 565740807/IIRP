"""Stdlib real HTTP client, run outside the measured application's cgroup.

Each request has a 5s socket deadline, no retries. Full-body HTTP latency and
JSON assertion time are reported separately. A slow completed request still
fails the predeclared maximum; failures stay in the samples.
"""

import argparse
import hashlib
import http.client
import json
import math
import os
import statistics
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[2]


def memory():
    result = {"pid": os.getpid()}
    for row in Path("/proc/self/status").read_text().splitlines():
        if row.startswith(("VmRSS:", "VmSwap:", "VmHWM:")):
            key, value, _unit = row.split()
            result[key.rstrip(":") + "_bytes"] = int(value) * 1024
    result["cpu_seconds"] = time.process_time()
    result["cgroup"] = Path("/proc/self/cgroup").read_text().strip()
    return result


def statistics_for(rows, key):
    values = sorted(row[key] for row in rows if key in row)
    return {
        "n": len(values),
        "p50_ms": statistics.median(values) if values else None,
        "p95_ms": values[math.ceil(len(values) * 0.95) - 1] if values else None,
        "max_ms": max(values) if values else None,
    }


def memory_summary(rows):
    observations = [row[key] for row in rows for key in ("client_before", "client_after")]
    return {
        # VmHWM includes allocations released before the after-request observation.
        # It is the process lifetime high-water mark, not a scenario-local peak.
        "client_memory_peak_bytes": max(
            (item.get("VmHWM_bytes", item.get("VmRSS_bytes", 0)) for item in observations),
            default=0,
        ),
        "client_memory_peak_scope": "process_lifetime_high_water",
        "client_sampled_rss_peak_bytes": max(
            (item.get("VmRSS_bytes", 0) for item in observations), default=0
        ),
        "client_swap_peak_bytes": max(
            (item.get("VmSwap_bytes", 0) for item in observations), default=0
        ),
        "client_swap_peak_scope": "before_and_after_request_samples",
    }


def observed_publication_overlap(rows):
    """Queued work alone does not demonstrate writes during a measured read round."""
    return any(
        row.get("pass")
        and row.get("scenario") == "background-compute-publication"
        and row.get("background_published_before") is not None
        and row.get("background_published", 0) > row["background_published_before"]
        for row in rows
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    f = json.loads(args.manifest.read_text())
    u = urlsplit(f["base"])
    assert u.scheme == "http" and u.hostname == "127.0.0.1" and u.port != 18081
    assert (
        f["synthetic"]
        and f["no_external_provider_calls"]
        and f["database"].startswith("iirp_v1_test_")
    )
    targets = json.loads((ROOT / "config/validation-targets.json").read_text())
    assert int(f["application_memory_max"]) == targets["application_memory_bytes"]
    out = args.output
    out.mkdir(parents=True, exist_ok=False)
    (out / "targets-before-run.json").write_text(json.dumps(targets, indent=2))
    records = []

    def request(path, method="GET"):
        conn = http.client.HTTPConnection(
            u.hostname, u.port, timeout=targets["socket_timeout_seconds"]
        )
        start = time.perf_counter()
        try:
            conn.request(
                method,
                path,
                headers={
                    "X-IIRP-Client": "web",
                    "Origin": f["base"],
                    "X-IIRP-Test-Identity": f["validation_id"],
                },
            )
            response = conn.getresponse()
            payload = response.read()
            ms = (time.perf_counter() - start) * 1000
            assert response.status == 200, f"HTTP status {response.status}"
            return payload, ms
        finally:
            conn.close()

    identity = json.loads(request("/__iirp_test_identity__")[0])
    for key in ["validation_id", "database", "synthetic", "no_external_provider_calls"]:
        assert identity[key] == f[key], "Live identity mismatch"
    result_path = (
        "/api/v1/events/analyses/"
        + f["analysis_id"]
        + "?"
        + urlencode({"result_id": f["result_id"], "include_freshness": "false"})
    )
    overlap_path = f"/api/v1/analyses/{f['analysis_id']}/results/{f['result_id']}/event-overlaps"
    query = {"event_key": "event-00000", "limit": 100}
    log = (out / "samples.jsonl").open("x")
    try:
        for scenario in ["read-only", "background-compute-publication"]:
            if scenario != "read-only":
                launched = json.loads(request("/__iirp_test_workload__", "POST")[0])
                assert len(launched["queued_analyses"]) == 10
                (out / "background-start.json").write_text(json.dumps(launched, indent=2))
            for index in range(targets["rounds_per_network_scenario"]):
                row = {
                    "scenario": scenario,
                    "index": index,
                    "unix": time.time(),
                    "client_before": memory(),
                    "retry": 0,
                }
                start = time.perf_counter()
                try:
                    if scenario != "read-only":
                        status = json.loads(request("/__iirp_test_workload_status__")[0])
                        row["background_published_before"] = len(status["published"])
                    payload, row["result_ms"] = request(result_path)
                    row["result_bytes"] = len(payload)
                    view = json.loads(payload)
                    assert view["result_id"] == f["result_id"] and len(view["data"]["rows"]) == 1500
                    fingerprint = hashlib.sha256(
                        json.dumps(
                            view["data"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
                        ).encode()
                    ).hexdigest()
                    assert fingerprint == f["data_sha256"]
                    del payload, view
                    payload, row["detail_ms"] = request(overlap_path + "?" + urlencode(query))
                    detail = json.loads(payload)
                    assert detail["total"] == 1499 and len(detail["items"]) == 100
                    row["pass"] = True
                    if scenario != "read-only":
                        status = json.loads(request("/__iirp_test_workload_status__")[0])
                        row["background_published"] = len(status["published"])
                        row["background_worker_exit"] = status["worker_exit"]
                except Exception as exc:
                    row.update(
                        {
                            "pass": False,
                            "error": type(exc).__name__,
                            "timeout": isinstance(exc, TimeoutError),
                        }
                    )
                row["wall_with_validation_ms"] = (time.perf_counter() - start) * 1000
                row["client_after"] = memory()
                records.append(row)
                log.write(json.dumps(row) + "\n")
                log.flush()
            # Verify every page, order, exact count and preview after each scenario.
            keys, cursor, pages = [], None, 0
            while True:
                payload, _ms = request(
                    overlap_path
                    + "?"
                    + urlencode({**query, **({"cursor": cursor} if cursor else {})})
                )
                detail = json.loads(payload)
                keys.extend(x["event_key"] for x in detail["items"])
                pages += 1
                cursor = detail["next_cursor"]
                if not cursor:
                    break
            assert keys == [f"event-{i:05}" for i in range(1, 1500)]
            assert detail["summary"]["preview_event_ids"] == keys[:10]
            (out / (scenario + "-all-pages.json")).write_text(
                json.dumps(
                    {
                        "pages": pages,
                        "count": len(keys),
                        "keys_sha256": hashlib.sha256(json.dumps(keys).encode()).hexdigest(),
                    },
                    indent=2,
                )
            )
        deadline = time.monotonic() + 1100
        while True:
            status = json.loads(request("/__iirp_test_workload_status__")[0])
            if len(status["published"]) == 10:
                break
            assert status["worker_exit"] is None and time.monotonic() < deadline, (
                "Background publication failed"
            )
            time.sleep(0.5)
        (out / "background-complete.json").write_text(json.dumps(status, indent=2))
    finally:
        log.close()
        try:
            request("/__iirp_test_stop__", "POST")
        except (OSError, AssertionError):
            pass
        summary = {}
        for scenario in ["read-only", "background-compute-publication"]:
            rows = [r for r in records if r["scenario"] == scenario]
            summary[scenario] = {
                "result": statistics_for(rows, "result_ms"),
                "detail": statistics_for(rows, "detail_ms"),
                "failures": sum("error" in r for r in rows),
                "timeouts": sum(r.get("timeout", False) for r in rows),
                "retries": 0,
                **memory_summary(rows),
                "publication_during_read_round_observed": observed_publication_overlap(rows),
            }
        (out / "summary.json").write_text(json.dumps(summary, indent=2))
    for item in summary.values():
        assert item["failures"] == 0 and item["result"]["n"] == 100 and item["detail"]["n"] == 100
        assert item["result"]["p95_ms"] <= targets["default_result_p95_ms"]
        assert item["detail"]["p95_ms"] <= targets["overlap_detail_p95_ms"]
        assert item["result"]["max_ms"] <= targets["maximum_http_ms"]
        assert item["detail"]["max_ms"] <= targets["maximum_http_ms"]
    assert observed_publication_overlap(records), (
        "No publication occurred during a measured read round (including client validation)"
    )


if __name__ == "__main__":
    main()
