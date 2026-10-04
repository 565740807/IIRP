"""Measurement claims must be supported by observed work and peak accounting."""

from scripts.validation.http_client import memory_summary, observed_publication_overlap


def test_queued_background_work_is_not_read_write_overlap():
    rows = [
        {
            "scenario": "background-compute-publication",
            "pass": True,
            "background_published_before": 0,
            "background_published": 0,
        }
        for _ in range(100)
    ]
    # The prior gate accepted this workload even if all writes happened afterwards.
    assert any(row["background_published"] < 10 for row in rows)
    assert not observed_publication_overlap(rows)


def test_publication_must_advance_during_a_successful_background_read_round():
    observed = {
        "scenario": "background-compute-publication",
        "pass": True,
        "background_published_before": 2,
        "background_published": 3,
    }
    assert observed_publication_overlap([observed])
    assert not observed_publication_overlap([{**observed, "pass": False}])
    assert not observed_publication_overlap([{**observed, "scenario": "read-only"}])
    assert not observed_publication_overlap([{**observed, "background_published_before": None}])


def test_client_peak_includes_freed_allocations_and_pre_request_swap():
    summary = memory_summary(
        [
            {
                "client_before": {"VmRSS_bytes": 100, "VmHWM_bytes": 120, "VmSwap_bytes": 12},
                "client_after": {"VmRSS_bytes": 110, "VmHWM_bytes": 700, "VmSwap_bytes": 0},
            }
        ]
    )
    assert summary["client_memory_peak_bytes"] == 700
    assert summary["client_sampled_rss_peak_bytes"] == 110
    assert summary["client_swap_peak_bytes"] == 12
    assert summary["client_memory_peak_scope"] == "process_lifetime_high_water"
