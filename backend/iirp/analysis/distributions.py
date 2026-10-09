"""Decimal distribution statistics shared by every research mode."""

from decimal import Decimal, localcontext

from pydantic import BaseModel, Field

# Two-sided 95% normal quantile.
Z95 = Decimal("1.959963984540054")


class DistributionStatistics(BaseModel):
    n: int
    min: str | None = None
    q25: str | None = None
    median: str | None = None
    mean: str | None = None
    q75: str | None = None
    max: str | None = None
    worst: str | None = None
    best: str | None = None
    up: int = 0
    flat: int = 0
    up_ratio: str | None = None
    beat_ratio: str | None = None
    whisker_low: str | None = None
    whisker_high: str | None = None
    outliers: list[str] = Field(default_factory=list)


def statistics(values) -> dict:
    """N, quartiles (linear interpolation), mean, extremes and the up/flat counts."""
    numbers = sorted(Decimal(str(value)) for value in values if value is not None)
    if any(not value.is_finite() for value in numbers):
        raise ValueError("Distribution values must be finite or missing")

    def quantile(q):
        position = Decimal(len(numbers) - 1) * Decimal(q)
        low = int(position)
        return numbers[low] + (position - low) * (numbers[min(low + 1, len(numbers) - 1)] - numbers[low])

    with localcontext() as context:
        context.prec = 34
        output = {name: str(value) for name, value in {
            "min": min(numbers), "q25": quantile(".25"), "median": quantile(".5"),
            "mean": sum(numbers) / len(numbers), "q75": quantile(".75"), "max": max(numbers),
        }.items()} if numbers else {}
    box = {}
    if numbers:
        with localcontext() as context:
            context.prec = 34
            q1, q3 = Decimal(output["q25"]), Decimal(output["q75"])
            iqr = q3 - q1
            inside = [v for v in numbers if q1 - Decimal("1.5") * iqr <= v <= q3 + Decimal("1.5") * iqr]
            box = {"whisker_low": str(min(inside)), "whisker_high": str(max(inside)),
                   "outliers": [str(v) for v in numbers if v < min(inside) or v > max(inside)],
                   "up_ratio": str(Decimal(sum(v > 0 for v in numbers)) / len(numbers))}
    return DistributionStatistics(**box, n=len(numbers), **output, worst=output.get("min"), best=output.get("max"),
                                  up=sum(x > 0 for x in numbers), flat=sum(x == 0 for x in numbers)).model_dump()


def wilson_interval(successes: int, n: int, z: Decimal = Z95) -> tuple[Decimal, Decimal]:
    """Wilson score interval of a binomial share (NIST). It describes the
    historical share under independence; it is not a forecast probability."""
    if n <= 0:
        raise ValueError("n must be positive")
    with localcontext() as context:
        context.prec = 34
        count, p = Decimal(n), Decimal(successes) / n
        denominator = 1 + z * z / count
        center = (p + z * z / (2 * count)) / denominator
        half = z * (p * (1 - p) / count + z * z / (4 * count * count)).sqrt() / denominator
        low = Decimal(0) if successes == 0 else max(Decimal(0), center - half)
        high = Decimal(1) if successes == n else min(Decimal(1), center + half)
    return low, high


def paired_ratio(up, n):
    with localcontext() as context:
        context.prec = 34
        return str(Decimal(up) / n) if n else None


def comparison(values):
    """Cross-ticker facts for the conclusion, computed in the backend."""
    ranked = sorted(((symbol, Decimal(value)) for symbol, value in values if value is not None),
                    key=lambda item: item[1], reverse=True)
    return {"count": len(ranked), "positive": sum(value > 0 for _, value in ranked),
            "high": str(ranked[0][1]) if ranked else None, "high_ticker": ranked[0][0] if ranked else None,
            "low": str(ranked[-1][1]) if ranked else None, "low_ticker": ranked[-1][0] if ranked else None}
