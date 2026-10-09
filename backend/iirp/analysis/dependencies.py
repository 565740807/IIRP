"""Price dependencies of a research result: the dates it can use and its cache.

A price cache never changes during its 24 hours, so its id identifies the
prices; a refetch is a new cache and therefore a new input.
"""
from datetime import date

from sqlalchemy import func, or_, select

from iirp.models import PriceCache, PriceCacheBar


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


def research_ranges(params, calendar='XNYS'):
    from iirp.analysis.research import period_ranges

    values = {k: v for k, v in params.items() if v is not None}
    cutoff = date.fromisoformat(values['cutoff_date'])
    return normalize_ranges([(first, min(cutoff, last)) for first, last in period_ranges(values, cutoff)])


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
