"""Price change before and after one insider transaction (D16)."""

from datetime import date, datetime

from iirp.analysis.calendar import (
    as_et,
    calendar_version,
    completed_through,
    next_regular_open_after,
    next_session,
    reaction_session,
    session_window,
)
from iirp.analysis.research import CALCULATION_VERSION, _bars_by_date, _point, _price, _ratio, _text
from iirp.messages import msg


def transaction_price_context(
    bars: list[dict],
    transaction_date: str | date,
    accepted_at: str | datetime | None,
    today: date | None = None,
    calendar: str = "XNYS",
    n: int = 5,
) -> dict:
    """Two explicitly different day-zero conventions and next-open observation.

    ``before_n`` = C(t)/C(t−n) − 1 and ``after_n`` = C(t+n)/C(t) − 1 (D16);
    the fixed 1- and 5-session values stay for exports.
    """
    index = _bars_by_date(bars)
    cutoff = completed_through(today, calendar)
    original = (
        date.fromisoformat(transaction_date)
        if isinstance(transaction_date, str)
        else transaction_date
    )
    transaction = next_session(original, calendar)

    def status(day: date, baseline, value) -> str:
        if day > cutoff:
            return "not_yet_formed"
        return "available" if baseline is not None and value is not None else "missing_price"

    def context(anchor: date, before: int = max(n, 5), after: int = max(n, 5)) -> dict:
        days = session_window(anchor, before, after, calendar)
        start_n, end_n = days[before - n], days[before + n]
        baseline = _price(index, anchor, cutoff)
        future = days[before + 1 :]
        path_prices = [_price(index, day, cutoff) for day in days if day <= cutoff]
        path_complete = all(price is not None for price in path_prices)
        first, last = _price(index, start_n, cutoff), _price(index, end_n, cutoff)
        return {
            "baseline_date": anchor.isoformat(),
            "baseline_close": _text(baseline),
            "n": n,
            "before_n": _ratio(first, baseline),
            "after_n": _ratio(baseline, last),
            "before_date": start_n.isoformat(),
            "after_date": end_n.isoformat(),
            "before_status": status(anchor, first, baseline),
            "after_status": status(end_n, baseline, last),
            "pre_5": _ratio(_price(index, days[before - 5], cutoff), baseline),
            "day_1": _ratio(baseline, _price(index, future[0], cutoff)),
            "day_5": _ratio(baseline, _price(index, future[4], cutoff)),
            "day_1_date": future[0].isoformat(),
            "day_5_date": future[4].isoformat(),
            "mature_sessions": sum(day <= cutoff for day in future),
            "status": "not_yet_formed"
            if anchor > cutoff
            else (
                "missing_baseline"
                if baseline is None
                else "available"
                if path_complete
                else "incomplete_path"
            ),
            "day_1_status": "not_yet_formed"
            if future[0] > cutoff
            else (
                "available" if baseline and _price(index, future[0], cutoff) else "missing_price"
            ),
            "day_5_status": "not_yet_formed"
            if future[4] > cutoff
            else (
                "available" if baseline and _price(index, future[4], cutoff) else "missing_price"
            ),
            "points": [
                {
                    **_point(day, i - before, baseline, index, cutoff),
                    "close": _text(_price(index, day, cutoff)),
                }
                for i, day in enumerate(days)
                if day <= cutoff and abs(i - before) <= n
            ],
        }

    left = {
        **context(transaction),
        "title": msg("window.title.transaction"),
        "original_date": original.isoformat(),
        "observation_anchor": transaction.isoformat(),
        "non_session_transaction": transaction != original,
    }
    right = None
    next_open = None
    date_only_acceptance = isinstance(accepted_at, str) and len(accepted_at) == 10
    if accepted_at is not None:
        reaction = reaction_session(accepted_at, calendar=calendar)
        baseline = date.fromisoformat(reaction["baseline_date"])
        reaction_day = date.fromisoformat(reaction["reaction_date"])
        observation = context(baseline)
        right = {
            **observation,
            **reaction,
            "price_status": observation["status"],
            "title": msg("window.title.disclosure_date" if date_only_acceptance
                         else "window.title.disclosure"),
            "opening_gap": _ratio(
                _price(index, baseline, cutoff), _price(index, reaction_day, cutoff, "open")
            )
            if reaction["opening_attribution"]
            else None,
        }
    if accepted_at is not None and not date_only_acceptance:
        starting = next_regular_open_after(accepted_at, calendar)
        days = session_window(starting, 0, 4, calendar)
        opening = _price(index, starting, cutoff, "open")
        next_open = {
            "title": msg("window.title.next_open"),
            "start_date": starting.isoformat(),
            "opening_price": _text(opening),
            "day_1": _ratio(opening, _price(index, days[0], cutoff)),
            "day_5": _ratio(opening, _price(index, days[4], cutoff)),
            "day_1_date": days[0].isoformat(),
            "day_5_date": days[4].isoformat(),
            "mature_sessions": sum(day <= cutoff for day in days),
            "day_1_status": "not_yet_formed"
            if days[0] > cutoff
            else ("available" if opening and _price(index, days[0], cutoff) else "missing_price"),
            "day_5_status": "not_yet_formed"
            if days[4] > cutoff
            else ("available" if opening and _price(index, days[4], cutoff) else "missing_price"),
        }
    return {
        "transaction": left,
        "disclosure": right,
        "next_open": next_open,
        "cutoff_date": cutoff.isoformat(),
        "calendar": calendar,
        "calendar_version": calendar_version(),
        "calculation_version": CALCULATION_VERSION,
        "accepted_at": as_et(accepted_at).isoformat()
        if accepted_at is not None and not date_only_acceptance
        else None,
        "accepted_date": as_et(accepted_at).date().isoformat() if accepted_at is not None else None,
        "calendar_days_to_disclosure": (as_et(accepted_at).date() - original).days
        if accepted_at
        else None,
        "price_basis": "split_only",
        "disclosure_status": "available" if right else "missing_acceptance_time",
    }
