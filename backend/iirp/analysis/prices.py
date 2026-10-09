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
