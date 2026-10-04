"""Never publish a delayed older response over a newer quoted observation."""
from datetime import datetime


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def merge_quote(previous, incoming):
    old_time, new_time = _time(previous.get("source_time")), _time(incoming.get("source_time"))
    # Legacy daily snapshots borrowed metadata timestamps. Only the new paired
    # contract can assert timestamp order; legacy snapshots are replaced normally.
    old_paired = previous.get("quote_kind") == "provider_snapshot" and old_time
    older = bool(old_paired and new_time and new_time < old_time)
    fallback = bool(old_paired and not new_time and
                    incoming.get("as_of", "") <= previous.get("as_of", ""))
    older_daily = bool(not new_time and incoming.get("as_of", "") < previous.get("as_of", ""))
    if older or fallback or older_daily:
        return {**previous, "last_checked_at": incoming.get("fetched_at"),
                "next_refresh_at": incoming.get("next_refresh_at"),
                "refresh_notice": "来源返回较旧或缺少时间的报价，保留最后有效值"}, True
    return {**incoming, "last_checked_at": incoming.get("fetched_at")}, False
