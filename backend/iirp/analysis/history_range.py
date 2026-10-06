"""User price targets and calculation/download bounds are distinct frozen facts."""

from datetime import date

from iirp.analysis.calendar import ET, last_completed_session


def price_range(params, as_of, *, collection=None):
    today = as_of.astimezone(ET).date()
    completed = last_completed_session(as_of=as_of)
    research = params.get("analysis_params")
    years = params.get("historical_years", 8)
    if research and research["kind"] in {"monthly", "interval"}:
        from iirp.analysis.research import _selected_years, plan_scope

        basis = "research_conditions"
        if research["kind"] == "monthly":
            current = research.get("current_year") or today.year
            selected, _ = _selected_years(research, current)
            start = date(min([*selected, current]), 1, 1)
            end = min(today, date(current, 12, 31))
        else:
            start, end = plan_scope(research, today=today)
        note = "按保存的研究年份与窗口获取；计算前收缓冲独立于目标范围。"
    elif params.get("start_date"):
        basis = "explicit_dates"
        start, end = (
            date.fromisoformat(params["start_date"]),
            date.fromisoformat(params["end_date"]),
        )
        note = "按明确选择的日期获取；尚未收盘、上市/成立前和供应商缺口不补为零。"
    else:
        basis = "complete_natural_years"
        if type(years) is not int or years < 1 or today.year - years < 2:
            raise ValueError("历史年数超出日期可表达范围；请选择合法起点")
        start, end = date(today.year - years, 1, 1), today
        note = f"过去 {years} 个完整自然年（{today.year - years}—{today.year - 1}）加 {today.year} 年至查询日；财报财政年度另按公司实际财年。"
    if start > end:
        raise ValueError("目标开始日期不能晚于截止日期")
    download_start, download_end = collection or (
        start,
        end if basis == "explicit_dates" else min(end, completed),
    )
    return {
        "target_start_date": start.isoformat(),
        "target_end_date": end.isoformat(),
        "collection_start_date": download_start.isoformat(),
        "collection_end_date": download_end.isoformat(),
        "completed_through": completed.isoformat(),
        "basis": basis,
        "historical_years": years,
        "explanation": note,
        "buffer_explanation": "额外获取目标起点之前的收盘，作为涨跌幅计算基准；不计为额外历史年。"
        if download_start < start
        else None,
    }
