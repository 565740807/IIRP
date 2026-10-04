"""One decimal statistics and same-date pairing contract for every research mode."""

from collections import Counter
from datetime import date
from decimal import Decimal, localcontext
from heapq import heappop, heappush
from typing import Any, Literal

from pydantic import BaseModel, Field

from iirp.analytics.calendar import sessions
from iirp.analytics.fiscal_calendar import quarter_calendar
from iirp.analytics.prices import endpoint_change


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


class ProportionUncertainty(BaseModel):
    n: int
    up: int
    flat: int
    observed: str | None = None
    lower: str | None = None
    upper: str | None = None
    method: Literal["wilson_score"] = "wilson_score"
    confidence_level: str = "0.95"
    assumptions: str = "独立、上涨概率恒定、样本事先确定的二项观察；平盘计入非上涨。金融样本的相关性、市场变化及事后筛选可能违反假设。"
    interpretation: str = "条件成立时的历史上涨比例估计区间，不是未来上涨概率或收益预测区间。"


class LeaveOneOutPoint(BaseModel):
    omitted_key: str
    omitted_year: int | None = None
    omitted_return: str
    n: int
    mean: str | None = None
    median: str | None = None
    mean_change: str | None = None


class LeaveOneOutSensitivity(BaseModel):
    available: bool
    points: list[LeaveOneOutPoint] = Field(default_factory=list)
    mean_min: str | None = None
    mean_max: str | None = None
    median_min: str | None = None
    median_max: str | None = None
    max_abs_mean_change: str | None = None
    influential_sample_keys: list[str] = Field(default_factory=list)
    interpretation: str = "每次去掉一个实际年度或事件后重算；检查单样本影响，不是样本外验证，也不增加独立样本。"


class HistoricalSegment(BaseModel):
    label: str
    target_years: list[int]
    sample_keys: list[str]
    statistics: DistributionStatistics


class HistoricalSegments(BaseModel):
    policy: Literal["target_year_midpoint", "candidate_year_midpoint"]
    boundary_year: int | None = None
    earlier: HistoricalSegment
    later: HistoricalSegment
    interpretation: str = "按目标历史年份跨度中点固定分段；目标未登记时用全部候选历史年。边界不依收益或缺失后有效N选择；描述性比较，不是显著性或样本外检验。"


class OverlappingSamples(BaseModel):
    first_key: str
    second_key: str
    start_date: str
    end_date: str


class DistributionRobustness(BaseModel):
    sample_unit: Literal["annual_sample", "event_sample", "paired_sample"]
    n: int
    sample_keys: list[str]
    proportion: ProportionUncertainty
    leave_one_out: LeaveOneOutSensitivity
    historical_segments: HistoricalSegments
    overlapping_pairs: list[OverlappingSamples] = Field(default_factory=list)
    overlap_pair_count: int = 0
    overlap_details_truncated: bool = False
    same_year_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class ResearchMethodology(BaseModel):
    price_basis: str = "仅拆股调整的价格收益，不含现金股息再投资。"
    baseline_rule: str
    sample_unit: Literal["annual_sample", "event_sample"]
    path_n_rule: str = "路径每个位置的N是该位置有效样本数，可与完整窗口或配对样本N不同；当前年与未完整窗口不混入完整历史分布。"
    drawdown_rule: str = "最大回撤取实际基准至终点的完整日收盘路径，表示正数的峰值到后续低点损失；不含全部盘中路径，不代表可实现的止损价格。"
    benchmark_rule: str = "股票自身N与同日期配对N分列；基准缺失不改动股票N。相对基准为逐样本简单收益差，不是风险调整alpha。"
    exploration_scope: str
    interpretation: str = "箱体和分位带描述这些实际历史样本，不是未来预测区间；均值、排名、上涨次数或单一N阈值均不能证明可交易规律。"


class Distribution(BaseModel):
    key: str
    label: str
    metric: str
    group: str
    stock: DistributionStatistics
    paired_stock: DistributionStatistics | None = None
    benchmark: DistributionStatistics | None = None
    difference: DistributionStatistics | None = None
    years: list[int] = Field(default_factory=list)
    target_years: list[int] = Field(default_factory=list)
    missing: dict[str, int] = Field(default_factory=dict)
    samples: list[dict[str, Any]] = Field(default_factory=list)
    whiskers: str = "sample_min_max"
    empty_reason: str | None = None
    robustness: DistributionRobustness | None = None
    paired_difference_robustness: DistributionRobustness | None = None
    closing_max_drawdown: DistributionStatistics | None = None


class MonthlyRanking(BaseModel):
    month: int
    statistics: DistributionStatistics
    valid_years: list[int]
    target_years: list[int]
    mean_rank: int | None = None
    median_rank: int | None = None
    mean_reverse_rank: int | None = None
    median_reverse_rank: int | None = None


def monthly_rankings(distributions: list[dict]) -> list[dict]:
    """Dense ranks from full historical months; N=0 never enters a ranking."""
    entries = [MonthlyRanking(month=int(d["group"]), statistics=d["stock"],
                             valid_years=d["years"], target_years=d["target_years"]).model_dump()
               for d in distributions]
    for metric in ("mean", "median"):
        values = sorted({Decimal(r["statistics"][metric]) for r in entries
                         if r["statistics"]["n"] and r["statistics"][metric] is not None}, reverse=True)
        for row in entries:
            value = row["statistics"][metric]
            if row["statistics"]["n"] and value is not None:
                position = values.index(Decimal(value))
                row[f"{metric}_rank"] = position + 1
                row[f"{metric}_reverse_rank"] = len(values) - position
    return sorted(entries, key=lambda r: r["month"])


def statistics(values) -> dict:
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
    return DistributionStatistics(n=len(numbers), **output, worst=output.get("min"), best=output.get("max"),
                                  up=sum(x > 0 for x in numbers), flat=sum(x == 0 for x in numbers)).model_dump()


def robustness(samples: list[dict], *, target_years: list[int], candidate_years: list[int],
               sample_unit: str, value_key: str = "stock") -> dict:
    """Use only the qualified cohort; never resample or drop an outlier."""
    values = [Decimal(item[value_key]) for item in samples]
    stats = statistics(values)
    n, up = len(values), stats["up"]
    proportion = ProportionUncertainty(n=n, up=up, flat=stats["flat"])
    points = []
    with localcontext() as context:
        context.prec = 34
        if n:
            # NIST Wilson score interval. This is not a future-probability interval.
            z = Decimal("1.959963984540054")
            count, p = Decimal(n), Decimal(up) / n
            denominator = 1 + z * z / count
            center = (p + z * z / (2 * count)) / denominator
            half = z * (p * (1 - p) / count + z * z / (4 * count * count)).sqrt() / denominator
            proportion.observed = str(p)
            proportion.lower = "0" if up == 0 else str(max(Decimal(0), center - half))
            proportion.upper = "1" if up == n else str(min(Decimal(1), center + half))
        # Rank once: each omitted median uses the two surviving middle positions.
        # Do not sort/copy the entire cohort separately for every leave-one-out.
        ordered = sorted((value, index) for index, value in enumerate(values))
        ranks = {index: rank for rank, (_, index) in enumerate(ordered)}
        total = sum(values)
        for index, item in enumerate(samples):
            mean = (total - values[index]) / (n - 1) if n > 1 else None
            median = None
            if n > 1:
                low, high = (n - 2) // 2, (n - 1) // 2
                median = (ordered[low + (low >= ranks[index])][0]
                          + ordered[high + (high >= ranks[index])][0]) / 2
            points.append(LeaveOneOutPoint(omitted_key=item["key"], omitted_year=item["year"],
                omitted_return=str(values[index]), n=n - 1,
                mean=str(mean) if mean is not None else None,
                median=str(median) if median is not None else None,
                mean_change=str(mean - Decimal(stats["mean"])) if mean is not None else None))
        changes = [abs(Decimal(point.mean_change)) for point in points if point.mean_change is not None]
        max_change = max(changes) if changes else None
        sensitivity = LeaveOneOutSensitivity(available=n > 1, points=points,
            max_abs_mean_change=str(max_change) if max_change is not None else None,
            influential_sample_keys=[point.omitted_key for point in points
                if point.mean_change is not None and abs(Decimal(point.mean_change)) == max_change])
        for metric in ("mean", "median"):
            numbers = [Decimal(getattr(point, metric)) for point in points if getattr(point, metric) is not None]
            setattr(sensitivity, f"{metric}_min", str(min(numbers)) if numbers else None)
            setattr(sensitivity, f"{metric}_max", str(max(numbers)) if numbers else None)
    # Fix the boundary before eligible returns; missing years cannot move it.
    years = sorted(set(target_years or candidate_years))
    boundary = (years[0] + years[-1]) // 2 if years else None
    segment_rows = []
    for earlier in (True, False):
        selected = [item for item in samples if item["year"] is not None and boundary is not None
                    and (item["year"] <= boundary) == earlier]
        segment_rows.append(HistoricalSegment(label="较早历史" if earlier else "较近历史",
            target_years=[year for year in years if (year <= boundary) == earlier],
            sample_keys=[item["key"] for item in selected],
            statistics=statistics([item[value_key] for item in selected])))
    # Count every overlap with a sweep; retain a bounded detail preview. The full
    # sample dates remain available, so large repeated-event imports stay linear.
    overlaps, active, overlap_count = [], [], 0
    windows = sorted((item["start_date"], item["end_date"], item["key"]) for item in samples
                     if item.get("start_date") and item.get("end_date"))
    for start, end, key in windows:
        while active and active[0][0] < start:
            heappop(active)
        overlap_count += len(active)
        for other_end, other_key in active[:max(0, 200 - len(overlaps))]:
            overlaps.append(OverlappingSamples(first_key=other_key, second_key=key,
                                               start_date=start, end_date=min(end, other_end)))
        heappush(active, (end, key))
    year_counts = Counter(str(item["year"]) for item in samples if item["year"] is not None)
    warnings = ["历史样本有限；均值和比例需结合实际点、敏感性、缺口及研究选择过程解读。"]
    if n < 2:
        warnings.append("有效样本不足以比较去掉一个样本后的结果；不推断稳定性。")
    if overlaps:
        warnings.append("存在重叠价格窗口，事件不是独立重复试验；Wilson区间的独立性假设未经满足。")
    if any(count > 1 for count in year_counts.values()):
        warnings.append("同一年包含多个事件；事件样本N不等于独立年度N。")
    return DistributionRobustness(sample_unit=sample_unit, n=n, sample_keys=[item["key"] for item in samples],
        proportion=proportion, leave_one_out=sensitivity,
        historical_segments=HistoricalSegments(policy="target_year_midpoint" if target_years else "candidate_year_midpoint",
            boundary_year=boundary, earlier=segment_rows[0], later=segment_rows[1]),
        overlapping_pairs=overlaps, overlap_pair_count=overlap_count,
        overlap_details_truncated=overlap_count > len(overlaps),
        same_year_counts=dict(year_counts), warnings=warnings).model_dump()


def methodology(kind: str) -> dict:
    baseline = {
        "monthly": "完整月度从上月最后交易日收盘到本月最后有效收盘；当前年/同期进度另列。",
        "interval": "区间从窗口内首个交易日收盘到所示终点收盘；不包含首日开盘至收盘，和月度前收基准不同。",
        "earnings": "精确反应窗口从公告反应日的前一交易日收盘起算；盘前/盘后按已核对时点确定反应日，只有日期时只进入日期观察。",
        "event_dates": "日期观察D0保留交易所当地事件日，非交易日顺延；前5日D−6至D−1、当日D−1至D0、后5日D0至D+5、含当天D−1至D+5，均用收盘。",
    }[kind]
    scope = "每个ticker比较12个完整历史月份；月份排名、年份排除和反复选择窗口均属探索性筛选，最优月份不代表可靠信号。" if kind == "monthly" else "在所选ticker、年份及日期/事件窗口内描述历史；反复调整窗口和事后挑选事件会产生选择偏差，需先明确纳入规则。"
    return ResearchMethodology(baseline_rule=baseline, sample_unit="annual_sample" if kind in {"monthly", "interval"} else "event_sample", exploration_scope=scope).model_dump()


def _benchmark_prices(benchmark):
    prices = {}
    for bar in (benchmark or {}).get("bars", []):
        day = str(bar["date"])[:10]
        if day in prices:
            raise ValueError(f"Duplicate benchmark daily bar for {day}")
        value = Decimal(str(bar["close"])) if bar.get("close") is not None else None
        prices[day] = value if bar.get("status") == "VALID" and value is not None and value.is_finite() and value > 0 else None
    return prices


def _benchmark_return(prices, benchmark, start, end, calendar):
    if not start or not end or start > end:
        return None, "missing_window"
    if benchmark.get("cutoff_date") and end > benchmark["cutoff_date"]:
        return None, "not_yet_formed"
    if benchmark.get("status") != "available":
        return None, benchmark.get("status", "benchmark_missing")
    if benchmark.get("listing_date") and start < benchmark["listing_date"]:
        return None, "before_listing"
    expected = sessions(date.fromisoformat(start), date.fromisoformat(end), calendar)
    if not expected or any(prices.get(str(day)) is None for day in expected):
        return None, "benchmark_missing_prices"
    value = endpoint_change(prices.get(start), prices.get(end)).value
    return (str(value), "available") if value is not None else (None, "benchmark_missing_prices")


def add_distributions(result: dict, benchmark: dict | None = None) -> dict:
    """Pair each qualified sample before aggregation. Never remap a benchmark date.

    Stock summaries remain independent; comparison summaries use precisely the
    same sample IDs for stock, benchmark and their individual differences.
    """
    metadata = result["metadata"]
    benchmark = {**benchmark, "cutoff_date": metadata["cutoff_date"]} if benchmark else None
    prices = _benchmark_prices(benchmark)
    calendar = metadata["calendar"]
    kind = result["kind"]
    metadata["methodology"] = methodology(kind)
    groups = {}
    target = metadata.get("historical_years", metadata.get("requested_fiscal_years", []))
    def sample(row, metric, group, label, value, eligible, start, end, reason):
        key = f"{group}:{metric}"
        entries = groups.setdefault(key, {"label": label, "metric": metric, "group": group, "samples": []})
        reasons = row.get("exclusion_reasons", [])
        reason = next((r for r in reasons if r != "incomplete_full_window"), reason)
        if not eligible and reason == "available":
            reason = "not_historical_year" if row.get("group") != "historical" else "ineligible_sample"
        item = {"key": str(row.get("key", row.get("event_id", row.get("year")))), "year": row.get("year"),
                "group": row.get("group"), "start_date": start, "end_date": end, "stock": value,
                "label": row.get("label", str(row.get("year", ""))),
                "max_drawdown": row.get("max_drawdown") if metric == "endpoint" else row.get("windows", {}).get(metric, {}).get("max_drawdown"),
                "eligible": bool(eligible), "status": "available" if eligible else reason or "ineligible_sample",
                "reasons": list(dict.fromkeys([r for r in reasons if r != "incomplete_full_window"] + ([] if eligible else [reason or "ineligible_sample"])))}
        if benchmark:
            other, status = _benchmark_return(prices, benchmark, start, end, calendar)
            item.update(benchmark=other, benchmark_status=status, difference=None, paired=False)
            if value is not None and other is not None:
                item.update(difference=str(Decimal(value) - Decimal(other)), paired=bool(eligible))
            row.setdefault("benchmark_windows", {})[metric] = dict(item)
        entries["samples"].append(item)

    if kind in {"monthly", "interval"}:
        # Every monthly distribution uses a full calendar month. The focused
        # same-progress path remains in rows/series, never in the annual ranking.
        data = result["cells"] if kind == "monthly" else result["rows"]
        for row in data:
            group = str(row["month"]) if kind == "monthly" else "interval"
            eligible = row.get("eligible", row["group"] == "historical" and row["complete"] and row["period_ended"])
            label = f"{group}月 · 完整历史月" if kind == "monthly" else "所选区间"
            sample(row, "endpoint", group, label, row["endpoint"], eligible, row["baseline_date"], row["actual_end"], row["status"])
    else:
        data = [*result["rows"], *result.get("coverage_rows", [])] if kind == "event_dates" else result["cells"]
        for row in data:
            group = row.get("category", f"Q{row.get('quarter')}")
            windows = row.get("windows", {})
            # Missing fiscal events must still contribute a visible coverage gap.
            keys = list(windows) or (["1", "5", "20", "60"] if kind == "earnings" else ["before5", "day0", "after5", "through5"])
            for metric in keys:
                window = windows.get(metric, {})
                sample(row, metric, group, group, window.get("value", window.get("cumulative")), window.get("eligible", False),
                       window.get("start_date", row.get("baseline_date")), window.get("end_date"),
                       window.get("status", row.get("status", "missing_event")))
    output = []
    for key, group in groups.items():
        samples = group.pop("samples")
        historical = [item for item in samples if item["group"] == "historical"]
        valid = [item for item in historical if item["eligible"] and item["stock"] is not None]
        paired = [item for item in valid if item.get("paired")]
        missing = Counter(item["status"] for item in historical if not item["eligible"])
        if benchmark:
            missing.update(item["benchmark_status"] for item in valid if not item["paired"])
        output.append(Distribution(key=key, **group, stock=statistics([x["stock"] for x in valid]),
            paired_stock=statistics([x["stock"] for x in paired]) if benchmark else None,
            benchmark=statistics([x["benchmark"] for x in paired]) if benchmark else None,
            difference=statistics([x["difference"] for x in paired]) if benchmark else None,
            years=sorted({x["year"] for x in valid if x["year"] is not None}), target_years=target,
            missing=dict(missing), samples=samples,
            robustness=robustness(valid, target_years=target, candidate_years=[x["year"] for x in historical if x["year"] is not None],
                sample_unit="annual_sample" if kind in {"monthly", "interval"} else "event_sample"),
            paired_difference_robustness=robustness(paired, target_years=target,
                candidate_years=[x["year"] for x in historical if x["year"] is not None],
                sample_unit="paired_sample", value_key="difference") if benchmark else None,
            closing_max_drawdown=statistics([x["max_drawdown"] for x in valid]),
            empty_reason=None if valid else "没有同时满足历史归属、事实核对和完整价格窗口的样本；请查看覆盖缺口。").model_dump())
    result["distributions"] = output
    if kind == "monthly":
        result["monthly_rankings"] = monthly_rankings(output)
    if kind == "earnings" or metadata.get("research_kind") == "earnings" or any(row.get("fiscal_year") is not None for row in result["rows"]):
        quarters = metadata.get("requested_fiscal_quarters") or [1, 2, 3, 4]
        result["fiscal_coverage"] = fiscal_coverage(output, target, metadata.get("current_fiscal_year", metadata.get("current_year")), quarters=quarters)
        observation = result.get("date_observation") or result
        calendar_rows = [*observation["rows"], *observation.get("coverage_rows", [])]
        result["fiscal_coverage"]["quarter_calendar"] = quarter_calendar(calendar_rows, target, metadata["cutoff_date"], quarters=quarters)
    if benchmark:
        result["benchmark"] = {k: v for k, v in benchmark.items() if k != "bars"}
        rowmap = {str(r.get("key", r.get("event_id", r.get("year")))): r for r in result["rows"]}
        for series in [*result["series"], *result.get("window_series", [])]:
            row = rowmap.get(series["key"])
            baseline = series.get("baseline_date") or ((row.get("baseline_date") or row.get("anchor", {}).get("baseline_date")) if row else None)
            for point in series["points"]:
                day = point.get("date")
                value, status = _benchmark_return(prices, benchmark, min(baseline, day), max(baseline, day), calendar) if baseline and day else (None, "missing_window")
                # Paths before the baseline must use the baseline denominator.
                if value is not None:
                    value = str(endpoint_change(prices[baseline], prices[day]).value)
                point["benchmark"] = value
                point["difference"] = str(Decimal(point["value"]) - Decimal(value)) if point.get("value") is not None and value is not None else None
                point["benchmark_status"] = status
    return result


def fiscal_coverage(distributions, target, current, *, quarters=(1, 2, 3, 4)):
    rankings, gaps = [], []
    metrics = sorted({item["metric"] for item in distributions}) or ["after5"]
    for metric in metrics:
        entries = []
        for quarter in (f"Q{value}" for value in quarters):
            item = next((x for x in distributions if x["group"] == quarter and x["metric"] == metric), None)
            item = item or {"stock": statistics([]), "years": [], "samples": []}
            entries.append({"quarter": quarter, "window": metric, "statistics": item["stock"], "valid_years": item["years"], "target_years": target})
            for year in target:
                samples = [s for s in item["samples"] if s["year"] == year]
                reasons = list(dict.fromkeys(reason for s in samples if not s["eligible"]
                                             for reason in s.get("reasons", [s["status"]])))
                gaps.append({"year": year, "quarter": quarter, "window": metric,
                             "status": "available" if any(s["eligible"] for s in samples) else reasons[0] if reasons else "missing_event",
                             "reasons": reasons or ([] if samples else ["missing_event"]),
                             "event_keys": [s["key"] for s in samples]})
        for statistic in ("median", "mean"):
            valid = sorted({Decimal(x["statistics"][statistic]) for x in entries if x["statistics"][statistic] is not None}, reverse=True)
            for item in entries:
                value = item["statistics"][statistic]
                item[f"{statistic}_rank"] = 1 + valid.index(Decimal(value)) if value is not None else None
                item[f"{statistic}_reverse_rank"] = len(valid) - valid.index(Decimal(value)) if value is not None else None
        rankings.extend(entries)
    return {"target_years": target, "current_fiscal_year": current, "rankings": rankings, "gaps": gaps,
            "definition": "财报日期附近股价表现；标准财季、已核对实际发布、完整历史窗口；当前财年另列。",
            "target_reason": None if target else "当前财年归属或研究年份未确定，不能将观察年数称为目标覆盖。"}


def qualify_common_quarters(rows, enabled, *, quarters=(1, 2, 3, 4)):
    """Optional common-year cohort, separately per window; never fill a gap."""
    requested = tuple(f"Q{quarter}" for quarter in quarters)
    if not enabled or not requested or not any(
        row.get("category", f"Q{row.get('quarter')}") in requested for row in rows
    ):
        return
    for metric in {key for row in rows for key in row.get("windows", {})}:
        qualified = {q: {row["year"] for row in rows if row.get("group") == "historical"
                        and row.get("category", f"Q{row.get('quarter')}") == q
                        and row.get("windows", {}).get(metric, {}).get("eligible")} for q in requested}
        common = set.intersection(*qualified.values())
        for row in rows:
            window = row.get("windows", {}).get(metric)
            if window and window.get("eligible") and row.get("year") not in common:
                window.update(eligible=False, status="not_common_fiscal_year")
    for x in {p["x"] for row in rows for p in row.get("points", [])}:
        for flag in ("eligible", "daily_eligible"):
            qualified = {q: {row["year"] for row in rows if row.get("category") == q and any(p["x"] == x and p.get(flag) for p in row.get("points", []))} for q in requested}
            common = set.intersection(*qualified.values())
            for row in rows:
                for point in row.get("points", []):
                    if point["x"] == x and row.get("year") not in common:
                        point[flag] = False
