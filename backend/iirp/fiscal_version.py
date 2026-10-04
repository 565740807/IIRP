"""Explain an immutable result's historical fiscal-qualification contract."""

import re


def fiscal_version_notice(kind: str, version: object) -> str | None:
    if kind not in {"native_earnings", "event_import"}:
        return None
    pattern = r"research-v(\d+)(?:-|$)" if kind == "native_earnings" else r"event-dates-v(\d+)(?:-|$)"
    match = re.match(pattern, version) if isinstance(version, str) else None
    threshold = 10 if kind == "native_earnings" else 7
    if match and int(match[1]) >= threshold:
        return None
    if not match:
        return "该冻结结果未记录可识别的计算版本，无法确认财期资格与覆盖分母口径；请按当前版本新建同条件研究核对。"
    if kind == "native_earnings":
        if match and int(match[1]) == 9:
            return "旧版冻结财报结果的公告日期来源及实际公开时刻归因尚未独立核对，精确反应 N 可能偏高；请用当前版本新建同条件研究核对。旧结果保留供版本复现。"
        return "旧版冻结财报结果可能把未核对常规财期类型或实际公开时刻来源的事件计入历史 N/精确反应；请用当前版本新建同条件研究核对。旧结果保留供版本复现。"
    return "旧版冻结导入研究可能扩大导入年份/财季覆盖分母，财报也可能把未获来源支持的常规财期计入 N；请用当前版本新建同条件研究核对。旧结果保留供版本复现。"
