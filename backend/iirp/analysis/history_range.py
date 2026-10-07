"""User price targets and calculation/download bounds are distinct frozen facts."""

from datetime import date

from iirp.analysis.calendar import ET, last_completed_session
from iirp.messages import UserError, msg


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
        note = msg("price_range.research_conditions")
    elif params.get("start_date"):
        basis = "explicit_dates"
        start, end = (
            date.fromisoformat(params["start_date"]),
            date.fromisoformat(params["end_date"]),
        )
        note = msg("price_range.explicit_dates")
    else:
        basis = "complete_natural_years"
        if type(years) is not int or years < 1 or today.year - years < 2:
            raise UserError("price_range.years_out_of_range")
        start, end = date(today.year - years, 1, 1), today
        note = msg("price_range.complete_years", years=years, first=today.year - years,
                   last=today.year - 1, current=today.year)
    if start > end:
        raise UserError("price_range.start_after_end")
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
        "buffer_explanation": msg("price_range.buffer")
        if download_start < start
        else None,
    }
