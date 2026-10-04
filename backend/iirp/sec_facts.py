"""SEC observations, current row facts, and immutable local reading snapshots.

The caller supplies one fenced SQLAlchemy transaction and owns its commit. There
is no network or file I/O here. SourceObject records must already exist in that
same transaction. A new parser/source revision retains old observations; an
uncertain amendment remains reviewable and never silently replaces a trade.
"""

import base64
import copy
import json
import uuid
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from zoneinfo import ZoneInfo

from sqlalchemy import Date, DateTime, cast, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from iirp.business_models import (
    AmendmentRelation,
    BatchJob,
    CoverageSegment,
    FeedGroupOrder,
    FeedRevision,
    FeedSession,
    Filing,
    FilingOwner,
    FilingVersion,
    Issuer,
    Owner,
    Security,
    SourceObservation,
    TransactionEvent,
)
from iirp.domain.ownership import parse_ownership_xml
from iirp.models import ACTIVE, Job, now
from iirp.sec_sources import OWNERSHIP_FORMS, validate_sec_url
from iirp.ticker_identity import normalized_ticker

PARSER_VERSION = "ownership-v1.1"
ET = ZoneInfo("America/New_York")
VISIBLE = ("CURRENT", "NEEDS_REVIEW")
_NAMESPACE = uuid.UUID("9916b579-ef13-4d36-aeb5-4bcb5e572f11")


def _uid(*parts) -> str:
    return str(uuid.uuid5(_NAMESPACE, ":".join(str(part) for part in parts)))


def _json(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return {"encoding": "base64", "payload": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {key: _json(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(child) for child in value]
    return value


def _instant(value) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ValueError("接受时间必须包含真实时间和时区。")
    return parsed


def _date(value) -> date | None:
    if value == "":
        return None
    return date.fromisoformat(value) if isinstance(value, str) else value


DATE_ANOMALY_LABEL = "日期异常、待核对"


def _accepted_day(value) -> str | None:
    instant = _instant(value)
    return instant.astimezone(ET).date().isoformat() if instant else None


def _date_anomaly(row) -> dict | None:
    """A transaction dated after its own SEC acceptance date, e.g. a mistyped year.

    The reported value is kept and shown; it never drives date sorting or ranges.
    """
    transaction, accepted = row.get("transaction_date"), _accepted_day(row.get("accepted_at"))
    if transaction and accepted and str(transaction) > accepted:
        return {"code": "TRANSACTION_AFTER_ACCEPTANCE", "label": DATE_ANOMALY_LABEL,
                "transaction_date": str(transaction), "accepted_date": accepted}
    return None


def _sort_transaction_date(row) -> str:
    return "" if _date_anomaly(row) else (row.get("transaction_date") or "")


def _cik(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdigit()
        or not 1 <= len(value) <= 10
        or int(value) == 0
    ):
        raise ValueError("CIK 必须是 1 至 10 位数字。")
    return value.zfill(10)


def _observation(s: Session, job, source_hash: str, params: dict):
    job_id = getattr(job, "id", None)
    marker = sha256(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    s.execute(
        insert(SourceObservation)
        .values(
            id=_uid("source", job_id, source_hash, marker),
            job_id=job_id,
            source_hash=source_hash,
            provider="sec",
            params=params,
        )
        .on_conflict_do_nothing(index_elements=[SourceObservation.id])
    )


def persist_discovery(s: Session, job, response: dict, sources: dict[str, str]) -> list[dict]:
    """Save every discovered accession and scan evidence; return document work."""
    target = getattr(job, "target", {}) or {}
    scan = response.get("scan", {})
    accessions = [entry["accession"] for entry in response.get("entries", [])]
    if getattr(job, "id", None):
        job.checkpoint = {
            **(job.checkpoint or {}),
            "sec_scan": scan,
            "sec_cursor": response.get("cursor"),
            "manifest_hash": sha256(json.dumps(sorted(accessions)).encode()).hexdigest(),
            "manifest_count": len(accessions),
            "discovered_accessions": accessions if target.get("mode", "latest") == "latest" else [],
        }
    for url, source_hash in sources.items():
        _observation(
            s,
            job,
            source_hash,
            {"url": url, "target": target, "scan": scan, "accessions": accessions},
        )
    needed, changed = [], set()
    for entry in response.get("entries", []):
        if entry.get("form") not in OWNERSHIP_FORMS:
            raise ValueError("非 Ownership 表格不能进入交易事实链路。")
        accession = entry["accession"]
        s.execute(
            insert(Filing)
            .values(
                accession=accession,
                form=entry["form"],
                filing_date=_date(entry.get("filing_date")),
                accepted_at=_instant(entry.get("accepted_at")),
                index_url=validate_sec_url(entry["index_url"]),
                status="DISCOVERED",
            )
            .on_conflict_do_nothing(index_elements=[Filing.accession])
        )
        filing = s.scalar(select(Filing).where(Filing.accession == accession).with_for_update())
        if filing.form != entry["form"]:
            raise ValueError("同一 accession 的表格类型冲突，需核对来源。")
        if filing.current_version is None:
            filing.accepted_at = filing.accepted_at or _instant(entry.get("accepted_at"))
            filing.filing_date = filing.filing_date or _date(entry.get("filing_date"))
        if not filing.visible:
            filing.visible = True
            filing.status = "PARSED" if filing.current_version else "DISCOVERED"
            for event in s.scalars(
                select(TransactionEvent).where(TransactionEvent.accession == filing.accession)
            ):
                changed.add((event.issuer_id, _group_date(event)))
        if filing.current_version is None or target.get("reconcile_documents"):
            needed.append(dict(entry))
    scan = response.get("scan", {})
    start, end = _date(scan.get("start_date")), _date(scan.get("end_date"))
    if start and end:
        source_hash = next(iter(sources.values()), None)
        marker = json.dumps({"scan": scan, "sources": sources}, sort_keys=True)
        coverage_id = _uid("sec-coverage", sha256(marker.encode()).hexdigest())
        s.execute(
            insert(CoverageSegment)
            .values(
                id=coverage_id,
                provider="sec",
                kind="ownership_index",
                start_date=start,
                end_date=end,
                status="COMPLETE" if scan.get("complete") else "PARTIAL",
                source_hash=source_hash,
                details={
                    "scan": scan,
                    "cursor": response.get("cursor"),
                    "coverage_scope": "index_filing_dates_only",
                },
            )
            .on_conflict_do_nothing(index_elements=[CoverageSegment.id])
        )
    # A complete, single quarterly index can prove that previously indexed
    # accessions vanished from that *same* filing-date range. Preserve evidence.
    if (
        target.get("reconcile_removals")
        and scan.get("complete")
        and scan.get("mode") == "quarterly"
        and start
        and end
        and (start.year, (start.month - 1) // 3) == (end.year, (end.month - 1) // 3)
        and len(scan.get("scanned_indexes", [])) == 1
        and scan["scanned_indexes"][0].get("as_of")
    ):
        present = {entry["accession"] for entry in response.get("entries", [])}
        for filing in s.scalars(
            select(Filing).where(Filing.filing_date.between(start, end), Filing.visible.is_(True))
        ):
            if filing.accession not in present:
                filing.visible, filing.status = False, "SOURCE_WITHDRAWN"
                for event in s.scalars(
                    select(TransactionEvent).where(TransactionEvent.accession == filing.accession)
                ):
                    changed.add((event.issuer_id, _group_date(event)))
    if changed:
        s.flush()
        _refresh_groups(s, changed)
    return needed


def _row_data(row, observation, owners) -> dict:
    data = _json(asdict(row))
    category = row.action_category
    kind = {
        "purchase_market_or_private": "公开市场或私人买入",
        "sale_market_or_private": "公开市场或私人卖出",
        "grant_or_award": "授予或奖励",
        "tax_or_exercise_price_withholding": "税款或行权价代扣",
        "exercise_or_conversion": "行权或转换",
        "needs_review": "交易含义待核对",
        "other": "其他交易行为",
    }.get(category, category)
    if row.table == "II":
        kind = "衍生品 · " + kind
    data.update(
        {
            "kind": kind,
            "price": data["price_per_share"],
            "ticker": normalized_ticker(observation.issuer_ticker),
            "issuer_ticker_raw": observation.issuer_ticker,
            "issuer_name": observation.issuer_name,
            "owner_names": {owner.cik: owner.name for owner in owners if owner.cik},
            "owner_relationships": {owner.cik: {key: getattr(owner, key, None) for key in ("name", "is_director", "is_officer", "is_ten_percent_owner", "is_other", "officer_title", "other_text")} for owner in owners if owner.cik},
            "footnotes": [
                _json(asdict(note)) for note in observation.footnotes if note.id in row.footnote_ids
            ],
            "raw_10b5_1_flag": observation.raw_10b5_1_flag,
            "form": observation.form_type,
        }
    )
    return data


def _fingerprint(data: dict, owner_ids: list[str], *, economic=False) -> str:
    if economic:
        keys = (
            "table",
            "security_title",
            "transaction_date",
            "code",
            "direction",
            "shares",
            "price_per_share",
            "direct_or_indirect",
            "shares_after",
        )
        value = {key: data.get(key) for key in keys}
    else:
        value = {
            key: value
            for key, value in data.items()
            if key not in {"raw_xml", "source_row_index", "owner_names", "owner_relationships", "issuer_name", "form"}
        }
    return sha256(
        json.dumps([value, sorted(owner_ids)], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _group_date(event) -> date | None:
    accepted = _instant(event.data.get("original_accepted_at")) or event.accepted_at
    if not accepted:
        return None
    return accepted.astimezone(ET).date()


def _group_time_column():
    return func.coalesce(
        cast(TransactionEvent.data["original_accepted_at"].astext, DateTime(timezone=True)),
        TransactionEvent.accepted_at,
    )


def persist_document(s: Session, job, response: dict, sources: dict[str, str]) -> None:
    """Publish valid observations and row facts inside the caller's fenced commit."""
    raw = response.get("filing")
    if not raw:
        raise ValueError("原文尚未验证，不能生成交易事实。")
    xml = (
        raw["xml_payload"].encode("utf-8")
        if raw.get("xml_encoding", "utf8") == "utf8"
        else base64.b64decode(raw["xml_payload"], validate=True)
    )
    if raw.get("xml_sha256") and sha256(xml).hexdigest() != raw["xml_sha256"]:
        raise ValueError("Ownership 原文字节与来源哈希不一致。")
    observation = parse_ownership_xml(
        xml, source_kind="sec", accession=raw["accession"], source_url=raw["document_url"]
    )
    if not observation.issuer_cik:
        raise ValueError("原文发行人 CIK 缺失，保存证据后等待核对。")
    if observation.form_type != raw["form"]:
        raise ValueError("原文表格类型与来源元数据不一致。")
    # A header correction can change acceptance time while XML is unchanged.
    # Prefer the complete submission, or the index that proved fallback timing,
    # so these changes create a new immutable observation version as well.
    source_hash = next(
        (value for url, value in sources.items() if url.endswith(raw["accession"] + ".txt")),
        None,
    )
    if source_hash is None and raw.get("accepted_at_source") == "sec_filing_index":
        source_hash = next(
            (
                value
                for url, value in sources.items()
                if url.endswith(("-index.html", "-index.htm"))
            ),
            None,
        )
    source_hash = source_hash or sources.get(raw["document_url"])
    if source_hash is None:
        raise ValueError("缺少包含本份 Ownership 原文的已保存来源对象。")
    for url, digest in sources.items():
        _observation(
            s,
            job,
            digest,
            {"url": url, "accession": raw["accession"], "xml_sha256": sha256(xml).hexdigest()},
        )
    s.execute(
        insert(Issuer)
        .values(id=observation.issuer_cik, name=observation.issuer_name or observation.issuer_cik)
        .on_conflict_do_update(
            index_elements=[Issuer.id],
            set_={"name": observation.issuer_name or observation.issuer_cik},
        )
    )
    s.execute(
        insert(Filing)
        .values(
            accession=raw["accession"],
            form=observation.form_type,
            accepted_at=_instant(raw.get("accepted_at")),
            filing_date=_date(raw.get("filing_date")),
            issuer_id=observation.issuer_cik,
            index_url=(getattr(job, "target", {}) or {}).get("index_url", ""),
            status="DISCOVERED",
        )
        .on_conflict_do_nothing(index_elements=[Filing.accession])
    )
    filing = s.scalar(select(Filing).where(Filing.accession == raw["accession"]).with_for_update())
    version_id = _uid("version", filing.accession, source_hash, PARSER_VERSION)
    if s.get(FilingVersion, version_id):
        return
    prior_events = list(
        s.scalars(
            select(TransactionEvent).where(
                TransactionEvent.accession == filing.accession, TransactionEvent.status.in_(VISIBLE)
            )
        )
    )
    changed_groups = {(event.issuer_id, _group_date(event)) for event in prior_events}
    filing.form = observation.form_type
    filing.accepted_at = _instant(raw.get("accepted_at"))
    filing.filing_date = _date(raw.get("filing_date")) or filing.filing_date
    filing.issuer_id, filing.visible = observation.issuer_cik, True
    data = _json(asdict(observation))
    data.update(
        {"source_metadata": raw, "xml_sha256": sha256(xml).hexdigest(), "source_objects": sources}
    )
    s.add(
        FilingVersion(
            id=version_id,
            accession=filing.accession,
            source_hash=source_hash,
            parser_version=PARSER_VERSION,
            data=data,
        )
    )
    s.flush()
    owner_ids = sorted({owner.cik for owner in observation.owners if owner.cik})
    for owner in sorted(observation.owners, key=lambda item: item.cik or ""):
        if not owner.cik:
            continue
        s.execute(
            insert(Owner)
            .values(id=owner.cik, name=owner.name or owner.cik)
            .on_conflict_do_update(
                index_elements=[Owner.id], set_={"name": owner.name or owner.cik}
            )
        )
        s.execute(
            insert(FilingOwner)
            .values(
                version_id=version_id,
                owner_id=owner.cik,
                relationship=_json(asdict(owner)),
            )
            .on_conflict_do_nothing(index_elements=[FilingOwner.version_id, FilingOwner.owner_id])
        )
    old_by_key = {event.row_key: event for event in prior_events}
    rows = [
        (row, _row_data(row, observation, observation.owners))
        for row in observation.rows
        if row.is_trade_observation
    ]
    incoming_fingerprints = {_fingerprint(row_data, owner_ids) for _, row_data in rows}
    used_old_ids, event_rows = set(), {}
    for row, row_data in rows:
        key = f"{row.table}:{row.row_kind}:{row.source_row_index}"
        previous = old_by_key.get(key)
        fingerprint = _fingerprint(row_data, owner_ids)
        unchanged = [
            candidate
            for candidate in prior_events
            if candidate.id not in used_old_ids
            and _fingerprint(candidate.data, candidate.owner_ids) == fingerprint
            and candidate.accepted_at == filing.accepted_at
            and candidate.issuer_id == filing.issuer_id
        ]
        if unchanged:
            retained = previous if previous in unchanged else unchanged[0]
            used_old_ids.add(retained.id)
            event_rows[key] = retained.id
            continue
        if previous and (
            previous.id in used_old_ids
            or _fingerprint(previous.data, previous.owner_ids) in incoming_fingerprints
        ):
            previous = None  # A row inserted above an unchanged row is not its correction.
        event_id = _uid("event", version_id, key)
        event_rows[key] = event_id
        event = TransactionEvent(
            id=event_id,
            issuer_id=observation.issuer_cik,
            accession=filing.accession,
            version_id=version_id,
            row_key=key,
            transaction_date=row.transaction_date,
            accepted_at=filing.accepted_at,
            data=row_data,
            owner_ids=owner_ids,
            status="NEEDS_REVIEW"
            if observation.is_amendment or not owner_ids or row.action_category == "needs_review"
            else "CURRENT",
            replaces_id=previous.id if previous else None,
        )
        s.add(event)
        s.flush()
        if previous:
            used_old_ids.add(previous.id)
            previous.status = "SUPERSEDED"
            s.add(
                AmendmentRelation(
                    id=_uid("source-change", previous.id, event.id),
                    accession=filing.accession,
                    original_event_id=previous.id,
                    amended_event_id=event.id,
                    action="SOURCE_REVISION",
                    evidence={
                        "same_accession": filing.accession,
                        "old_version": previous.version_id,
                        "new_version": version_id,
                        "row_key": key,
                    },
                )
            )
        elif observation.is_amendment:
            candidates = list(
                s.scalars(
                    select(TransactionEvent).where(
                        TransactionEvent.issuer_id == observation.issuer_cik,
                        TransactionEvent.accession != filing.accession,
                        TransactionEvent.transaction_date == row.transaction_date,
                        TransactionEvent.status.in_(VISIBLE),
                    )
                )
            )
            plausible = [
                candidate.id
                for candidate in candidates
                if candidate.data.get("table") == row.table
                and candidate.data.get("security_title") == row.security_title
                and set(candidate.owner_ids) & set(owner_ids)
            ]
            s.add(
                AmendmentRelation(
                    id=_uid("amendment", event.id),
                    accession=filing.accession,
                    amended_event_id=event.id,
                    action="UNCONFIRMED",
                    evidence={
                        "candidate_event_ids": plausible,
                        "source_url": raw["document_url"],
                        "reason": "修订范围需逐行核对；不因同日期或行位置自动替换。",
                    },
                )
            )
        else:
            possible = list(
                s.scalars(
                    select(TransactionEvent).where(
                        TransactionEvent.issuer_id == observation.issuer_cik,
                        TransactionEvent.accession != filing.accession,
                        TransactionEvent.transaction_date == row.transaction_date,
                        TransactionEvent.status.in_(VISIBLE),
                    )
                )
            )
            duplicates = [
                candidate
                for candidate in possible
                if set(candidate.owner_ids) & set(owner_ids)
                and _fingerprint(candidate.data, [], economic=True)
                == _fingerprint(row_data, [], economic=True)
            ]
            for candidate in duplicates:
                event.status, candidate.status = "NEEDS_REVIEW", "NEEDS_REVIEW"
                changed_groups.add((candidate.issuer_id, _group_date(candidate)))
                s.add(
                    AmendmentRelation(
                        id=_uid("possible-duplicate", candidate.id, event.id),
                        accession=filing.accession,
                        original_event_id=candidate.id,
                        amended_event_id=event.id,
                        action="UNCONFIRMED_DUPLICATE",
                        evidence={
                            "reason": "不同 accession 的相似行仅标可能重复，未静默合并。",
                            "source_url": raw["document_url"],
                        },
                    )
                )
        changed_groups.add((event.issuer_id, _group_date(event)))
    for key, previous in old_by_key.items():
        if previous.id not in used_old_ids:
            previous.status = "SUPERSEDED"
            s.add(
                AmendmentRelation(
                    id=_uid("source-row-removed", previous.id, version_id),
                    accession=filing.accession,
                    original_event_id=previous.id,
                    action="SOURCE_ROW_REMOVED",
                    evidence={
                        "old_version": previous.version_id,
                        "new_version": version_id,
                        "row_key": key,
                    },
                )
            )
    filing.current_version, filing.status = version_id, "PARSED"
    s.get(FilingVersion, version_id).data = {**data, "event_rows": event_rows}
    s.flush()
    _refresh_groups(s, changed_groups)


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
}


def _ticker_read_view(data):
    """Normalize old immutable revision payloads without rewriting source history."""
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
        roles.append("董事")
    if truth("is_officer"):
        roles.append(relationship.get("officer_title") or "高管")
    if truth("is_ten_percent_owner"):
        roles.append("持股超过10%")
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
        return "修订关系待确认，源申报数值暂未计入确认汇总"
    if amendment_update:
        return "历史交易修订通知，金额在原交易组计入"
    return {
        "SUPERSEDED": "已由修订版本替代，不计入当前确认汇总",
        "DUPLICATE_CONFIRMED": "已确认重复，不重复计入汇总",
        "WITHDRAWN": "申报交易已撤回，不计入确认汇总",
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
            if not compact or key in _LIST_SOURCE_FIELDS
        }
    )
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
        or "主体身份待核对",
        "price": price,
        "known_amount": amount,
        "amount": amount,
        "status": status,
        "eligible_for_totals": status == "CURRENT",
        "summary_exclusion_reason": _summary_exclusion_reason(status),
        "replaces_id": event.replaces_id,
        "date_anomaly": _date_anomaly({
            "transaction_date": event.transaction_date.isoformat() if event.transaction_date else None,
            "accepted_at": event.accepted_at,
        }),
    }


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
    raise ValueError("不支持的交易筛选。")


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
    from iirp.feed_index import publish_current

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
                "kind": "修订更新",
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


def _offset(cursor: str) -> int:
    if not cursor:
        return 0
    if not cursor.isascii() or not cursor.isdigit():
        raise ValueError("分页游标无效。")
    return int(cursor)


def _session(s: Session, session_id: str, purpose: str) -> FeedSession:
    from iirp.feed_snapshots import load_feed_session

    saved = load_feed_session(s, session_id)
    if saved is None or saved.expires_at < now() or saved.filters.get("purpose") != purpose:
        raise ValueError("阅读快照不存在或已过期，请刷新后重新打开。")
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


def _group_facets(rows):
    return [
        kind
        for kind in ("all", "focus", "buy", "sell", "derivative", "other")
        if any(_matches(row, kind) for row in rows)
    ]


def latest_feed_metadata(s: Session, kind="all", order="transaction") -> list[dict]:
    """Current non-empty revisions in listing order, scalar metadata only."""
    order_row = FeedGroupOrder
    statement = (
        select(order_row.revision_id.label("id"), order_row.group_key, order_row.accepted_at)
        .where(order_row.kind == _canonical_kind(kind), order_row.sort_order == order)
        .order_by(order_row.sort_key.desc(), order_row.accepted_at.desc(), order_row.group_key.desc())
    )
    return [dict(row) for row in s.execute(statement).mappings()]


def open_feed_session(s: Session, kind: str, order: str, **extra) -> FeedSession:
    """A reading session is a watermark plus filters; no revision list is stored."""
    from iirp.feed_index import count, new_watermark

    watermark = new_watermark(s)
    saved = FeedSession(
        revision_ids=[],
        filters={"purpose": "feed", "kind": kind, "order": order, "as_of": now().isoformat(),
                 "watermark": watermark, "total_groups": count(s, watermark, _canonical_kind(kind), order),
                 **extra},
        expires_at=now() + timedelta(hours=12),
    )
    s.add(saved)
    s.flush()
    return saved


def feed_watermark(saved: FeedSession) -> dict | None:
    """None for sessions frozen as revision manifests before the watermark schema."""
    return saved.filters.get("watermark")


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


def feed(s: Session, session_id: str = "", cursor: str = "", kind: str = "all", order="transaction") -> dict:
    """Return twenty company/date groups of one reading session; session creation is local only."""
    from iirp.feed_index import PAGE_SIZE, decode_cursor, encode_cursor, listing

    _matches({}, kind)
    if order not in {"transaction", "accepted"}:
        raise ValueError("请选择实际交易日期或最近披露排序")
    if session_id:
        saved = _session(s, session_id, "feed")
        if "order" not in saved.filters:
            order = "accepted"  # Preserve pre-upgrade frozen group ordering.
        if saved.filters.get("kind") != kind or saved.filters.get("order", "accepted") != order:
            raise ValueError("筛选已变化，请创建新的阅读快照。")
    else:
        saved = open_feed_session(s, kind, order)
    watermark = feed_watermark(saved)
    if watermark is None:
        # Manifest sessions from before the upgrade stay readable until they expire.
        from iirp.feed_snapshots import session_revision_ids

        offset = _offset(cursor)
        revision_ids = session_revision_ids(s, saved)
        page_ids, total = revision_ids[offset : offset + 20], len(revision_ids)
        next_cursor = str(offset + 20) if offset + 20 < total else None
    else:
        entries = listing(s, watermark, _canonical_kind(kind), order,
                          after=decode_cursor(cursor) if cursor else None)
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
            "message": "展示本地已解析记录；具体历史范围以索引核对和剩余缺口为准。",
        },
    }


def feed_group(
    s: Session, session_id: str, group_id: str, cursor: str = "", limit: int = 20, trader_key=""
) -> dict:
    saved, offset = _session(s, session_id, "feed"), _offset(cursor.removeprefix("g:"))
    if not 1 <= limit <= 20:
        raise ValueError("每页明细最多 20 条。")
    watermark = feed_watermark(saved)
    if watermark is not None:
        from iirp.feed_index import group_revision, in_listing

        row = group_revision(s, watermark, group_id)
        kind, order = _canonical_kind(saved.filters["kind"]), saved.filters.get("order", "accepted")
        revision = s.get(FeedRevision, row.id) if row is not None and in_listing(row, kind, order) else None
    else:
        from iirp.feed_snapshots import session_revision_ids

        visible_ids = set(session_revision_ids(s, saved))
        revision_id = next(
            (
                value
                for value in s.scalars(
                    select(FeedRevision.id).where(FeedRevision.group_key == group_id)
                )
                if value in visible_ids
            ),
            None,
        )
        revision = s.get(FeedRevision, revision_id) if revision_id else None
    if revision is None:
        raise ValueError("本阅读快照中没有该公司组。")
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


def entity_history(
    s: Session,
    kind: str,
    id: str,
    start=None,
    end=None,
    recent_count=None,
    date_basis="transaction_date",
    cursor="",
    limit=20,
    session_id="",
) -> dict:
    from iirp.entity_reads import read_entity_history

    return read_entity_history(
        s, kind, id, start, end, recent_count, date_basis, cursor, limit, session_id
    )


def transaction_record(s: Session, id: str) -> dict:
    event = s.get(TransactionEvent, id)
    if event is None:
        raise ValueError("该交易尚未在本地保存。")
    filing, version = s.get(Filing, event.accession), s.get(FilingVersion, event.version_id)
    relations = list(
        s.scalars(
            select(AmendmentRelation).where(
                or_(
                    AmendmentRelation.amended_event_id == id,
                    AmendmentRelation.original_event_id == id,
                )
            )
        )
    )
    securities = list(s.scalars(select(Security).where(Security.issuer_id == event.issuer_id)))
    return {
        **_with_filing_owners(s, [_event_view(event)])[0],
        "first_seen_at": filing.first_seen_at.isoformat(),
        "source_url": version.data.get("source_url"),
        "source_hash": version.source_hash,
        "source_visible": filing.visible,
        "filing_status": filing.status,
        "owner_observations": version.data.get("owners", []),
        "amendments": [
            {
                "id": relation.id,
                "action": relation.action,
                "original_event_id": relation.original_event_id,
                "amended_event_id": relation.amended_event_id,
                "evidence": relation.evidence,
            }
            for relation in relations
        ],
        "securities": [
            {"id": security.id, "symbol": security.symbol, "status": security.status}
            for security in securities
        ],
        "security_id": None,
        "security_notice": "须确认交易证券或衍生品标的与具体股类的对应关系。",
    }


def resolve_amendment(
    s: Session, relation_id: str, action: str, original_event_id: str | None, evidence
) -> dict:
    """Apply a documented row-level decision, preserving both source observations."""
    aliases = {
        "additional": "ADD",
        "new": "ADD",
        "add": "ADD",
        "replace": "REPLACE",
        "unchanged": "UNCHANGED",
        "no_change": "UNCHANGED",
        "withdraw": "REMOVE",
        "remove": "REMOVE",
    }
    chosen = aliases.get(action.lower(), action.upper())
    if chosen not in {"ADD", "REPLACE", "UNCHANGED", "REMOVE"}:
        raise ValueError("请选择新增、替换、未变化或撤回。")
    if isinstance(evidence, str):
        evidence = {"note": evidence}
    if (
        not isinstance(evidence, dict)
        or not str(evidence.get("note", evidence.get("description", ""))).strip()
    ):
        raise ValueError("请保存逐行核对的依据说明。")
    relation = s.scalar(
        select(AmendmentRelation).where(AmendmentRelation.id == relation_id).with_for_update()
    )
    if relation is None:
        raise ValueError("待核对修订关系不存在。")
    if relation.action not in {"UNCONFIRMED", "UNCONFIRMED_DUPLICATE"}:
        if relation.action == chosen and relation.original_event_id == original_event_id:
            return {"id": relation.id, "action": relation.action, "reused": True}
        raise ValueError("该修订已经有明确结论，不能覆盖已有核对证据。")
    amended = s.get(TransactionEvent, relation.amended_event_id)
    if amended:
        s.scalar(select(Issuer).where(Issuer.id == amended.issuer_id).with_for_update())
    event_ids = sorted({value for value in [relation.amended_event_id, original_event_id] if value})
    locked = {
        event.id: event
        for event in s.scalars(
            select(TransactionEvent)
            .where(TransactionEvent.id.in_(event_ids))
            .order_by(TransactionEvent.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    amended = locked.get(relation.amended_event_id)
    original = locked.get(original_event_id)
    if not amended or (chosen != "ADD" and not original):
        raise ValueError("该操作需要指定有效的原交易行。")
    if original and (
        original.id == amended.id
        or original.issuer_id != amended.issuer_id
        or original.status not in VISIBLE
        or not set(original.owner_ids) & set(amended.owner_ids)
    ):
        raise ValueError("原行必须是同发行人且具有共同申报主体的现行交易。")
    changed = {(amended.issuer_id, _group_date(amended))}
    if original:
        changed.add((original.issuer_id, _group_date(original)))
    if chosen == "ADD":
        amended.status = "CURRENT"
        if relation.action == "UNCONFIRMED_DUPLICATE" and original:
            original.status = "CURRENT"
    elif chosen == "REPLACE":
        original.status, amended.status = "SUPERSEDED", "CURRENT"
        amended.replaces_id = original.id
        # Correct the original disclosure group without counting the amendment
        # as a second economic transaction on its acceptance day.
        amended.data = {
            **amended.data,
            "original_accepted_at": _json(
                original.data.get("original_accepted_at") or original.accepted_at
            ),
            "original_accession": original.data.get("original_accession", original.accession),
        }
    elif chosen == "UNCHANGED":
        amended.status, original.status = "DUPLICATE_CONFIRMED", "CURRENT"
    else:
        amended.status, original.status = "WITHDRAWN", "WITHDRAWN"
    relation.original_event_id = original.id if original else None
    relation.evidence = {
        **relation.evidence,
        "resolution": {
            **evidence,
            "recorded_at": now().isoformat(),
            "original_event_id": original.id if original else None,
            "action": chosen,
        },
    }
    relation.action = chosen
    s.flush()
    _refresh_groups(s, changed)
    return {
        "id": relation.id,
        "action": chosen,
        "amended_event_id": amended.id,
        "original_event_id": relation.original_event_id,
        "reused": False,
    }


def _quarter_ranges(start: date, end: date) -> list[tuple[date, date]]:
    current = date(start.year, ((start.month - 1) // 3) * 3 + 1, 1)
    result = []
    while current <= end:
        following = (
            date(current.year + 1, 1, 1)
            if current.month == 10
            else date(current.year, current.month + 3, 1)
        )
        result.append((max(start, current), min(end, following - timedelta(days=1))))
        current = following
    return list(reversed(result))


def _sec_recent_days(end: date, count=5) -> list[date]:
    # SEC closes on federal holidays. This calendar is independent of NYSE;
    # an exceptional closure still produces an explicit missing-index gap.
    from pandas.tseries.holiday import USFederalHolidayCalendar

    holidays = {
        value.date()
        for value in USFederalHolidayCalendar().holidays(start=end - timedelta(days=30), end=end)
    }
    result, current = [], end
    while len(result) < count:
        if current.weekday() < 5 and current not in holidays:
            result.append(current)
        current -= timedelta(days=1)
    return result


def plan_sec_scope(
    s: Session, scope, batch, capacity: list[int], latest_capacity: list[int] | None = None,
    document_link_capacity: list[int] | None = None,
) -> None:
    """Roll a SEC scope through discovery and each durable document, within budget.

    Discovery manifests are persisted independently of document jobs. A full
    queue merely defers unscheduled filings; failed filings stay visible and are
    never mistaken for parsed ones. Latest checks and recent daily indexes run
    first, followed by quarterly history. All-market discovery is necessary even
    for a single company/person because an index's CIK is not an issuer relation.
    """
    from iirp.lifecycle import add_job, digest

    if batch.requested_action or batch.status in {
        "PAUSED",
        "CANCELLED",
        "PAUSE_REQUESTED",
        "CANCEL_REQUESTED",
    }:
        return
    if not isinstance(capacity, list) or len(capacity) != 1 or type(capacity[0]) is not int:
        raise ValueError("SEC 调度容量须为单项整数列表。")
    s.flush()
    jobs = list(
        s.scalars(
            select(Job)
            .join(BatchJob, BatchJob.job_id == Job.id)
            .where(BatchJob.scope_id == scope.id, BatchJob.active.is_(True))
        )
    )
    by_key = {job.idempotency_key: job for job in jobs}
    started, endpoint = scope.start_date, scope.end_date
    if not started or not endpoint:
        raise ValueError("SEC 批次必须具有冻结的起止日期。")
    scan_start = max(started, _date((scope.checkpoint or {}).get("scan_start_date")) or started)
    initial_latest = batch.kind == "sec_latest"
    if initial_latest:
        # Older saved scopes may predate latest-only collection. Keep this chain on its day.
        endpoint = batch.created_at.astimezone(ET).date()
        started = _sec_recent_days(endpoint, 2)[-1]
        scan_start = started
    week = batch.created_at.astimezone(ET).strftime("%G-W%V")
    reconcile_documents = bool(
        batch.params.get("reconcile_documents") or batch.params.get("intent") == "refresh"
    )
    round_key = batch.created_at.replace(second=0, microsecond=0).isoformat()
    # Discovery and publication need separate bounded queue places. Thousands
    # of saved pagination jobs must not prevent an already discovered filing
    # from receiving its first document job. Count jobs, not subscriptions, so
    # sharing a document across latest rounds does not use capacity twice.
    document_capacity = latest_capacity if latest_capacity is not None else capacity
    document_pending = ("QUEUED", "RUNNING", "RETRY_WAIT")
    if initial_latest and batch.trigger != "manual":
        queued_documents = s.scalar(select(func.count()).select_from(Job).where(
            Job.kind == "sec_document", Job.status.in_(document_pending), Job.priority <= 0,
        ))
        document_capacity = [max(0, 8 - queued_documents)]
    # Manual latest keeps the caller's reserved foreground allowance. This
    # creates no extra execution slots and does not bypass the SEC limiter.

    def ensure(kind, target, priority):
        key = digest([kind, target])
        if key in by_key:
            return by_key[key]
        existing = s.scalar(
            select(Job).where(Job.idempotency_key == key).order_by(Job.created_at.desc()).limit(1)
        )
        if initial_latest and kind == "sec_document":
            budget = document_capacity
        elif initial_latest and latest_capacity is not None and kind == "sec_discover":
            budget = latest_capacity
        else:
            budget = capacity
        if existing is None and budget[0] <= 0:
            head_check = initial_latest and kind == "sec_discover" and not target.get("cursor")
            active_head = s.scalar(select(Job.id).where(
                Job.kind == "sec_discover", Job.target["mode"].astext == "latest",
                Job.target["cursor"].astext.is_(None), Job.status.in_(ACTIVE),
                Job.target["end_date"].astext == target.get("end_date"),
            ).limit(1)) if head_check else None
            # A promoted document backlog must never consume the one head-check slot.
            if not head_check or active_head:
                return None
        job = add_job(s, scope, kind, target, priority)
        if existing is None:
            budget[0] -= 1
        by_key[key] = job
        jobs.append(job)
        return job

    scan_targets = []
    if initial_latest:
        saved_target = (scope.checkpoint or {}).get("latest_target")
        shared_head = s.scalar(select(Job).where(
            Job.kind == "sec_discover", Job.status.in_(ACTIVE),
            Job.target["mode"].astext == "latest", Job.target["cursor"].astext.is_(None),
            Job.target["end_date"].astext == str(endpoint),
        ).order_by(Job.created_at.desc()).limit(1)) if not saved_target else None
        # A new reader can share an unfinished same-day check. Saved rounds
        # retain their exact request parameters and continuation chain.
        target = dict(saved_target or (shared_head.target if shared_head else {})) or {
            "mode": "latest",
            "start_date": str(started),
            "end_date": str(endpoint),
            "max_pages": 1,
            "round": round_key,
        }
        # A previously complete *scan*, not the newest filing timestamp, can
        # establish a continuity watermark for the next overlapping feed scan.
        previous = s.scalar(
            select(Job)
            .where(
                Job.kind == "sec_discover",
                Job.status == "SUCCEEDED",
                Job.target["mode"].astext == "latest",
                Job.checkpoint["sec_scan"]["complete"].astext == "true",
                Job.created_at < batch.created_at,
            )
            .order_by(Job.created_at.desc())
            .limit(1)
        )
        if previous and not saved_target and shared_head is None:
            watermark = previous.checkpoint.get("sec_scan", {}).get("newest_accepted_at")
            if watermark:
                target["watermark"] = watermark
        scope.checkpoint = {**(scope.checkpoint or {}), "latest_target": target}
        scan_targets.append((target, 0, True))
    # Quarterly manifests are filtered to the frozen range and newest quarter first.
    # The independent latest chain handles today's feed; don't enqueue redundant daily
    # scans before publishing the newest historical documents.
    for begin, end in ([] if initial_latest else _quarter_ranges(scan_start, endpoint)):
        scan_targets.append(
            (
                {
                    "mode": "quarterly",
                    "start_date": str(begin),
                    "end_date": str(end),
                    "max_pages": 1,
                    "reconcile_removals": True,
                    "reconcile_documents": reconcile_documents,
                    "round": week,
                },
                20,
                True,
            )
        )
    required_scans, unscheduled_scans = [], 0
    for target, priority, required in scan_targets:
        job = ensure("sec_discover", target, priority)
        if required:
            if job is None:
                unscheduled_scans += 1
            else:
                required_scans.append(job)

    discovered_latest = {
        accession
        for job in jobs
        if job.kind == "sec_discover"
        for accession in job.checkpoint.get("discovered_accessions", [])
    }
    condition = Filing.filing_date.between(scan_start, endpoint)
    if discovered_latest:
        condition = or_(condition, Filing.accession.in_(discovered_latest))
    filings = list(
        s.scalars(
            select(Filing)
            .where(condition, Filing.visible.is_(True))
            .order_by(
                Filing.accepted_at.desc().nulls_last(),
                Filing.filing_date.desc().nulls_last(),
                Filing.accession.desc(),
            )
        )
    )
    missing = [filing for filing in filings if not filing.current_version]
    scheduled_accessions = {
        job.target.get("accession") for job in jobs if job.kind == "sec_document"
    }
    unscheduled_documents = 0
    # A refresh must acquire first documents without dropping source revision
    # checks. Newer parsed filings otherwise consume every bounded queue wave
    # before older DISCOVERED records. Seven first acquisitions followed by one
    # recheck keeps both flows moving; each group retains accepted-time order.
    if reconcile_documents:
        rechecks = iter(filing for filing in filings if filing.current_version)
        document_candidates = []
        for offset in range(0, len(missing), 7):
            document_candidates.extend(missing[offset:offset + 7])
            recheck = next(rechecks, None)
            if recheck is not None:
                document_candidates.append(recheck)
        document_candidates.extend(rechecks)
    else:
        document_candidates = missing
    link_budget = document_link_capacity if document_link_capacity is not None else [100]
    for document_index, filing in enumerate(document_candidates):
        if filing.accession in scheduled_accessions:
            continue
        if link_budget[0] <= 0:
            # Linking shared jobs also writes rows. Bound those writes so a large
            # existing backlog cannot hold the worker's completion locks for a full scan.
            unscheduled_documents += sum(
                value.accession not in scheduled_accessions
                for value in document_candidates[document_index:]
            )
            break
        link_budget[0] -= 1
        # Share another batch's existing filing operation even when it has a
        # different discovery URL alias or this round has no new queue capacity.
        shared_query = select(Job).where(
            Job.kind == "sec_document", Job.target["accession"].astext == filing.accession
        )
        if reconcile_documents:
            shared_query = shared_query.where(Job.target["reconcile_round"].astext == week)
        shared = s.scalar(shared_query.order_by(Job.created_at.desc()).limit(1))
        if shared:
            counted = shared.priority <= 0 and shared.status in document_pending
            add_job(s, scope, shared.kind, shared.target, 0 if initial_latest else 30)
            if (initial_latest and batch.trigger != "manual" and not counted
                    and shared.status in document_pending):
                # Reuse remains possible with a full queue. Promoting existing
                # work consumes the allowance for additional new downloads.
                document_capacity[0] -= 1
            jobs.append(shared)
            scheduled_accessions.add(filing.accession)
            continue
        target = {
            "accession": filing.accession,
            "form": filing.form,
            "index_url": filing.index_url,
            "filing_date": _json(filing.filing_date),
            "accepted_at": _json(filing.accepted_at),
        }
        if reconcile_documents:
            target["reconcile_round"] = week
        if filing.index_url.endswith(("-index.html", "-index.htm")):
            target["submission_url"] = filing.index_url.rsplit("-index.", 1)[0] + ".txt"
        if ensure("sec_document", target, 0 if initial_latest else 30) is None:
            # A full queue must not trigger one database lookup per unscheduled
            # filing. Preserve the remaining count and resume this order later.
            unscheduled_documents += sum(
                value.accession not in scheduled_accessions
                for value in document_candidates[document_index:]
            )
            break
        else:
            scheduled_accessions.add(filing.accession)
    s.flush()
    active = [job for job in jobs if job.status in ACTIVE]
    failures = [job for job in jobs if job.status in {"FAILED", "PARTIAL", "CANCELLED"}]
    # An index scope consists of a single quarter per task, so there is no
    # ambiguous “last page succeeded” shortcut across several partial indexes.
    scans_done = (
        not unscheduled_scans
        and bool(required_scans)
        and all(
            job.status == "SUCCEEDED" and (
                job.checkpoint.get("sec_scan", {}).get("complete")
                or (initial_latest and any(
                    tail.kind == "sec_discover" and tail.status == "SUCCEEDED"
                    and tail.target.get("round") == job.target.get("round")
                    and tail.checkpoint.get("sec_scan", {}).get("complete")
                    for tail in jobs
                ))
            )
            for job in required_scans
        )
    )
    # Partial Latest/daily discovery can be superseded by a complete quarterly
    # manifest, while genuine document errors always keep the scope incomplete.
    relevant_failures = [
        job
        for job in failures
        if job.kind == "sec_document" or job in required_scans or not scans_done
    ]
    recent_count = batch.params.get("recent_count") or 0
    count = None
    if recent_count:
        statement = (
            select(TransactionEvent)
            .join(Filing, Filing.accession == TransactionEvent.accession)
            .where(TransactionEvent.status == "CURRENT", Filing.visible.is_(True))
        )
        if batch.params.get("issuer_id"):
            statement = statement.where(
                TransactionEvent.issuer_id == _cik(batch.params["issuer_id"])
            )
        if batch.params.get("owner_id"):
            statement = statement.where(
                TransactionEvent.owner_ids.contains([_cik(batch.params["owner_id"])])
            )
        date_basis = batch.params.get("date_basis", "transaction")
        ordering = (
            TransactionEvent.accepted_at
            if date_basis in {"accepted", "accepted_at", "accepted_date"}
            else TransactionEvent.transaction_date
        )
        if date_basis in {"accepted", "accepted_at", "accepted_date"}:
            statement = statement.where(
                func.timezone("America/New_York", TransactionEvent.accepted_at).cast(Date).between(started, endpoint)
            )
        else:
            statement = statement.where(TransactionEvent.transaction_date.between(started, endpoint))
        selected = list(
            s.scalars(
                statement.order_by(ordering.desc().nulls_last(), TransactionEvent.id.desc()).limit(
                    recent_count
                )
            )
        )
        count = len(selected)
        # Stop at the requested boundary even when fewer than N records exist.
        scope.checkpoint = {
            **(scope.checkpoint or {}),
            "history_boundary_reached": scans_done and not missing and count < recent_count,
        }
    successful_rechecks = {
        job.target.get("accession")
        for job in jobs
        if job.kind == "sec_document"
        and job.target.get("reconcile_round") == week
        and job.status == "SUCCEEDED"
    }
    scope.checkpoint = {
        **(scope.checkpoint or {}),
        "scan_start_date": str(scan_start),
        "scan_done": scans_done,
        "discovered_count": len(filings),
        "parsed_count": len(filings) - len(missing),
        "remaining_documents": len(missing),
        "unscheduled_documents": unscheduled_documents,
        "reconcile_documents": reconcile_documents,
        "reconcile_round": week if reconcile_documents else None,
        "remaining_rechecks": sum(
            1
            for filing in filings
            if reconcile_documents
            and filing.current_version
            and filing.accession not in successful_rechecks
        ),
        "requested_count": recent_count or None,
        "observed_count": count,
        "discovery_jobs": len([job for job in jobs if job.kind == "sec_discover"]),
    }
    if active:
        scope.status, scope.wait_reason = (
            "RUNNING",
            next((job.error for job in active if job.error), None),
        )
    elif unscheduled_scans or unscheduled_documents:
        scope.status, scope.wait_reason = (
            "QUEUED",
            "继续处理剩余清单与原文；队列容量仅控制执行节奏。",
        )
    elif relevant_failures or missing or not scans_done:
        scope.status = "PARTIAL"
        scope.wait_reason = next(
            (job.error for job in relevant_failures if job.error),
            "发现、原文或索引范围仍有缺口，可重试失败项。",
        )
    else:
        scope.status, scope.wait_reason = "READY", None
