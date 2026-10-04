"""Warnings travel with old frozen exports without rewriting their data."""

from iirp.fiscal_version import fiscal_version_notice


def test_version_specific_notice_is_based_on_frozen_result_not_running_code():
    assert fiscal_version_notice("native_earnings", "research-v10-time-source-attribution") is None
    assert fiscal_version_notice("event_import", "event-dates-v7-fiscal-scope-evidence") is None
    assert "常规财期" in fiscal_version_notice("native_earnings", "research-v8-robustness")
    assert "公告日期来源" in fiscal_version_notice("native_earnings", "research-v9-fiscal-kind-evidence")
    assert "覆盖分母" in fiscal_version_notice("event_import", "event-dates-v6-robustness")
    assert "无法确认" in fiscal_version_notice("event_import", None)
    assert fiscal_version_notice("monthly", "research-v8-robustness") is None
