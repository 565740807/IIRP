"""Closing-price observations expressed as decimal ratios, never percent points.

Input positions must already follow the verified trading calendar. Each position
is one expected, completed trading session; None means missing data. Future
sessions must not be appended. These helpers do not fetch or adjust prices.
"""

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Literal, Sequence

Status = Literal[
    "available",
    "missing_price",
    "not_yet_formed",
    "incomplete_path",
    "no_sessions",
    "unconfirmed_event_time",
]
ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass(frozen=True)
class Metric:
    value: Decimal | None
    status: Status


@dataclass(frozen=True)
class PathSummary:
    path: tuple[Decimal | None, ...]
    endpoint: Metric
    relative_high: Metric
    relative_low: Metric
    closing_max_drawdown: Metric
    complete: bool
    observed_sessions: int
    expected_sessions: int


@dataclass(frozen=True)
class EventWindow:
    reaction_day: int
    cumulative: Metric
    after_open: Metric


@dataclass(frozen=True)
class EventReturns:
    opening_gap: Metric
    windows: dict[int, EventWindow]


def _validate(price: Decimal | None) -> None:
    if price is None:
        return
    if not isinstance(price, Decimal):
        raise TypeError("Prices must be Decimal or None; binary floats are not accepted")
    if not price.is_finite() or price <= ZERO:
        raise ValueError("Prices must be finite and strictly positive")


def endpoint_change(start: Decimal | None, end: Decimal | None) -> Metric:
    """Return end/start - 1. The caller must supply the intended boundaries."""
    _validate(start)
    _validate(end)
    if start is None or end is None:
        return Metric(None, "missing_price")
    with localcontext() as context:
        context.prec = 34
        return Metric(end / start - ONE, "available")


def price_path(
    prices: Sequence[Decimal | None],
    *,
    baseline: Decimal | None,
) -> tuple[Decimal | None, ...]:
    """Keep gaps; a missing baseline never shifts to the next available close."""
    _validate(baseline)
    return tuple(endpoint_change(baseline, price).value for price in prices)


def maximum_drawdown(prices: Sequence[Decimal | None]) -> Metric:
    """Positive peak-to-later-trough closing drawdown; requires the entire path."""
    for price in prices:
        _validate(price)
    if not prices:
        return Metric(None, "no_sessions")
    if any(price is None for price in prices):
        return Metric(None, "incomplete_path")
    peak = prices[0]
    result = ZERO
    with localcontext() as context:
        context.prec = 34
        for price in prices:
            peak = max(peak, price)
            result = max(result, ONE - price / peak)
    return Metric(result, "available")


def interval_summary(prices: Sequence[Decimal | None]) -> PathSummary:
    """Normalize to first calendar-selected close, excluding first-day intraday move."""
    if not prices:
        empty = Metric(None, "no_sessions")
        return PathSummary((), empty, empty, empty, empty, False, 0, 0)
    path = price_path(prices, baseline=prices[0])
    observed = sum(price is not None for price in prices)
    complete = observed == len(prices)
    unavailable = Metric(None, "incomplete_path")
    return PathSummary(
        path=path,
        endpoint=endpoint_change(prices[0], prices[-1]),
        relative_high=Metric(max(path), "available") if complete else unavailable,
        relative_low=Metric(min(path), "available") if complete else unavailable,
        closing_max_drawdown=maximum_drawdown(prices),
        complete=complete,
        observed_sessions=observed,
        expected_sessions=len(prices),
    )


def event_returns(
    previous_close: Decimal | None,
    reaction_open: Decimal | None,
    completed_closes: Sequence[Decimal | None],
    *,
    opening_attribution: bool,
    windows: Sequence[int] = (1, 5, 20, 60),
) -> EventReturns:
    """R is day 1; unavailable day N is never replaced by the latest close.

    Set opening_attribution=False for intraday, date-only, or conflicting event
    times. Calendar/event-time validation is the caller's responsibility. The
    cumulative date observation can remain visible without opening attribution.
    """
    for price in (previous_close, reaction_open, *completed_closes):
        _validate(price)
    if any(type(day) is not int or day < 1 for day in windows):
        raise ValueError("Event windows must contain positive integer reaction-day numbers")
    uncertain = Metric(None, "unconfirmed_event_time")
    opening_gap = (
        endpoint_change(previous_close, reaction_open) if opening_attribution else uncertain
    )
    result = {}
    for day in windows:
        if day > len(completed_closes):
            cumulative = Metric(None, "not_yet_formed")
            after_open = cumulative if opening_attribution else uncertain
        else:
            close = completed_closes[day - 1]
            cumulative = endpoint_change(previous_close, close)
            after_open = endpoint_change(reaction_open, close) if opening_attribution else uncertain
        result[day] = EventWindow(day, cumulative, after_open)
    return EventReturns(opening_gap, result)
