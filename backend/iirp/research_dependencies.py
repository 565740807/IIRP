"""Price dependencies of a research result: the dates it can use and its cache.

A price cache never changes during its 24 hours, so its id identifies the
prices; a refetch is a new cache and therefore a new input.
"""
from datetime import date

from sqlalchemy import func, or_, select

from iirp.analytics.calendar import previous_session, reaction_session, session_window
from iirp.business_models import PriceCache, PriceCacheBar


def normalize_ranges(ranges):
    output = []
    for first, last in sorted(set(ranges)):
        if first > last:
            continue
        if output and first <= output[-1][1]:
            output[-1] = (output[-1][0], max(last, output[-1][1]))
        else:
            output.append((first, last))
    return output


def range_predicate(column, ranges):
    return or_(*(column.between(first, last) for first, last in normalize_ranges(ranges))) if ranges else False


def research_ranges(params, calendar='XNYS', events=()):
    from iirp.analytics.research import _interval_rules, _mapped_day, _selected_years

    values = {k: v for k, v in params.items() if v is not None}
    cutoff = date.fromisoformat(values['cutoff_date'])
    kind = values.get('kind', 'monthly')
    ranges = []
    if kind == 'monthly':
        current = values.get('current_year', cutoff.year)
        years, _ = _selected_years(values, current)
        for year in [*years, current]:
            ranges.append((previous_session(date(year, 1, 1), calendar), min(cutoff, date(year, 12, 31))))
    elif kind == 'interval':
        first, last, cross, current = _interval_rules(values, cutoff)
        years, _ = _selected_years(values, current)
        for year in [*years, current]:
            ranges.append((_mapped_day(year, first), min(cutoff, _mapped_day(year + int(cross), last))))
    elif kind == 'earnings':
        # The fiscal matrix and date observations can use every selected quarter,
        # including its independent 1/5/20/60-day maturity and pre-event path.
        current = values.get('current_fiscal_year')
        years, _ = _selected_years(values, current)
        allowed = {*years, current}
        for event in events:
            item = event if isinstance(event, dict) else {
                key: getattr(event, key) for key in ('announced_at', 'announced_date', 'time_precision', 'fiscal_year')
            }
            if current and item.get('fiscal_year') is not None and item['fiscal_year'] not in allowed:
                continue
            anchor = reaction_session(item.get('announced_at'), announced_date=item.get('announced_date'), time_precision=item.get('time_precision', 'date_only'), calendar=calendar)
            if anchor['baseline_date']:
                days = session_window(date.fromisoformat(anchor['baseline_date']), 20, 60, calendar)
                ranges.append((days[0], min(cutoff, days[-1])))
    return normalize_ranges(ranges)


def dataset_dependency(s, dataset, ranges):
    """``dataset`` is the PriceCache the result reads."""
    if dataset is None:
        return {'cache_id': None, 'basis': None, 'range_count': len(ranges), 'row_count': 0}
    ranges = normalize_ranges(ranges)
    rows = s.scalar(select(func.count()).select_from(PriceCacheBar).where(
        PriceCacheBar.cache_id == dataset.id, range_predicate(PriceCacheBar.session_date, ranges)))
    return {
        'cache_id': dataset.id,
        'fetched_at': dataset.fetched_at.isoformat(),
        'basis': 'SPLIT_ONLY',
        'range_count': len(ranges),
        'row_count': rows,
    }


def benchmark_dependency(s, snapshot, ranges):
    if not snapshot:
        return None
    dataset = s.get(PriceCache, snapshot['dataset_id']) if snapshot.get('dataset_id') else None
    return {k: v for k, v in snapshot.items() if k != 'dataset_id'} | {'price_dependency': dataset_dependency(s, dataset, ranges)}
