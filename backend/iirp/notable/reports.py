"""Publication groups for the timeline. Filters select rows; pagination selects reports.

The heading counts all rows of the report within the person scope, including assets
hidden by row filters. Preview rows are the three largest matching stock/option
trades, using the filed amount or the lower end of the disclosed range. Unknown
amounts sort last; ties keep the source row order. Expanding exposes every match.
"""

from collections import Counter, defaultdict
from decimal import Decimal

from sqlalchemy import func, literal, select, tuple_

from iirp.messages import UserError
from iirp.notable.people import keys
from iirp.notable.trades import _decode, _encode, _hydrate, _union


def reports(s, *, scope="watched", person=None, source=None, direction=None, ticker=None,
            published_from=None, published_to=None, assets="equities", limit=5, cursor=""):
    if published_from and published_to and published_from > published_to:
        raise UserError("notable.date_range_invalid")
    filters = dict(scope=scope, person=person, source=source, direction=direction, ticker=ticker,
                   issuer_id=None, published_from=published_from, published_to=published_to, assets=assets)
    union = _union(s, **filters)
    if union is None:
        return {"items": [], "total": 0, "next_cursor": None}
    ciks, rows = union
    groups = (select(rows.c.source, rows.c.published, rows.c.rank, rows.c.group_id.label("tie"))
              .distinct().subquery())
    total = s.scalar(select(func.count()).select_from(groups))
    page = select(groups).order_by(groups.c.published.desc(), groups.c.rank.desc(), groups.c.tie.desc())
    if cursor:
        published, rank, tie = _decode(cursor)
        page = page.where(tuple_(groups.c.published, groups.c.rank, groups.c.tie)
                          < tuple_(literal(published), literal(rank), literal(tie)))
    found = s.execute(page.limit(limit + 1)).all()
    next_cursor = _encode(*found[limit - 1][1:4]) if len(found) > limit else None
    ids = [row.tie for row in found[:limit]]
    if not ids:
        return {"items": [], "total": total, "next_cursor": None}
    selected = s.execute(select(rows).where(rows.c.group_id.in_(ids))
                         .order_by(rows.c.tie.desc())).all()
    matches = defaultdict(list)
    followed = keys(s)
    for ref, item in zip(selected, _hydrate(s, selected, ciks, followed), strict=True):
        matches[ref.group_id].append(item)
    # Summary counts include rows excluded by ticker or buy/sell filters.
    _, full = _union(s, **{**filters, "direction": None, "ticker": None, "assets": "all"})
    selected = s.execute(select(full).where(full.c.group_id.in_(ids)).order_by(full.c.tie.desc())).all()
    complete = defaultdict(list)
    for ref, item in zip(selected, _hydrate(s, selected, ciks, followed), strict=True):
        complete[ref.group_id].append(item)
    items = []
    for row in found[:limit]:
        transactions = matches[row.tie]
        head = transactions[0]
        all_rows = complete[row.tie]
        counts = Counter(item["asset_kind"] for item in all_rows)
        equities = [item for item in all_rows if item["asset_kind"] in ("stock", "option")]
        sides = Counter(item["side"] for item in equities)
        preview = [item for item in transactions if item["asset_kind"] in ("stock", "option")] or transactions
        ranked = sorted(preview, key=lambda item: (
            item["asset_kind"] not in ("stock", "option"),
            item["amount"] is None and item["amount_low"] is None,
            -Decimal(item["amount"] or item["amount_low"] or "0")))
        items.append({"id": row.tie, "source": head["source"], "published": head["published"],
                      "published_at": head["published_at"], "person": head["person"], "others": head["others"],
                      "url": head["url"], "amendment": head["amendment"],
                      "counts": dict(counts), "equities": len(equities), "sides": dict(sides),
                      "preview_ids": [item["id"] for item in ranked[:3]], "trades": transactions})
    return {"items": items, "total": total, "next_cursor": next_cursor}
