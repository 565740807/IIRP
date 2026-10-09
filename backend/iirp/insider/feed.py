"""Feed reading sessions (watermarks) and their pages and group details."""

import copy
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from iirp.insider.common import VISIBLE
from iirp.insider.views import (
    _compact_row,
    _matches,
    _ordered_rows,
    _range_summary,
    _summary,
    _ticker_read_view,
    _trader_groups,
    _trader_key,
    _with_filing_owners,
)
from iirp.messages import UserError, msg
from iirp.models import (
    FeedRevision,
    FeedSession,
    Filing,
    TransactionEvent,
    now,
)


def _offset(cursor: str) -> int:
    if not cursor:
        return 0
    if not cursor.isascii() or not cursor.isdigit():
        raise UserError("common.cursor_invalid")
    return int(cursor)


def _session(s: Session, session_id: str, purpose: str) -> FeedSession:
    saved = s.get(FeedSession, session_id)
    if (saved is None or saved.expires_at < now() or saved.filters.get("purpose") != purpose
            or "watermark" not in saved.filters):
        raise UserError("insider.snapshot.expired")
    return saved


def _canonical_kind(kind):
    _matches({}, kind)
    return {
        "全部": "all",
        "": "all",
        "重点": "focus",
        "purchase": "buy",
        "P": "buy",
        "sale": "sell",
        "S": "sell",
        "derivatives": "derivative",
        "option": "derivative",
    }.get(kind, kind)


def open_feed_session(s: Session, kind: str, order: str, **extra) -> FeedSession:
    """A reading session is a watermark plus filters; no revision list is stored."""
    from iirp.insider.feed_index import count, new_watermark

    watermark = new_watermark(s)
    saved = FeedSession(
        revision_ids=[],
        filters={"purpose": "feed", "kind": kind, "order": order, "as_of": now().isoformat(),
                 "watermark": watermark, "total_groups": count(s, watermark, _canonical_kind(kind), order, index=extra.get("index", "all")),
                 **extra},
        expires_at=now() + timedelta(hours=12),
    )
    s.add(saved)
    s.flush()
    return saved


def feed_watermark(saved: FeedSession) -> dict:
    return saved.filters["watermark"]


def feed_groups(s: Session, page_ids: list[str], kind: str, order: str) -> list[dict]:
    """Render only requested immutable revisions, shared by pages and deltas."""
    groups = []
    revisions_by_id = (
        {
            revision.id: revision
            for revision in s.scalars(select(FeedRevision).where(FeedRevision.id.in_(page_ids)))
        }
        if page_ids
        else {}
    )
    # One relation read for the complete page; immutable filing-time roles.
    enriched = {revision.id: copy.deepcopy(revision.data["transactions"]) for revision in revisions_by_id.values()}
    _with_filing_owners(s, [row for rows in enriched.values() for row in rows])
    for revision_id in page_ids:
        revision = revisions_by_id[revision_id]
        data = _ticker_read_view(copy.deepcopy(revision.data))
        filtered = _ordered_rows([row for row in enriched[revision_id] if _matches(row, kind)], order)
        data.update(
            {
                "revision_id": revision.id,
                "revision_created_at": revision.created_at.isoformat(),
                "transactions": [_compact_row(row) for row in filtered[:20]],
                "matching_transactions": len(filtered),
                "amendment_count": sum(bool(row.get("is_amendment_update") or "/A" in (row.get("form") or "")) for row in filtered),
                "summary": _summary(filtered),
                **_range_summary(filtered),
                "trader_groups": _trader_groups(filtered)[:20],
                "next_trader_cursor": "g:20" if len(_trader_groups(filtered)) > 20 else None,
                "next_cursor": "20" if len(filtered) > 20 else None,
            }
        )
        groups.append(data)
    return groups


def feed(s: Session, session_id: str = "", cursor: str = "", kind: str = "all", order="transaction", index="all") -> dict:
    """Return twenty company/date groups of one reading session; session creation is local only."""
    from iirp.insider.feed_index import PAGE_SIZE, decode_cursor, encode_cursor, listing

    _matches({}, kind)
    if order not in {"transaction", "accepted"}:
        raise UserError("feed.order_invalid")
    if session_id:
        saved = _session(s, session_id, "feed")
        if saved.filters.get("kind") != kind or saved.filters.get("order") != order or saved.filters.get("index", "all") != index:
            raise UserError("feed.filters_changed")
    else:
        saved = open_feed_session(s, kind, order, index=index)
    entries = listing(s, feed_watermark(saved), _canonical_kind(kind), order,
                      after=decode_cursor(cursor) if cursor else None, index=index)
    page = entries[:PAGE_SIZE]
    page_ids, total = [entry[2] for entry in page], saved.filters["total_groups"]
    next_cursor = encode_cursor(page[-1][0]) if len(entries) > PAGE_SIZE else None
    groups = feed_groups(s, page_ids, kind, order)
    pending = (
        s.scalar(select(func.count()).select_from(Filing).where(Filing.current_version.is_(None), Filing.visible.is_(True)))
        or 0
    )
    untimed = (
        s.scalar(
            select(func.count())
            .select_from(TransactionEvent)
            .where(TransactionEvent.accepted_at.is_(None), TransactionEvent.status.in_(VISIBLE))
        )
        or 0
    )
    return {
        "groups": groups,
        "order": order,
        "session_id": saved.id,
        "as_of": saved.filters["as_of"],
        "next_cursor": next_cursor,
        "total_groups": total,
        "data_status": "AVAILABLE"
        if total
        else "NOT_FETCHED"
        if pending == 0
        else "PARTIAL",
        "coverage": {
            "status": "LOCAL_OBSERVATIONS",
            "pending_filings": pending,
            "missing_acceptance_rows": untimed,
            "message": msg("feed.scope"),
        },
    }


def feed_group(
    s: Session, session_id: str, group_id: str, cursor: str = "", limit: int = 20, trader_key=""
) -> dict:
    saved, offset = _session(s, session_id, "feed"), _offset(cursor.removeprefix("g:"))
    if not 1 <= limit <= 20:
        raise UserError("feed.page_too_large", max=20)
    from iirp.insider.feed_index import group_revision, in_listing

    row = group_revision(s, feed_watermark(saved), group_id)
    kind, order = _canonical_kind(saved.filters["kind"]), saved.filters["order"]
    revision = s.get(FeedRevision, row.id) if row is not None and in_listing(row, kind, order) else None
    if revision is None:
        raise UserError("feed.group_missing")
    rows = _ordered_rows([row for row in revision.data["transactions"] if _matches(row, saved.filters["kind"])], saved.filters.get("order", "accepted"))
    if trader_key:
        rows = [row for row in rows if _trader_key(row) == trader_key]
    if cursor.startswith("g:"):
        groups = _trader_groups(_with_filing_owners(s, copy.deepcopy(rows)))
        return {"items": groups[offset:offset + limit], "revision_id": revision.id, "total": len(groups),
                "next_cursor": f"g:{offset + limit}" if offset + limit < len(groups) else None}
    return {
        "items": [_compact_row(row) for row in _with_filing_owners(s, copy.deepcopy(rows[offset : offset + limit]))],
        "revision_id": revision.id,
        "total": len(rows),
        "next_cursor": str(offset + limit) if offset + limit < len(rows) else None,
    }
