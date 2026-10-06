"""Persist SEC discoveries and documents as filings, versions and row-level transactions."""

import base64
import json
from dataclasses import asdict
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from iirp.insider.common import PARSER_VERSION, VISIBLE, _date, _group_date, _instant, _json, _uid
from iirp.insider.tickers import normalized_ticker
from iirp.insider.views import _refresh_groups
from iirp.models import (
    AmendmentRelation,
    CoverageSegment,
    Filing,
    FilingOwner,
    FilingVersion,
    Issuer,
    Owner,
    SourceObservation,
    TransactionEvent,
)
from iirp.sec.ownership import parse_ownership_xml
from iirp.sec.parse import OWNERSHIP_FORMS, validate_sec_url


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
    # The original XML stays in the saved filing document (source_objects).
    data.pop("raw_xml", None)
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
    # The XML itself is the saved filing document referenced in source_objects;
    # the parsed facts do not keep further copies of it.
    data.pop("raw_xml", None)
    for item in data.get("rows", []):
        item.pop("raw_xml", None)
    data.update(
        {"source_metadata": {key: value for key, value in raw.items() if key != "xml_payload"},
         "xml_sha256": sha256(xml).hexdigest(), "source_objects": sources}
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
