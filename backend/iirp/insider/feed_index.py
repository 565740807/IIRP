"""Current revision pointers, sort keys and reading watermarks for the Insider feed.

A reading session stores only a watermark (the revision sequence value and a
PostgreSQL snapshot taken when it opened) plus its filters. A revision belongs
to the session when its sequence is at or below the watermark and its writing
transaction is visible in that snapshot. The set never changes afterwards, so
keyset pages stay stable while new filings are published; anything newer is
only counted as an update. Revisions written before this schema carry NULLs
and are visible to every session.

``feed_group_order`` holds the sort keys of each group's current revision, one
row per filter and order, so a page is a single bounded index range scan.
"""

import base64
import json
from datetime import date, datetime, timezone

from sqlalchemy import String, and_, cast, delete, func, or_, select, text, tuple_, update, values
from sqlalchemy import column as sql_column
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.types import DateTime

from iirp.models import (
    XID8,
    FeedGroupCurrent,
    FeedGroupOrder,
    FeedRevision,
    FeedSession,
    FeedWatermarkCluster,
    PGSnapshot,
)

ORDERS = ("transaction", "accepted")
PAGE_SIZE = 20
# Sorts below every real date; the same sentinel is written by migrations 0021 and 0022.
SORT_MISSING = datetime(1, 1, 1, tzinfo=timezone.utc)
CURSOR_PREFIX = "k1."


def sort_value(value, order):
    """A stored revision sort date ('YYYY-MM-DD' or ISO instant) as a sortable instant."""
    if not value:
        return SORT_MISSING
    if order == "transaction":
        day = date.fromisoformat(str(value)[:10])
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def revision_sort_keys(match_kinds, sort_dates, row_count):
    """{(kind, order): sort key} of a non-empty revision; mirrors migrations 0021 and 0022."""
    if not row_count:
        return {}
    sort_dates = sort_dates or {}
    return {
        (kind, order): sort_value(sort_dates.get(kind if order == "transaction" else "accepted:" + kind), order)
        for kind in match_kinds or []
        for order in ORDERS
    }


# --- watermarks ---------------------------------------------------------------


def new_watermark(s):
    """Sequence value, snapshot and (if any) the opening transaction's own id.

    A snapshot never shows its own transaction as committed, yet revisions that
    transaction wrote before opening the session belong to it; ``own`` covers
    them, and anything it writes later has a higher sequence value.
    """
    snapshot, seq, own = s.execute(text(
        "SELECT pg_current_snapshot()::text, "
        "(SELECT CASE WHEN is_called THEN last_value ELSE 0 END FROM feed_revision_seq), "
        "pg_current_xact_id_if_assigned()::text"
    )).one()
    return {"seq": int(seq), "snapshot": snapshot, **({"own": own} if own else {})}


def _snapshot(watermark):
    return cast(watermark["snapshot"], PGSnapshot())


def _xid_visible(column, watermark):
    terms = [column.is_(None), func.pg_visible_in_snapshot(column, _snapshot(watermark))]
    if watermark.get("own"):
        terms.append(column == cast(watermark["own"], XID8()))
    return or_(*terms)


def _xid_after(column, watermark):
    """Written by a transaction still running (or not begun) at the snapshot."""
    snapshot = _snapshot(watermark)
    terms = [column >= func.pg_snapshot_xmin(snapshot), ~func.pg_visible_in_snapshot(column, snapshot)]
    if watermark.get("own"):
        terms.append(column != cast(watermark["own"], XID8()))
    return and_(*terms)


def visible(table, watermark):
    """SQL predicate: this revision (or index row of one) belongs to the watermark."""
    return and_(or_(table.seq.is_(None), table.seq <= watermark["seq"]),
                _xid_visible(table.xid, watermark))


def changed_groups(s, watermark):
    """Groups whose current revision is newer than the watermark (usually a handful)."""
    current = FeedGroupCurrent
    return set(s.scalars(select(current.group_key).where(or_(
        current.seq > watermark["seq"], _xid_after(current.xid, watermark)))))


def asof_revisions(s, watermark, group_keys):
    """The newest revision of each group that belongs to the watermark."""
    if not group_keys:
        return {}
    revision = FeedRevision
    rows = s.execute(
        select(revision.id, revision.group_key, revision.accepted_at, revision.match_kinds,
               revision.row_count, revision.transaction_sort_dates)
        .where(revision.group_key.in_(sorted(group_keys)), visible(revision, watermark))
        .distinct(revision.group_key)
        .order_by(revision.group_key, revision.seq.desc().nulls_last(),
                  revision.created_at.desc(), revision.id.desc())
    )
    return {row.group_key: row for row in rows}


def _asof_key(row, kind, order):
    keys = revision_sort_keys(row.match_kinds, row.transaction_sort_dates, row.row_count)
    if (kind, order) not in keys:
        return None
    return (keys[(kind, order)], row.accepted_at, row.group_key)


def in_listing(row, kind, order):
    return _asof_key(row, kind, order) is not None


def listing(s, watermark, kind, order, *, after=None, limit=PAGE_SIZE):
    """Ordered (key, group_key, revision_id) entries of the watermark after ``after``.

    Returns up to ``limit + 1`` entries so the caller can tell whether more
    exist; ``limit=None`` returns the complete ordered listing.
    """
    o = FeedGroupOrder
    query = (
        select(o.sort_key, o.accepted_at, o.group_key, o.revision_id)
        .where(o.kind == kind, o.sort_order == order, visible(o, watermark))
        .order_by(o.sort_key.desc(), o.accepted_at.desc(), o.group_key.desc())
    )
    if after is not None:
        query = query.where(tuple_(o.sort_key, o.accepted_at, o.group_key) < tuple_(*after))
    if limit is not None:
        query = query.limit(limit + 1)
    entries = [((row.sort_key, row.accepted_at, row.group_key), row.group_key, row.revision_id)
               for row in s.execute(query)]
    changed = changed_groups(s, watermark)
    if changed:
        entries = [entry for entry in entries if entry[1] not in changed]
        for group_key, row in asof_revisions(s, watermark, changed).items():
            key = _asof_key(row, kind, order)
            if key is not None and (after is None or key < after):
                entries.append((key, group_key, row.id))
        entries.sort(key=lambda entry: entry[0], reverse=True)
    return entries if limit is None else entries[: limit + 1]


def count(s, watermark, kind, order):
    o = FeedGroupOrder
    total = s.scalar(select(func.count()).select_from(o).where(
        o.kind == kind, o.sort_order == order, visible(o, watermark)))
    changed = changed_groups(s, watermark)
    return total + sum(
        _asof_key(row, kind, order) is not None
        for row in asof_revisions(s, watermark, changed).values()
    )


def group_revision(s, watermark, group_key):
    """The revision of one group as the watermark sees it, or None."""
    return asof_revisions(s, watermark, {group_key}).get(group_key)


def _current_revisions(s, group_keys):
    if not group_keys:
        return {}
    revision, current = FeedRevision, FeedGroupCurrent
    rows = s.execute(
        select(revision.id, revision.group_key, revision.accepted_at, revision.match_kinds,
               revision.row_count, revision.transaction_sort_dates)
        .join(current, current.revision_id == revision.id)
        .where(current.group_key.in_(sorted(group_keys)))
    )
    return {row.group_key: row for row in rows}


def pending(s, base, kind, order):
    """Changes between a watermark and the current pointers (an update check)."""
    candidates = changed_groups(s, base)
    return _compare(candidates, asof_revisions(s, base, candidates),
                    _current_revisions(s, candidates), kind, order)


def delta(s, base, target, kind, order):
    """Groups changed between two watermarks, ordered by the target's sort keys.

    Deterministic for a fixed pair, so offset pages over it never shift.
    Returns (changed entries [(key, group_key, revision_id)], removed group keys).
    """
    revision = FeedRevision
    candidates = set(s.scalars(select(revision.group_key).where(
        or_(revision.seq > base["seq"], _xid_after(revision.xid, base)),
        visible(revision, target),
    )))
    return _compare(candidates, asof_revisions(s, base, candidates),
                    asof_revisions(s, target, candidates), kind, order)


def _compare(candidates, before, after, kind, order):
    changed, removed = [], []
    for group_key in candidates:
        old, new = before.get(group_key), after.get(group_key)
        old_key = _asof_key(old, kind, order) if old else None
        new_key = _asof_key(new, kind, order) if new else None
        if new_key is not None and (old_key is None or old.id != new.id):
            changed.append((new_key, group_key, new.id))
        elif old_key is not None and new_key is None:
            removed.append(group_key)
    changed.sort(key=lambda entry: entry[0], reverse=True)
    return changed, sorted(removed)


def encode_cursor(key):
    sort_key, accepted_at, group_key = key
    raw = json.dumps([sort_key.isoformat(), accepted_at.isoformat(), group_key], separators=(",", ":"))
    return CURSOR_PREFIX + base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor):
    if not cursor.startswith(CURSOR_PREFIX) or len(cursor) > 300:
        raise ValueError("分页游标无效。")
    body = cursor[len(CURSOR_PREFIX):]
    try:
        sort_key, accepted_at, group_key = json.loads(
            base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        key = (datetime.fromisoformat(sort_key), datetime.fromisoformat(accepted_at), str(group_key))
    except (ValueError, TypeError):
        raise ValueError("分页游标无效。") from None
    if key[0].tzinfo is None or key[1].tzinfo is None:
        raise ValueError("分页游标无效。")
    return key


# --- maintenance of pointers and sort keys -------------------------------------


def publish_current(s, revision_ids):
    """Point each group at its newest revision and rewrite its sort keys.

    Called as the last step of a publication so the per-group row lock is held
    only until commit. A concurrent older revision never replaces a newer one.
    """
    revision, current = FeedRevision, FeedGroupCurrent
    rows = s.execute(
        select(revision.id, revision.group_key, revision.match_kinds, revision.row_count,
               revision.transaction_sort_dates)
        .where(revision.id.in_(list(revision_ids)))
        .order_by(revision.group_key, revision.seq)
    ).all()
    for row in rows:
        statement = insert(current).from_select(
            ["group_key", "revision_id", "issuer_id", "accepted_at", "row_count", "seq", "xid"],
            select(revision.group_key, revision.id, revision.issuer_id, revision.accepted_at,
                   revision.row_count, revision.seq, revision.xid).where(revision.id == row.id),
        )
        statement = statement.on_conflict_do_update(
            index_elements=[current.group_key],
            set_={key: statement.excluded[key]
                  for key in ("revision_id", "issuer_id", "accepted_at", "row_count", "seq", "xid")},
            where=or_(current.seq.is_(None), current.seq < statement.excluded.seq),
        ).returning(current.group_key)
        if s.execute(statement).first() is None:
            continue
        _write_order_rows(s, row)


def _write_order_rows(s, row):
    revision, order = FeedRevision, FeedGroupOrder
    s.execute(delete(order).where(order.group_key == row.group_key))
    keys = revision_sort_keys(row.match_kinds, row.transaction_sort_dates, row.row_count)
    if not keys:
        return  # A tombstone keeps its pointer but leaves every listing.
    table = values(
        sql_column("kind", String), sql_column("sort_order", String),
        sql_column("sort_key", DateTime(timezone=True)), name="sort_keys",
    ).data([(kind, sort_order, key) for (kind, sort_order), key in sorted(keys.items())])
    s.execute(insert(order).from_select(
        ["kind", "sort_order", "group_key", "sort_key", "accepted_at", "revision_id", "seq", "xid"],
        select(table.c.kind, table.c.sort_order, revision.group_key, table.c.sort_key,
               revision.accepted_at, revision.id, revision.seq, revision.xid)
        .select_from(table).join(revision, revision.id == row.id),
    ))


def ensure_cluster(s):
    """Forget transaction ids written by another cluster (restore elsewhere, upgrade).

    Stored xids only exclude publications still running when a session opened.
    In a different cluster they would compare as future transactions and hide
    published revisions, so they are cleared (the sequence alone then decides)
    and sessions whose snapshots came from the old cluster are dropped.
    """
    identifier = s.scalar(text("SELECT system_identifier::text FROM pg_control_system()"))
    stored = s.scalar(select(FeedWatermarkCluster).where(FeedWatermarkCluster.id == 1).with_for_update())
    if stored is None:
        s.add(FeedWatermarkCluster(id=1, system_identifier=identifier))
        return False
    if stored.system_identifier == identifier:
        return False
    s.execute(text("SET LOCAL statement_timeout = '300s'"))
    for table in (FeedRevision, FeedGroupCurrent, FeedGroupOrder):
        s.execute(update(table).where(table.xid.is_not(None)).values(xid=None))
    s.execute(delete(FeedSession).where(FeedSession.filters.has_key("watermark")))
    stored.system_identifier = identifier
    return True


def reconcile(s):
    """Repair pointers for revisions published by code that predates this index."""
    revision, current = FeedRevision, FeedGroupCurrent
    stale = list(s.scalars(
        select(revision.id)
        .outerjoin(current, current.group_key == revision.group_key)
        .where(revision.seq.is_not(None),
               or_(current.group_key.is_(None), current.seq.is_(None), revision.seq > current.seq))
        .distinct(revision.group_key)
        .order_by(revision.group_key, revision.seq.desc())
    ))
    if stale:
        publish_current(s, stale)
    return len(stale)
