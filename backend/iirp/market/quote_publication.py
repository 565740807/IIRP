"""Never publish a delayed older response over a newer quoted observation.

A quote may also carry ``history``: the daily bars of the last six months that
the market detail page asked for once. It lives 24 hours and is kept
across the ordinary quote refreshes, which fetch only the last 40 days.
"""
from datetime import datetime, timedelta

from iirp.messages import msg
from iirp.models import now

HISTORY_DAYS = 186
HISTORY_HOURS = 24


def history_snapshot(response):
    """Daily open/high/low/close of a history response, with its fetch and expiry times."""
    from iirp.market.quotes import _daily_records

    fetched = now()
    return {"records": [{key: row.get(key) for key in ("date", "open", "high", "low", "close")}
                        for row in _daily_records(response)],
            "fetched_at": fetched.isoformat(),
            "expires_at": (fetched + timedelta(hours=HISTORY_HOURS)).isoformat()}


def fresh_history(quote, current=None):
    history = (quote or {}).get("history")
    if not history or not history.get("expires_at"):
        return None
    return history if datetime.fromisoformat(history["expires_at"]) > (current or now()) else None


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
                "refresh_notice": msg("quote.retained"),
                **({"history": incoming["history"]} if incoming.get("history") else {})}, True
    kept = incoming.get("history") or fresh_history(previous)
    return {**incoming, "last_checked_at": incoming.get("fetched_at"),
            **({"history": kept} if kept else {})}, False
