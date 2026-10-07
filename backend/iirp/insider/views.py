"""Transaction rows and company/date groups as shown to readers; group revisions."""

import copy
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session

from iirp.insider.common import (
    ET,
    VISIBLE,
    _date_anomaly,
    _group_time_column,
    _instant,
    _json,
    _sort_transaction_date,
    transaction_kind,
)
from iirp.insider.tickers import normalized_ticker
from iirp.messages import UserError, msg
from iirp.models import (
    FeedRevision,
    Filing,
    FilingOwner,
    Issuer,
    TransactionEvent,
)

_LIST_SOURCE_FIELDS = {
    "table",
    "code",
    "security_title",
    "currency",
    "shares",
    "price_per_share",
    "owner_names",
    "owner_relationships",
    "kind",
    "ticker",
    "warnings",
    "direction",
    "action_category",
    "quantity_unit",
    "form",
    "source_row_index",
    "direct_or_indirect",
    "nature_of_ownership",
    "shares_after",
    "raw_10b5_1_flag",
}


_LIST_FIELDS = _LIST_SOURCE_FIELDS | {
    "id",
    "issuer_id",
    "accession",
    "version_id",
    "transaction_date",
    "accepted_at",
    "owner_ids",
    "owner_id",
    "owners",
    "owner",
    "price",
    "known_amount",
    "amount",
    "status",
    "eligible_for_totals",
    "replaces_id",
    "is_amendment_update",
    "summary_exclusion_reason",
    "group_accession",
    "group_accepted_at",
    "shares_before",
    "holding_change",
}


# Labels that stored rows and group revisions carried before message codes.
_LEGACY_LABELS = {
    "董事": msg("insider.role.director"),
    "高管": msg("insider.role.officer"),
    "持股超过10%": msg("insider.role.ten_percent_owner"),
    "主体身份待核对": msg("insider.owner_pending"),
    "修订更新": msg("insider.kind.amendment_update"),
    "日期异常、待核对": msg("insider.date_anomaly"),
    "修订关系待确认，源申报数值暂未计入确认汇总": msg("insider.exclusion.needs_review"),
    "历史交易修订通知，金额在原交易组计入": msg("insider.exclusion.amendment_update"),
    "已由修订版本替代，不计入当前确认汇总": msg("insider.exclusion.superseded"),
    "已确认重复，不重复计入汇总": msg("insider.exclusion.duplicate"),
    "申报交易已撤回，不计入确认汇总": msg("insider.exclusion.withdrawn"),
    "修订范围需逐行核对；不因同日期或行位置自动替换。": msg("insider.amendment.needs_row_review"),
    "不同 accession 的相似行仅标可能重复，未静默合并。": msg("insider.amendment.possible_duplicate"),
    **{label: transaction_kind(category, table) for table in ("I", "II") for category, label in {
        "purchase_market_or_private": "公开市场或私人买入",
        "sale_market_or_private": "公开市场或私人卖出",
        "grant_or_award": "授予或奖励",
        "tax_or_exercise_price_withholding": "税款或行权价代扣",
        "exercise_or_conversion": "行权或转换",
        "needs_review": "交易含义待核对",
        "other": "其他交易行为",
    }.items() for label in [label if table == "I" else "衍生品 · " + label]},
}


def _legacy_labels(value):
    if isinstance(value, dict):
        return {key: _legacy_labels(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_legacy_labels(child) for child in value]
    return _LEGACY_LABELS.get(value, value) if isinstance(value, str) else value


def _ticker_read_view(data):
    """Normalize old immutable revision payloads without rewriting source history."""
    data = _legacy_labels(data)
    if "ticker" in data:
        raw = data.get("issuer_ticker_raw", data["ticker"])
        data["ticker"] = normalized_ticker(data["ticker"])
        if data["ticker"] is None and raw:
            data["issuer_ticker_raw"] = raw
    return data


def _compact_row(row):
    compact = {
        key: value
        for key, value in row.items()
        if key in _LIST_FIELDS and key not in {"owner_names", "owner_relationships"}
    }
    if "ticker" in row and "issuer_ticker_raw" in row:
        compact["issuer_ticker_raw"] = row["issuer_ticker_raw"]
    anomaly = row.get("date_anomaly") or _date_anomaly(row)
    if anomaly:
        compact["date_anomaly"] = anomaly
    return _ticker_read_view(compact)


def _owner_view(identifier, relationship, name=None):
    def truth(key):
        return str(relationship.get(key, "")).lower() in {"1", "true"}
    roles = []
    if truth("is_director"):
        roles.append(msg("insider.role.director"))
    if truth("is_officer"):
        roles.append(relationship.get("officer_title") or msg("insider.role.officer"))
    if truth("is_ten_percent_owner"):
        roles.append(msg("insider.role.ten_percent_owner"))
    if truth("is_other") and relationship.get("other_text"):
        roles.append(relationship["other_text"])
    # SEC role flags are evidence; names never determine a person's/entity's type.
    category = relationship.get("entity_type")
    if category not in {"person", "institution"}:
        category = "person" if truth("is_director") or truth("is_officer") else "unknown"
    return {"id": identifier, "name": name or relationship.get("name") or identifier,
            "roles": roles, "entity_type": category}


def _summary_exclusion_reason(status, *, amendment_update=False):
    if status == "NEEDS_REVIEW":
        return msg("insider.exclusion.needs_review")
    if amendment_update:
        return msg("insider.exclusion.amendment_update")
    return {
        "SUPERSEDED": msg("insider.exclusion.superseded"),
        "DUPLICATE_CONFIRMED": msg("insider.exclusion.duplicate"),
        "WITHDRAWN": msg("insider.exclusion.withdrawn"),
    }.get(status)


def _with_filing_owners(s, rows):
    versions = {row.get("version_id") for row in rows if row.get("version_id")}
    owners = {}
    original_ids = {row.get("replaces_id") for row in rows if row.get("replaces_id") and not row.get("is_amendment_update")}
    originals = {item.id: item for item in s.execute(select(
        TransactionEvent.id, TransactionEvent.accession, TransactionEvent.accepted_at,
        TransactionEvent.data["original_accession"].astext.label("original_accession"),
        TransactionEvent.data["original_accepted_at"].astext.label("original_accepted_at"),
    ).where(TransactionEvent.id.in_(original_ids)))} if original_ids else {}
    if versions:
        for owner in s.scalars(select(FilingOwner).where(FilingOwner.version_id.in_(versions))):
            owners.setdefault(owner.version_id, {})[owner.owner_id] = owner.relationship
    for row in rows:
        original = originals.get(row.get("replaces_id")) if not row.get("is_amendment_update") else None
        row["group_accession"] = (original.original_accession or original.accession) if original else row.get("accession")
        accepted = (_instant(original.original_accepted_at) or original.accepted_at) if original else None
        row["group_accepted_at"] = accepted.isoformat() if accepted else row.get("accepted_at")
        names = {owner["id"]: owner.get("name") for owner in row.get("owners", [])}
        row["owners"] = [_owner_view(identifier, owners.get(row.get("version_id"), {}).get(identifier, {}), names.get(identifier)) for identifier in row.get("owner_ids", [])]
        row["summary_exclusion_reason"] = _summary_exclusion_reason(
            row.get("status"), amendment_update=row.get("is_amendment_update", False)
        )
    return rows


def _ordered_rows(rows, order="transaction"):
    return sorted(rows, key=lambda row: ((_sort_transaction_date(row) if order == "transaction" else row.get("group_accepted_at", row.get("accepted_at"))) or "", row.get("accepted_at") or "", row.get("id") or ""), reverse=True)


def _range_summary(rows):
    dates = sorted({date for row in rows if (date := _sort_transaction_date(row))})
    return {"transaction_dates": [dates[0], dates[-1]] if len(dates) > 1 else dates,
            "owners": len({owner for row in rows for owner in row.get("owner_ids", [])}),
            "filings": len({row.get("group_accession", row.get("accession")) for row in rows if row.get("accession")}),
            "accepted_at": max((row.get("group_accepted_at", row.get("accepted_at")) or "" for row in rows), default=None)}


def _trader_key(row):
    return sha256(json.dumps([sorted(row.get("owner_ids", [])), row.get("transaction_date"), row.get("table"), row.get("code"), row.get("security_title"), row.get("currency")]).encode()).hexdigest()[:24]


def _trader_groups(rows):
    groups = {}
    for row in rows:
        groups.setdefault(_trader_key(row), []).append(row)
    return [{"id": key, "owners": group[0].get("owners", []), "transaction_date": group[0].get("transaction_date"),
             "summary": _summary(group), "rows": len(group)} for key, group in groups.items()]


def _event_view(
    event: TransactionEvent, *, snapshot_status: str | None = None, compact=False
) -> dict:
    data = copy.deepcopy(
        {
            key: value
            for key, value in event.data.items()
            if (not compact or key in _LIST_SOURCE_FIELDS) and key != "raw_xml"
        }
    )
    if "kind" in data:
        data["kind"] = _legacy_labels(data["kind"])
    if "ticker" in data:
        data["issuer_ticker_raw"] = data.get("issuer_ticker_raw", data["ticker"])
        data["ticker"] = normalized_ticker(data["ticker"])
    status = snapshot_status or event.status
    shares, price = data.get("shares"), data.get("price_per_share")
    amount = (
        str(Decimal(shares) * Decimal(price))
        if status == "CURRENT" and shares is not None and price is not None
        else None
    )
    return {
        **data,
        "id": event.id,
        "issuer_id": event.issuer_id,
        "accession": event.accession,
        "version_id": event.version_id,
        "transaction_date": event.transaction_date.isoformat() if event.transaction_date else None,
        "accepted_at": event.accepted_at.isoformat() if event.accepted_at else None,
        "group_accepted_at": event.data.get("original_accepted_at") or (event.accepted_at.isoformat() if event.accepted_at else None),
        "group_accession": event.data.get("original_accession") or event.accession,
        "owner_ids": list(event.owner_ids),
        "owner_id": event.owner_ids[0] if len(event.owner_ids) == 1 else None,
        "owners": [
            _owner_view(owner_id, data.get("owner_relationships", {}).get(owner_id, {}), data.get("owner_names", {}).get(owner_id))
            for owner_id in event.owner_ids
        ],
        "owner": "、".join(
            data.get("owner_names", {}).get(owner_id) or owner_id for owner_id in event.owner_ids
        )
        or msg("insider.owner_pending"),
        "price": price,
        "known_amount": amount,
        "amount": amount,
        "status": status,
        "eligible_for_totals": status == "CURRENT",
        "summary_exclusion_reason": _summary_exclusion_reason(status),
        "replaces_id": event.replaces_id,
        **_holding_change(data),
        "date_anomaly": _date_anomaly({
            "transaction_date": event.transaction_date.isoformat() if event.transaction_date else None,
            "accepted_at": event.accepted_at,
        }),
    }


def _holding_change(data: dict) -> dict:
    """Holdings before the row and the change as a ratio of them (A adds, D removes)."""
    try:
        shares, after = Decimal(str(data["shares"])), Decimal(str(data["shares_after"]))
    except (KeyError, TypeError, ArithmeticError, ValueError):
        return {}
    sign = {"A": 1, "D": -1}.get(data.get("direction"))
    if sign is None or not shares.is_finite() or not after.is_finite():
        return {}
    before = after - sign * shares
    if before < 0:
        return {}
    return {"shares_before": str(before),
            "holding_change": str(sign * shares / before) if before > 0 else None}


def _matches(row: dict, kind: str) -> bool:
    if kind in {"all", "全部", ""}:
        return True
    if kind in {"focus", "重点"}:
        return row.get("table") == "II" or row.get("code") in {"P", "S"}
    if kind in {"buy", "purchase", "P"}:
        return row.get("table") == "I" and row.get("code") == "P"
    if kind in {"sell", "sale", "S"}:
        return row.get("table") == "I" and row.get("code") == "S"
    if kind in {"derivative", "derivatives", "option"}:
        return row.get("table") == "II"
    if kind == "other":
        return row.get("table") != "II" and row.get("code") not in {"P", "S"}
    raise UserError("insider.filter_invalid")


def _summary(rows: list[dict]) -> list[dict]:
    buckets = {}
    for row in rows:
        key = (row.get("table"), row.get("security_title"), row.get("currency"), row.get("code"))
        bucket = buckets.setdefault(
            key,
            {
                "table": key[0],
                "security_title": key[1],
                "currency": key[2],
                "code": key[3],
                "kind": row.get("kind"),
                "shares": Decimal(0),
                "known_amount": Decimal(0),
                "rows": 0,
                "missing_price_rows": 0,
                "review_rows": 0,
                "known_price_rows": 0,
                "known_shares_rows": 0,
                "owners_by_id": {},
                "reported_value_review_rows": 0,
                "missing_source_value_rows": 0,
            },
        )
        bucket["rows"] += 1
        for owner in row.get("owners", []):
            bucket["owners_by_id"][owner["id"]] = owner
        for identifier in row.get("owner_ids", []):
            bucket["owners_by_id"].setdefault(identifier, {"id": identifier, "entity_type": "unknown"})
        if not row["eligible_for_totals"]:
            bucket["review_rows"] += 1
            if row.get("shares") is not None or row.get("price") is not None:
                bucket["reported_value_review_rows"] += 1
            else:
                bucket["missing_source_value_rows"] += 1
            continue
        if row.get("shares") is not None:
            bucket["shares"] += Decimal(row["shares"])
            bucket["known_shares_rows"] += 1
        if row.get("known_amount") is not None:
            bucket["known_amount"] += Decimal(row["known_amount"])
            bucket["known_price_rows"] += 1
        else:
            bucket["missing_price_rows"] += 1
    for bucket in buckets.values():
        owners = list(bucket.pop("owners_by_id").values())
        bucket["owner_count"] = len(owners)
        bucket["person_count"] = sum(owner.get("entity_type") == "person" for owner in owners)
        bucket["institution_count"] = sum(owner.get("entity_type") == "institution" for owner in owners)
        bucket["unknown_owner_count"] = len(owners) - bucket["person_count"] - bucket["institution_count"]
        if not bucket["known_price_rows"]:
            bucket["known_amount"] = None
        if not bucket["known_shares_rows"]:
            bucket["shares"] = None
    return [_json(bucket) for bucket in buckets.values()]


def _group_sort_dates(rows) -> dict:
    """Stored per-filter sort dates; anomalous transaction dates never count."""
    facets = _group_facets(rows)
    return {
        **{kind: max((_sort_transaction_date(row) for row in rows if _matches(row, kind)), default="") for kind in facets},
        **{"accepted:" + kind: max((row.get("group_accepted_at") or row.get("accepted_at") or "" for row in rows if _matches(row, kind)), default="") for kind in facets},
    }


def _refresh_groups(s: Session, groups: set[tuple[str, date | None]]) -> None:
    from iirp.insider.feed_index import publish_current

    published = []
    for issuer_id, day in sorted(groups, key=lambda item: (item[0], str(item[1]))):
        if day is None:
            continue  # Never invent an acceptance instant for a feed time group.
        start = datetime.combine(day, time.min, tzinfo=ET)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=ET)
        events = list(
            s.scalars(
                select(TransactionEvent)
                .join(Filing, Filing.accession == TransactionEvent.accession)
                .where(
                    TransactionEvent.issuer_id == issuer_id,
                    _group_time_column() >= start,
                    _group_time_column() < end,
                    TransactionEvent.status.in_(VISIBLE),
                    Filing.visible.is_(True),
                )
                .order_by(TransactionEvent.accepted_at.desc(), TransactionEvent.id.desc())
            )
        )
        rows = [_compact_row(_event_view(event, compact=True)) for event in events]
        updates = list(
            s.scalars(
                select(TransactionEvent)
                .join(Filing, Filing.accession == TransactionEvent.accession)
                .where(
                    TransactionEvent.issuer_id == issuer_id,
                    TransactionEvent.accepted_at >= start,
                    TransactionEvent.accepted_at < end,
                    TransactionEvent.data.has_key("original_accepted_at"),
                    TransactionEvent.status == "CURRENT",
                    Filing.visible.is_(True),
                    _group_time_column() < start,
                )
                .order_by(TransactionEvent.accepted_at.desc(), TransactionEvent.id.desc())
            )
        )
        update_rows = [
            {
                **_compact_row(_event_view(event, compact=True)),
                "is_amendment_update": True,
                "group_accepted_at": event.accepted_at.isoformat(),
                "group_accession": event.accession,
                "kind": msg("insider.kind.amendment_update"),
                "known_amount": None,
                "amount": None,
                "eligible_for_totals": False,
            }
            for event in updates
        ]
        issuer = s.get(Issuer, issuer_id)
        dates = sorted({date for row in rows if (date := _sort_transaction_date(row))})
        group_key = sha256(f"{issuer_id}:{day.isoformat()}".encode()).hexdigest()[:32]
        accepted = max(
            [
                *(
                    _instant(event.data.get("original_accepted_at")) or event.accepted_at
                    for event in events
                ),
                *(event.accepted_at for event in updates),
            ],
            default=start,
        )
        data = {
            "id": group_key,
            "issuer_id": issuer_id,
            "company": issuer.name if issuer else issuer_id,
            "ticker": next(
                (row["ticker"] for row in rows + update_rows if row.get("ticker")), None
            ),
            "accepted_at": accepted.isoformat(),
            "accepted_date": day.isoformat(),
            "transaction_dates": [dates[0], dates[-1]] if len(dates) > 1 else dates,
            "owners": len({owner for event in events + updates for owner in event.owner_ids}),
            "filings": len(
                {event.data.get("original_accession", event.accession) for event in events}
            ),
            "transactions": rows + update_rows,
            "total_transactions": len(rows),
            "summary": _summary(rows),
            "amendment_updates": len(update_rows),
        }
        latest = s.scalar(
            select(FeedRevision)
            .where(FeedRevision.group_key == group_key)
            .order_by(FeedRevision.seq.desc().nulls_last(), FeedRevision.created_at.desc(),
                      FeedRevision.id.desc())
            .limit(1)
        )
        if latest and latest.data == data:
            continue
        # Empty revisions are tombstones; old sessions still reference old data.
        revision = FeedRevision(
            group_key=group_key,
            issuer_id=issuer_id,
            accepted_at=accepted,
            data=data,
            match_kinds=_group_facets(rows + update_rows),
            row_count=len(rows + update_rows),
            transaction_sort_dates=_group_sort_dates(rows + update_rows),
        )
        s.add(revision)
        published.append(revision)
    s.flush()
    # Pointer rows are locked per group; take them last so the lock lasts only to commit.
    publish_current(s, [revision.id for revision in published])


def _group_facets(rows):
    return [
        kind
        for kind in ("all", "focus", "buy", "sell", "derivative", "other")
        if any(_matches(row, kind) for row in rows)
    ]
