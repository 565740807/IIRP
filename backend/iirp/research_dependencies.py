"""Immutable price dependencies bounded to the dates a research result can use.

Dataset IDs remain provenance. Revisions outside these ranges do not change
an effective input; a changed adjustment basis or split manifest always does.
"""
from datetime import date

from sqlalchemy import or_, select

from iirp.analytics.calendar import previous_session, reaction_session, session_window
from iirp.business_models import DatasetBar, MarketBar, PriceDataset
from iirp.market_data import digest


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
    if dataset is None:
        return {'fingerprint': None, 'basis': None, 'range_count': len(ranges), 'row_count': 0}
    ranges = normalize_ranges(ranges)
    # adj_close includes dividend adjustment and can drift in Yahoo rounding.
    # It is not an input to our split-only calculations. Keep open because
    # exact-time earnings use opening gaps; retain complete original revisions.
    # Equal prices alone cannot establish equivalent source evidence. Keep the
    # per-bar source identity; unchanged bars retain their original source when
    # an unrelated date is revised, so outside-range revisions still reuse.
    rows = s.execute(
        select(DatasetBar.session_date, MarketBar.open, MarketBar.close, MarketBar.status, MarketBar.reason,
               MarketBar.provider, MarketBar.source_hash)
        .join(MarketBar, MarketBar.id == DatasetBar.bar_id)
        .where(DatasetBar.dataset_id == dataset.id, range_predicate(DatasetBar.session_date, ranges))
        .order_by(DatasetBar.session_date)
    ).all()
    return {
        'fingerprint': digest(['research-price-dependencies-v3-effective-source', dataset.security_id, dataset.basis, dataset.basis_key, dataset.manifest.get('verified_splits'), [(str(a), str(b)) for a, b in ranges], [(str(row.session_date), row.open, row.close, row.status, row.reason, row.provider, row.source_hash) for row in rows]]),
        'basis': dataset.basis,
        'range_count': len(ranges),
        'row_count': len(rows),
    }


def benchmark_dependency(s, snapshot, ranges):
    if not snapshot:
        return None
    dataset = s.get(PriceDataset, snapshot['dataset_id']) if snapshot.get('dataset_id') else None
    return {k: v for k, v in snapshot.items() if k != 'dataset_id'} | {'price_dependency': dataset_dependency(s, dataset, ranges)}
