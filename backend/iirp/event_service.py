"""Reviewed event research. GETs are local; commands create durable existing jobs."""

import csv
import io
import json
from datetime import date

from sqlalchemy import inspect, select

from iirp.analytics.calendar import (
    ET,
    calendar_version,
    last_completed_session,
    previous_session,
)
from iirp.analytics.event_dates import CALCULATION_VERSION, event_price_scope
from iirp.analytics.event_overlaps import REPRESENTATION_VERSION
from iirp.business_models import (
    AnalysisRequest,
    AnalysisResult,
    Batch,
    RequestScope,
    Security,
)
from iirp.db import session
from iirp.event_contracts import (
    CustomEventsImport,
    EarningsEventsImport,
    import_warnings,
    parse_event_import,
)
from iirp.event_models import EventCommandReceipt, EventImportPreview, EventSet, EventSetVersion
from iirp.event_overlap_reads import get_overlap_page  # noqa: F401
from iirp.market_data import digest
from iirp.models import ACTIVE, now

MIC_EXCHANGES = {
    "XNAS": {"NMS", "NGM", "NCM", "NASDAQ", "XNAS"},
    "XNYS": {"NYQ", "NYSE", "XNYS"},
    "XASE": {"ASE", "AMEX", "XASE"},
    "ARCX": {"PCX", "ARCX"},
    "BATS": {"BTS", "BATS"},
}


def _lock(s, value):
    from iirp.lifecycle import advisory

    advisory(s, ["events", value])


def _required(s, model, identifier):
    row = s.get(model, identifier)
    if row is None:
        raise LookupError("找不到对应的事件资料或分析")
    return row


def _version(s, collection, number=None):
    number = collection.version if number is None else number
    row = s.scalar(
        select(EventSetVersion).where(
            EventSetVersion.set_id == collection.id, EventSetVersion.version == number
        )
    )
    if row is None:
        raise LookupError("事件集版本不存在")
    return row


def _candidate(row):
    return {
        "id": row.id,
        "symbol": row.symbol,
        "name": row.name,
        "currency": row.currency,
        "exchange": row.exchange,
        "status": row.status,
        "calendar": row.calendar,
    }


def _excluded_title_terms(event, scope):
    title = event["event_name"].casefold()
    return [term for term in scope.get("exclude_keywords", []) if term.casefold() in title]


def preview_import(values):
    parsed = parse_event_import(values["text"])
    document = parsed.model_dump(mode="json")
    with session() as s, s.begin():
        parent = _required(s, EventSet, values["set_id"]) if values.get("set_id") else None
        expected = values.get("expected_version")
        if parent and expected != parent.version:
            raise RuntimeError("事件集已更新，请读取最新版本后重新预览")
        if (
            parent
            and ("earnings" if isinstance(parsed, EarningsEventsImport) else "custom")
            != parent.kind
        ):
            raise ValueError("修订不能把财报事件集改成普通活动，或反向替换")
        candidates = (
            s.scalars(
                select(Security).where(Security.symbol == document["company"]["ticker"])
            ).all()
            if document["company"]["ticker"]
            else []
        )
        warnings = import_warnings(parsed)
        scope = document["scope"]
        if scope.get("include_keywords") or scope.get("exclude_keywords"):
            warnings.append(
                "纳入关键词仅指导 AI 查找候选，不能代替逐项人工核对；排除关键词按事件名称检查，"
                "命中项必须取消选入并填写理由，原事实仍保留。关键词不会静默删除事件。"
            )
            for event in document["events"]:
                matches = _excluded_title_terms(event, scope)
                if matches:
                    warnings.append(
                        f"{event['event_name']}：名称命中排除关键词 {', '.join(matches)}；保存前须取消选入并填写理由"
                    )
        if not candidates:
            warnings.append("本地尚无匹配证券；确认后先核对证券身份，再获取行情")
        elif len(candidates) > 1:
            warnings.append("本地有多个证券候选，保存前必须选择具体证券")
        candidate_ids = [row.id for row in candidates]
        prior_sets = s.scalars(
            select(EventSet).where(EventSet.security_id.in_(candidate_ids))
        ).all()
        incoming = {
            (event["event_type"], event["event_name"].casefold().strip(), event["event_date"])
            for event in document["events"]
        }
        for item in prior_sets:
            old = _version(s, item)
            overlaps = [
                e["event_name"]
                for e in old.events
                if (e["event_type"], e["event_name"].casefold().strip(), e["event_date"])
                in incoming
            ]
            if overlaps and (not parent or item.id != parent.id):
                warnings.append(
                    f"与事件集「{item.title}」存在疑似重复：{'、'.join(overlaps[:5])}；请核对新增还是修订"
                )
        preview = EventImportPreview(
            raw_text=values["text"],
            content_hash=digest(document),
            document=document,
            warnings=warnings,
            set_id=parent.id if parent else None,
            expected_version=expected,
        )
        s.add(preview)
        s.flush()
        return {
            "preview_id": preview.id,
            "content_hash": preview.content_hash,
            "document": document,
            "warnings": warnings,
            "candidates": [_candidate(row) for row in candidates],
            "set_id": preview.set_id,
            "expected_version": preview.expected_version,
            "events": [
                {
                    **event,
                    "date_verified": False,
                    "time_verified": False,
                    "period_verified": False,
                    "excluded": False,
                }
                for event in document["events"]
            ],
        }


def _review(document, reviews):
    indexed = {item["client_event_id"]: item for item in reviews}
    identifiers = {event["client_event_id"] for event in document["events"]}
    if len(indexed) != len(reviews) or set(indexed) != identifiers:
        raise ValueError("必须逐项保留所有事件的选择/排除记录，ID 不能重复或缺少")
    output = []
    stamp = now().isoformat()
    for event in document["events"]:
        review = indexed[event["client_event_id"]]
        selected = review.get("selected", False)
        supported = {field for source in event["sources"] for field in source["supports"]}
        date_verified = review.get("date_verified", False)
        time_verified = review.get("time_verified", False)
        period_verified = review.get("period_verified", False)
        if date_verified and (
            not event["event_date"]
            or event["date_status"] != "supported"
            or "event_date" not in supported
        ):
            raise ValueError(f"{event['event_name']}：日期缺失/冲突或无依据，不能确认日期已核对")
        if time_verified:
            actual_minute = (
                event["event_time"] is not None and event["time_basis"] == "reported_actual"
            )
            actual_session = (
                event["event_type"] == "earnings_release"
                and event["event_time"] is None
                and event.get("release_session", "unknown") != "unknown"
                and "release_session" in supported
            )
            if (
                not date_verified
                or event["event_status"] != "occurred"
                or not (actual_minute or actual_session)
            ):
                raise ValueError(f"{event['event_name']}：仅日期或官方日程不能确认实际时刻已核对")
        if period_verified and (
            event["event_type"] != "earnings_release"
            or not event.get("fiscal_year")
            or not event.get("fiscal_quarter")
            or not {"fiscal_year", "fiscal_quarter"}.issubset(supported)
        ):
            raise ValueError(f"{event['event_name']}：财年/财季缺少对应依据，不能确认财期已核对")
        if period_verified and event.get("period_kind") == "regular" and "period_kind" not in supported:
            raise ValueError(
                f"{event['event_name']}：常规财期缺少支持 period_kind 的来源，"
                "不能确认常规财期资格；请补充依据或保留财期类型未知"
            )
        if not selected and not review.get("note", "").strip():
            raise ValueError(f"{event['event_name']}：请保留未选入分析的原因")
        output.append(
            {
                **event,
                "date_verified": date_verified,
                "time_verified": time_verified,
                "period_verified": period_verified,
                "excluded": not selected,
                "review": {
                    "method": "user_confirmed_sources"
                    if any((date_verified, time_verified, period_verified))
                    else "user_selection",
                    "confirmed_at": stamp,
                    "note": review.get("note", ""),
                    "selected": selected,
                },
            }
        )
    return output


def _choose_security(s, document, security_id, parent=None):
    from iirp.lifecycle import advisory

    company = document["company"]
    if company["ticker"]:
        # Share the collection planner's identity lock across different imports.
        advisory(s, ["security", company["ticker"]])
    matches = s.scalars(select(Security).where(Security.symbol == company["ticker"])).all()
    if security_id:
        security = _required(s, Security, security_id)
        if company["ticker"] and security.symbol != company["ticker"]:
            raise ValueError("所选证券 ticker 与导入资料不一致，请修订资料后重新预览")
    elif len(matches) == 1:
        security = matches[0]
    elif matches:
        raise ValueError("存在多个证券候选，请明确选择证券")
    else:
        if not company["ticker"]:
            raise ValueError("ticker 尚未确定，须先在证券页面确认具体证券")
        security = Security(symbol=company["ticker"])
        s.add(security)
        s.flush()
    mic = company.get("exchange_mic")
    if (
        security.status == "VERIFIED"
        and mic
        and security.exchange not in MIC_EXCHANGES.get(mic, set())
    ):
        raise ValueError("所选证券交易所与导入资料不一致，请核对股类和证券身份")
    if parent and parent.security_id != security.id:
        raise ValueError("同一事件集的修订不能替换证券，请另建事件集")
    return security


def _store_event_version(s, version):
    """Assemble large notes once, then restore both complete frozen views.

    The new row and text restoration are in the caller's confirmation
    transaction. No partial version is visible and existing versions are never
    rewritten. The threshold chooses transport, not an accepted-input limit.
    """
    from sqlalchemy import text
    from sqlalchemy.orm.attributes import set_committed_value

    events, reviews = version.events, version.reviews
    positions = {event["client_event_id"]: i for i, event in enumerate(events)}
    large = []
    for i, review in enumerate(reviews):
        note = review.get("note", "")
        index = positions[review["client_event_id"]]
        if (isinstance(note, str) and len(note) > 1024**2
                and events[index]["review"].get("note") == note):
            large.append((index, i, note))
    if large:
        event_indices = {index for index, _, _ in large}
        review_indices = {index for _, index, _ in large}
        version.events = [
            {**event, "review": {**event["review"], "note": ""}}
            if i in event_indices else event for i, event in enumerate(events)
        ]
        version.reviews = [
            {**review, "note": ""} if i in review_indices else review
            for i, review in enumerate(reviews)
        ]
    s.add(version)
    s.flush()
    if large:
        # Temporary rows keep transport bounded without repeatedly rebuilding
        # an ever-growing JSONB value. DDL/data roll back with confirmation;
        # ON COMMIT DROP also prevents state leaking through the pooled session.
        s.execute(text("""CREATE TEMP TABLE iirp_event_note_chunks
            (ordinal integer NOT NULL, value text NOT NULL) ON COMMIT DROP"""))
    for event_index, review_index, note in large:
        # At most 256 KiB UTF-8 per parameter, including four-byte characters.
        # This is a transport size, never truncation or an accepted-input limit.
        for start in range(0, len(note), 64 * 1024):
            s.execute(text("""INSERT INTO pg_temp.iirp_event_note_chunks
                (ordinal, value) VALUES (:ordinal, :value)"""), {
                    "ordinal": start, "value": note[start:start + 64 * 1024],
                })
        s.execute(text("""UPDATE event_set_version SET
            events = jsonb_set(events, CAST(:event_path AS text[]),
                (SELECT to_jsonb(string_agg(value, '' ORDER BY ordinal))
                 FROM pg_temp.iirp_event_note_chunks))
            WHERE id = :id"""), {
                "id": version.id, "event_path": [str(event_index), "review", "note"],
            })
        # Copy the complete scalar inside PG; do not transmit/assemble it again.
        s.execute(text("""UPDATE event_set_version SET
            reviews = jsonb_set(reviews, CAST(:review_path AS text[]),
                events #> CAST(:event_path AS text[]))
            WHERE id = :id"""), {
                "id": version.id, "event_path": [str(event_index), "review", "note"],
                "review_path": [str(review_index), "note"],
            })
        s.execute(text("TRUNCATE pg_temp.iirp_event_note_chunks"))
    if large:
        s.execute(text("DROP TABLE pg_temp.iirp_event_note_chunks"))
        # The SQL has restored exactly these original values; avoid a second
        # ORM UPDATE or a full decode merely to refresh the identity map.
        set_committed_value(version, "events", events)
        set_committed_value(version, "reviews", reviews)


def confirm_import(values):
    command_hash = digest(values)
    with session() as s, s.begin():
        _lock(s, ["command", values["request_id"]])
        receipt = s.get(EventCommandReceipt, values["request_id"])
        if receipt:
            if receipt.content_hash != command_hash:
                raise RuntimeError("同一 request_id 不能提交不同内容")
            return {**receipt.response, "reused": True}
        preview = _required(s, EventImportPreview, values["preview_id"])
        document = preview.document
        _lock(s, ["document", preview.content_hash])
        parent = None
        if preview.set_id:
            parent = s.scalar(
                select(EventSet).where(EventSet.id == preview.set_id).with_for_update()
            )
            if (
                parent.version != preview.expected_version
                or values.get("expected_version") != parent.version
            ):
                raise RuntimeError("事件集版本冲突；旧预览不能覆盖新版本")
            if not values.get("revision_note", "").strip():
                raise ValueError("修订必须填写变更说明")
        reviewed = _review(document, values["reviews"])
        for event in reviewed:
            matches = _excluded_title_terms(event, document["scope"])
            if matches and not event["excluded"]:
                raise ValueError(
                    f"{event['event_name']}：名称命中排除关键词 {', '.join(matches)}；"
                    "请取消选入、填写理由，或修订关键词范围"
                )
        security = _choose_security(s, document, values.get("security_id"), parent)
        # Confirmation time is evidence, not a reason to duplicate identical content.
        revision_hash = digest([preview.content_hash, security.id, values["reviews"]])
        duplicate = s.scalar(
            select(EventSetVersion)
            .join(EventSet, EventSet.id == EventSetVersion.set_id)
            .where(
                EventSetVersion.content_hash == revision_hash,
                EventSet.security_id == security.id,
                EventSet.id == parent.id
                if parent
                else EventSet.kind
                == (
                    "earnings"
                    if document["schema_version"] == "iirp.earnings-events.v1"
                    else "custom"
                ),
            )
            .order_by(EventSetVersion.created_at.desc())
            .limit(1)
        )
        if duplicate:
            version = duplicate
            collection = _required(s, EventSet, version.set_id)
        else:
            previous_events = {
                event["client_event_id"]: event for event in _version(s, parent).events
            } if parent else {}
            # Raw import timestamps are system observations, not release times.
            # Keep first-seen across revisions; legacy missing chronology stays
            # unknown instead of adopting the latest review/preview timestamp.
            for item in reviewed:
                prior = previous_events.get(item["client_event_id"])
                item["first_observed_at"] = (
                    prior.get("first_observed_at") if prior is not None
                    else preview.created_at.isoformat()
                )
                item["last_observed_at"] = preview.created_at.isoformat()
            collection = parent or EventSet(
                security_id=security.id,
                title=(values.get("title") or f"{document['company']['name']} · 事件日期观察")[
                    :200
                ],
                kind="earnings"
                if document["schema_version"] == "iirp.earnings-events.v1"
                else "custom",
            )
            if parent:
                collection.version += 1
                collection.updated_at = now()
            else:
                s.add(collection)
                s.flush()
            version = EventSetVersion(
                set_id=collection.id,
                version=collection.version,
                preview_id=preview.id,
                content_hash=revision_hash,
                document=document,
                events=reviewed,
                reviews=values["reviews"],
                revision_note=values.get("revision_note", ""),
            )
            _store_event_version(s, version)
        result = {
            "set_id": collection.id,
            "version": version.version,
            "version_id": version.id,
            "reused": duplicate is not None,
        }
        if values.get("analyze"):
            result["analysis"] = _create_analysis(
                s,
                collection,
                {
                    "request_id": values["request_id"] + ":analysis",
                    "version": version.version,
                },
            )
        s.add(
            EventCommandReceipt(
                request_id=values["request_id"],
                content_hash=command_hash,
                response=result,
            )
        )
        return result


def _set_view(s, collection, version=None, details=True):
    revision = _version(s, collection, version)
    security = _required(s, Security, collection.security_id)
    result = {
        "id": collection.id,
        "title": collection.title,
        "kind": collection.kind,
        "version": revision.version,
        "latest_version": collection.version,
        "version_id": revision.id,
        "security": _candidate(security),
        "created_at": collection.created_at.isoformat(),
        "updated_at": collection.updated_at.isoformat(),
        "research_as_of": revision.document["research_as_of"],
        "event_count": len(revision.events),
        "selected_count": sum(not e["excluded"] for e in revision.events),
        "verified_count": sum(e["date_verified"] and not e["excluded"] for e in revision.events),
        "revision_note": revision.revision_note,
    }
    if details:
        preview = _required(s, EventImportPreview, revision.preview_id)
        versions = s.scalars(
            select(EventSetVersion)
            .where(EventSetVersion.set_id == collection.id)
            .order_by(EventSetVersion.version.desc())
        ).all()
        analyses = s.scalars(
            select(AnalysisRequest)
            .where(AnalysisRequest.params["event_set_id"].astext == collection.id)
            .order_by(AnalysisRequest.created_at.desc())
        ).all()
        result.update(
            {
                "document": revision.document,
                "events": revision.events,
                "reviews": revision.reviews,
                "raw_text": preview.raw_text,
                "content_hash": revision.content_hash,
                "warnings": preview.warnings,
                "versions": [
                    {
                        "version": row.version,
                        "id": row.id,
                        "created_at": row.created_at.isoformat(),
                        "revision_note": row.revision_note,
                    }
                    for row in versions
                ],
                "analyses": [
                    {
                        "id": row.id,
                        "batch_id": row.batch_id,
                        "params": row.params,
                        "created_at": row.created_at.isoformat(),
                    }
                    for row in analyses
                ],
            }
        )
    return result


def list_sets(kind=None):
    with session() as s:
        query = select(EventSet).order_by(EventSet.updated_at.desc())
        if kind:
            query = query.where(EventSet.kind == kind)
        return {"items": [_set_view(s, row, details=False) for row in s.scalars(query).all()]}


def get_set(identifier, version=None):
    with session() as s:
        return _set_view(s, _required(s, EventSet, identifier), version)


def _create_analysis(s, collection, values):
    for field in ("years", "excluded_years"):
        chosen = values.get(field) or []
        if len(chosen) != len(set(chosen)):
            raise ValueError(f"{field} 不能包含重复年份，避免重复计算覆盖分母")
    request_id = values["request_id"]
    _lock(s, ["analysis", request_id])
    revision = _version(s, collection, values.get("version"))
    security = _required(s, Security, collection.security_id)
    cutoff = last_completed_session(calendar=security.calendar or "XNYS")
    if values.get("cutoff_date"):
        cutoff = previous_session(
            min(cutoff, date.fromisoformat(values["cutoff_date"])),
            security.calendar or "XNYS",
            inclusive=True,
        )
    compatible_values = {
        key: value
        for key, value in values.items()
        if not (
            (key == "date_window" and value == "after5")
            or (key == "date_category" and value is None)
        )
    }
    fingerprint = digest([collection.id, revision.id, compatible_values])
    existing = s.scalar(select(Batch).where(Batch.request_id == request_id))
    if existing:
        if existing.scope_key not in {fingerprint, digest([collection.id, revision.id, values])}:
            raise RuntimeError("同一分析 request_id 不能替换参数")
        request = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == existing.id))
        return {"analysis_id": request.id, "batch_id": existing.id, "reused": True}
    from iirp.analytics.research import _current_fiscal_year

    fiscal = values.get("current_fiscal_year") or _current_fiscal_year(
        {"fiscal_year_end_mmdd": security.metadata_json.get("verified_fiscal_year_end")},
        now().astimezone(ET).date(),
    )
    scope_doc = revision.document["scope"]
    current = fiscal if collection.kind == "earnings" else now().astimezone(ET).year
    scope_start = scope_doc.get("fiscal_year_start", scope_doc.get("year_start"))
    scope_end = scope_doc.get("fiscal_year_end", scope_doc.get("year_end"))
    requested_years = values.get("years")
    if requested_years is not None and any(
        year < scope_start or year > scope_end for year in requested_years
    ):
        raise ValueError("指定历史年份超出导入事件范围；请先扩展并核对事件资料")
    last_historical = min(scope_end, current - 1) if current is not None else scope_end
    target_years = requested_years if requested_years is not None else list(
        range(scope_start, last_historical + 1)
    )
    target_years = [
        y for y in target_years
        if y not in values.get("excluded_years", []) and (current is None or y < current)
    ]
    params = {
        "kind": "event_dates",
        "event_set_id": collection.id,
        "event_version_id": revision.id,
        "event_version": revision.version,
        "cutoff_date": cutoff.isoformat(),
        "current_year": now().astimezone(ET).year,
        "current_fiscal_year": fiscal,
        "current_fiscal_year_basis": "user_selected"
        if values.get("current_fiscal_year")
        else "verified_fiscal_calendar"
        if fiscal
        else "unknown",
        "calendar": security.calendar or "XNYS",
        "company": revision.document["company"],
        "include_unverified": values.get("include_unverified", False),
        "benchmark": values.get("benchmark"),
        "historical_years": values.get("historical_years", 8),
        "years": target_years,
        "excluded_years": values.get("excluded_years", []),
        "common_years": values.get("common_years", False),
        "date_window": values.get("date_window", "after5"),
        "date_category": values.get("date_category"),
    }
    batch = Batch(
        request_id=request_id,
        scope_key=fingerprint,
        kind="event_dates",
        title=f"{security.symbol} · {collection.title}",
        params={**params, **({"retry_generation": values["retry_generation"]} if values.get("retry_generation") else {})},
    )
    s.add(batch)
    s.flush()
    ranges = event_price_scope(revision.events, cutoff, params["calendar"])
    scope = RequestScope(
        batch_id=batch.id,
        security_id=security.id,
        symbol=security.symbol,
        start_date=min((r[0] for r in ranges), default=cutoff),
        end_date=max((r[1] for r in ranges), default=cutoff),
    )
    request = AnalysisRequest(batch_id=batch.id, params=params)
    s.add_all([scope, request])
    s.flush()
    # Only existing results can appear immediately; new computation is durable worker work.
    if security.status == "VERIFIED":
        from iirp.event_pipeline import plan_event_compute
        plan_event_compute(s, request, scope, cache_only=True)
    return {"analysis_id": request.id, "batch_id": batch.id, "reused": False}


def create_analysis(identifier, values):
    with session() as s, s.begin():
        return _create_analysis(s, _required(s, EventSet, identifier), values)


def _selected_events(revision, params):
    return [
        event
        for event in revision.events
        if not event["excluded"]
        and _year_selected(event, params)
        and (event["date_verified"] or params.get("include_unverified"))
    ]


def _year_selected(event, params):
    year = (
        event.get("fiscal_year")
        if event.get("event_type") == "earnings_release"
        else event.get("event_year")
    )
    current = (
        params.get("current_fiscal_year")
        if event.get("event_type") == "earnings_release"
        else params.get("current_year")
    )
    return year not in params.get("excluded_years", []) and (
        params.get("years") is None or year is None or year in params["years"] or year == current
    )


def event_input_key(request, dataset, security, event_content_hash=None, benchmark=None, *, db=None):
    from iirp.benchmarks import benchmark_snapshot

    state = inspect(request, raiseerr=False)
    s = db if db is not None else getattr(state, "session", None)
    if s is None:
        s = getattr(inspect(security, raiseerr=False), "session", None)
    snapshot = benchmark if benchmark is not None else benchmark_snapshot(request.params, security, s)
    dependency = dataset.id if dataset else None
    content = event_content_hash
    if s is not None:
        from iirp.research_dependencies import benchmark_dependency, dataset_dependency

        revision = _required(s, EventSetVersion, request.params["event_version_id"])
        content = digest([revision.document, revision.events])
        ranges = event_price_scope(
            _selected_events(revision, request.params),
            date.fromisoformat(request.params["cutoff_date"]),
            security.calendar or "XNYS",
        )
        dependency = dataset_dependency(s, dataset, ranges)
        snapshot = benchmark_dependency(s, snapshot, ranges)
    # The immutable event_version_id includes source/review revisions. Price
    # provenance remains in the saved result, while out-of-window bars no longer
    # invalidate an otherwise identical effective input.
    return digest(
        [
            "event-input-f-v1",
            content,
            request.params,
            dependency,
            _candidate(security),
            security.instrument,
            calendar_version(),
            CALCULATION_VERSION,
            REPRESENTATION_VERSION,
            snapshot,
        ]
    )



def plan_event_scope(s, scope, batch, capacity):
    """Use shared durable market jobs for only missing selected event windows."""
    from iirp.lifecycle import add_job, linked_jobs

    request = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == batch.id))
    revision = _required(s, EventSetVersion, request.params["event_version_id"])
    security = _required(s, Security, scope.security_id)
    if security.status == "PENDING":
        job = add_job(
            s, scope, "market_identity", {"symbol": security.symbol, "security_id": security.id}
        )
        scope.status = "RUNNING" if job.status in ACTIVE else "PARTIAL"
        scope.wait_reason = job.error or "正在核对证券身份、股类、币种与交易日历"
        return
    mic = revision.document["company"].get("exchange_mic")
    if (
        security.status != "VERIFIED"
        or not security.calendar
        or (mic and security.exchange not in MIC_EXCHANGES.get(mic, set()))
    ):
        scope.status, scope.wait_reason = "PARTIAL", "证券身份或导入交易所不一致，请先核对证券"
        return
    cutoff = min(
        date.fromisoformat(request.params["cutoff_date"]),
        last_completed_session(calendar=security.calendar),
    )
    selected = _selected_events(revision, request.params)
    ranges = event_price_scope(selected, cutoff, security.calendar)
    from iirp.benchmarks import plan_benchmark
    from iirp.event_pipeline import plan_event_compute
    from iirp.price_cache import ensure_prices, fetch_state

    price_status, price_reason, benchmark_reason = "READY", None, None
    if ranges:
        first, last = min(a for a, _ in ranges), max(b for _, b in ranges)
        price_status, price_reason = fetch_state(ensure_prices(
            s, scope, security, first, last, title=f"{security.symbol} · 事件窗口行情"))
        benchmark_reason = plan_benchmark(s, scope, request, first, last)
    # A failed fetch still publishes which events lack prices.
    result = plan_event_compute(s, request, scope, capacity) if price_status != "RUNNING" else None
    jobs = linked_jobs(s, scope.id)
    active = [job for job in jobs if job.status in ACTIVE
        and (job.kind != "event_compute" or (
            job.status != "PAUSED" and result is None
            and job.target.get("input_key") == scope.checkpoint.get("compute_input_key")))]
    current_jobs = [job for job in jobs if job.kind != "event_compute"
        or job.target.get("input_key") == scope.checkpoint.get("compute_input_key")]
    bad = [job for job in current_jobs if job.status in ("FAILED", "PARTIAL")
        and (job.kind != "event_compute" or result is None)]
    stopped_compute = next((job for job in current_jobs if job.kind == "event_compute"
        and job.status in ("PAUSED", "CANCELLED")), None) if result is None else None
    total = len(selected)
    unresolved = sum(
        not e.get("event_date") or e.get("date_status") != "supported" for e in selected
    )
    ready = (
        sum(
            all(w["complete"] for w in row["windows"].values()) and bool(row["windows"])
            for row in result.data["rows"]
        )
        if result
        else 0
    )
    scope.checkpoint = {
        **scope.checkpoint,
        "stage": "event_compute" if any(j.kind == "event_compute" for j in active) else "market_history" if active else "published" if result else "event_compute",
        "event_count": total,
        "ready_events": ready,
        "required_ranges": [{"start_date": str(a), "end_date": str(b)} for a, b in ranges],
        "result_id": result.id if result else scope.checkpoint.get("result_id"),
    }
    scope.status = (
        "RUNNING"
        if active
        else "PARTIAL" if bad or stopped_compute
        else "QUEUED" if result is None
        else "PARTIAL" if unresolved or benchmark_reason or price_reason
        else "READY"
    )
    scope.wait_reason = (
        bad[-1].error
        if bad
        else "计算子任务已暂停或取消，请在任务中恢复或创建续作分析"
        if stopped_compute
        else "活动日期缺失、冲突或未有支持证据；请更新事件资料后重新分析"
        if unresolved
        else price_reason
        if price_reason
        else f"已就绪 {ready}/{total} 次事件；正在获取行情"
        if active and price_status == "RUNNING"
        else benchmark_reason
        if benchmark_reason
        else "研究结果正在计算或等待计算容量；已完成结果可先阅读"
        if result is None
        else f"已就绪 {ready}/{total} 次事件；尚未到达窗口结束交易日，等待收盘后更新行情"
        if ready < total
        else None
    )


def get_analysis(identifier, result_id=None, *, include_freshness=True):
    with session() as s:
        request = _required(s, AnalysisRequest, identifier)
        if request.params.get("kind") != "event_dates":
            raise LookupError("这不是事件日期观察分析")
        batch = _required(s, Batch, request.batch_id)
        # Results live as long as the price caches they used (D14).
        results = s.execute(
            select(AnalysisResult.id, AnalysisResult.security_id, AnalysisResult.input_key,
                AnalysisResult.created_at, AnalysisResult.inputs["dataset_id"].astext.label("dataset_id"),
                AnalysisResult.expires_at)
            .where(AnalysisResult.analysis_id == request.id,
                   AnalysisResult.expires_at.is_(None) | (AnalysisResult.expires_at > now()))
            .order_by(AnalysisResult.created_at.desc())
        ).all()
        chosen = next((row for row in results if row.id == result_id), None) if result_id else None
        if not result_id and results:
            # Every unexpired result is current data; show the newest one.
            chosen = max(results, key=lambda row: (row.created_at, row.id))
        # Only the selected immutable result needs its potentially large payload.
        selected = s.get(AnalysisResult, chosen.id) if chosen else None
        if result_id and not selected:
            raise LookupError("结果已过期或不属于当前分析；请重新获取")
        scopes = s.scalars(select(RequestScope).where(RequestScope.batch_id == batch.id)).all()
        from iirp.research_freshness import freshness

        live = {"freshness": freshness(s, request, [{"security_id": scope.security_id} for scope in scopes if scope.security_id])} if include_freshness else {}
        return {
            **live,
            "id": request.id,
            "batch_id": batch.id,
            "params": request.params,
            "status": batch.status,
            "requested_action": batch.requested_action,
            "created_at": request.created_at.isoformat(),
            "progress": [
                {
                    "symbol": scope.symbol,
                    "status": scope.status,
                    "wait_reason": scope.wait_reason,
                    **scope.checkpoint,
                }
                for scope in scopes
            ],
            "result_id": selected.id if selected else None,
            "result_cutoff": (selected.inputs.get("params", {}).get("cutoff_date") or selected.data.get("metadata", {}).get("cutoff_date")) if selected else None,
            "expires_at": selected.expires_at.isoformat() if selected and selected.expires_at else None,
            "data": selected.data if selected else None,
            "results": [
                {
                    "id": row.id,
                    "created_at": row.created_at.isoformat(),
                    "dataset_id": row.dataset_id,
                    "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                }
                for row in results
            ],
        }


def export_analysis(identifier, result_id, format="json"):
    # Exports describe the chosen immutable result, never the mutable task view.
    # In a refreshed request a readable carried result still owns its old params.
    with session() as s:
        request = _required(s, AnalysisRequest, identifier)
        if request.params.get("kind") != "event_dates":
            raise LookupError("这不是事件日期观察分析")
        selected = s.get(AnalysisResult, result_id)
        if not selected or selected.analysis_id != request.id:
            raise LookupError("结果已过期或不属于当前分析；请重新获取")
        if not selected.data:
            raise ValueError("尚无可导出的冻结结果")
        metadata = selected.data.get("metadata", {})
        params = selected.inputs.get("params")
        parameter_basis = "result_inputs"
        if not isinstance(params, dict):
            params = metadata.get("params")
            parameter_basis = "result_metadata"
        if not isinstance(params, dict):
            # Old non-carried results belong to their original immutable request.
            # A carried legacy result cannot borrow the new request's conditions.
            params = request.params
            parameter_basis = "original_request"
        from iirp.fiscal_version import fiscal_version_notice

        result = {
            "schema_version": "iirp.event-analysis-snapshot.v2" if metadata.get("representation_version") else "iirp.event-analysis-snapshot.v1",
            "version_notice": fiscal_version_notice("event_import", metadata.get("calculation_version")),
            "id": request.id,
            "batch_id": request.batch_id,
            "params": params,
            "parameter_basis": parameter_basis,
            "created_at": request.created_at.isoformat(),
            "result_id": selected.id,
            "result_created_at": selected.created_at.isoformat(),
            "result_cutoff": params.get("cutoff_date") or metadata.get("cutoff_date"),
            "input_version": selected.input_key,
            "inputs": selected.inputs,
            "data": selected.data,
            "results": [{"id": selected.id, "created_at": selected.created_at.isoformat(),
                "dataset_id": selected.inputs.get("dataset_id")}],
        }
    if format == "json":
        return json.dumps(result, ensure_ascii=False, indent=2)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "result_id",
            "event_version_id",
            "dataset_id",
            "event",
            "year",
            "category",
            "event_date",
            "D0",
            "offset",
            "trading_date",
            "close",
            "daily_return",
            "relative_to_D-1",
            "status",
            "date_verified",
            "time_verified",
            "calculation_version",
            "cutoff_date",
            "benchmark",
            "benchmark_return",
            "difference_percentage_ratio",
            "parameters",
            "record_type",
            "statistics_or_window",
        ]
    )
    metadata = result["data"]["metadata"]
    chosen_category = result["params"].get("date_category")
    for row in result["data"]["rows"]:
        if chosen_category and row["category"] != chosen_category:
            continue
        for point in row.get("window_points", []):
            writer.writerow(
                [
                    result_id,
                    metadata["event_version_id"],
                    metadata["dataset_id"],
                    "'" + row["label"]
                    if row["label"].lstrip().startswith(("=", "+", "-", "@"))
                    else row["label"],
                    row["year"],
                    row["category"],
                    row["anchor"]["original_date"],
                    row["anchor"]["anchor_date"],
                    point["x"],
                    point["date"],
                    point["close"],
                    "",
                    "",
                    point["status"],
                    row["date_verified"],
                    row["time_verified"],
                    metadata.get("calculation_version"),
                    result["result_cutoff"],
                    json.dumps(result["data"].get("benchmark"), ensure_ascii=False),
                    point.get("benchmark"),
                    point.get("difference"),
                    json.dumps(result["params"], ensure_ascii=False),
                    "selected_window_price",
                    json.dumps(
                        {
                            "window": metadata["date_window"],
                            "relative_to_window_start": point["value"],
                            "baseline_date": row["windows"][metadata["date_window"]]["start_date"],
                        },
                        ensure_ascii=False,
                    ),
                ]
            )
    for row in result["data"]["rows"]:
        for point in row["points"]:
            # Treat arbitrary imported text as spreadsheet text, never as a formula.
            label = (
                "'" + row["label"]
                if row["label"].lstrip().startswith(("=", "+", "-", "@"))
                else row["label"]
            )
            writer.writerow(
                [
                    result_id,
                    metadata["event_version_id"],
                    metadata["dataset_id"],
                    label,
                    row["year"],
                    row["category"],
                    row["anchor"]["original_date"],
                    row["anchor"]["anchor_date"],
                    point["x"],
                    point["date"],
                    point["close"],
                    point["daily_return"],
                    point["value"],
                    point["status"],
                    row["date_verified"],
                    row["time_verified"],
                    metadata.get("calculation_version"),
                    result["result_cutoff"],
                    json.dumps(result["data"].get("benchmark"), ensure_ascii=False),
                    point.get("benchmark"),
                    point.get("difference"),
                    json.dumps(result["params"], ensure_ascii=False),
                    "daily_price",
                    "",
                ]
            )
    for record_type, items in (
        ("overlap_representation", [{"version": metadata["representation_version"], "details": "frozen_result_pagination", "preview_limit": 10}] if metadata.get("representation_version") else []),
        ("version_notice", [{"message": result["version_notice"]}] if result["version_notice"] else []),
        # Custom events have no fiscal calendar carrying their evidence. Export
        # each frozen event once, including source-only/excluded records.
        ("event_evidence", [{key: value for key, value in row.items()
                             if key not in {"points", "window_points", "windows", "benchmark_windows", "overlapping_event_ids"}}
                            for row in result["data"]["rows"]]),
        ("coverage_event", result["data"].get("coverage_rows", [])),
        ("distribution", result["data"].get("distributions", [])),
        ("fiscal_coverage", [result["data"].get("fiscal_coverage", {})]),
        (
            "event_window",
            [
                {
                    "key": row["key"],
                    "windows": row["windows"],
                    "benchmark_windows": row.get("benchmark_windows"),
                }
                for row in result["data"]["rows"]
            ],
        ),
    ):
        for item in items:
            writer.writerow(
                [
                    result_id,
                    metadata["event_version_id"],
                    metadata["dataset_id"],
                    *([""] * 13),
                    metadata.get("calculation_version"),
                    result["result_cutoff"],
                    json.dumps(result["data"].get("benchmark"), ensure_ascii=False),
                    "",
                    "",
                    json.dumps(result["params"], ensure_ascii=False),
                    record_type,
                    json.dumps(item, ensure_ascii=False),
                ]
            )
    return output.getvalue()


def event_prompt(kind="custom", language="zh"):
    """Small external JSON; instructions and schema share one Pydantic source."""
    if kind not in {"custom", "earnings"} or language not in {"zh", "en"}:
        raise ValueError("提示词种类或沟通语言无效")
    model = EarningsEventsImport if kind == "earnings" else CustomEventsImport
    from iirp.event_prompt_contract import field_contract
    from iirp.event_simple import SimpleCustomImport, SimpleEarningsImport, simple_example
    simple_model = SimpleEarningsImport if kind == "earnings" else SimpleCustomImport
    example = simple_example(kind)
    schema = simple_model.model_json_schema()
    shared = field_contract(schema)
    if language == "zh":
        instructions = (
            "请为股票事件日期研究返回一个简洁JSON对象，不写研究报告、股价、收益或投资建议。"
            "先补问缺失条件，每轮最多 3 个问题；公司、具体证券ticker、交易所MIC/股类、"
            "事件类别、年份、财季及未来排期范围如果已给定，不要重复问，也不要改写。"
            "证券股类不确定就补问，不能猜测。条件完整后联网打开来源，优先官方原文；不能联网就说明无法核验。\n"
            "只使用下方字段，禁止额外键。交易所写company.exchange_mic（如XNAS），"
            "不能写company.exchange/company.mic；股类没有security_type字段，需在询问中澄清并待人工证券核对。"
            "未来排期写scope.include_scheduled，不能写include_future。"
            "scope.include_keywords仅指导检索，不能代替逐项核对；exclude_keywords命中的事件名称不可选入计算，"
            "候选须保留给用户查看和填写排除理由，不可静默删除。"
            "财报只要Q2就必须明确写scope.fiscal_quarters:[2]；省略该字段的旧简洁格式表示Q1—Q4，"
            "不得把仅Q2扩大为四季。未给关键范围时先问清，不返回猜测JSON。\n"
            "research_as_of为实际检索日期。events每项仅放目标范围事实；gaps仅记未查询、未找到、"
            "部分查询、冲突或有证据的明确未举办。遗漏的覆盖项是未查询，不代表没发生。"
            "日期未知用date:null及目标年；日期冲突用date:null,date_conflict:true并把候选放来源note。"
            "实际时刻未知用time:null；官方日程不是实际发生分钟，电话会不是业绩首次公开时刻。"
            "时间冲突用time_conflict:true,time:null,time_basis:未知，财报release_session:未知。"
            "status/period_kind默认未知，不能凭URL或一般季度语境填已发生/常规。"
            "每个source的supports只列该来源实际支持的事实；URL存在不代表已核验。"
            "财报常规财季汇总需要可核验的财年财季、真实财期类型和公告日期；常规财期须有支持‘财期类型’的来源，不能仅从Q2字样推断；"
            "精确反应还需要已核验首次公开时刻；未知仍可显示日期观察和逐次价格，不能为提高N虚构事实。"
            "来源内容优先保留官方原文，解释和补问用中文，JSON键名及枚举保持下列固定写法。\n"
            "字段契约（从实际输入schema生成，required为必填，optional可省略；null表示未知，不等于未查询）：\n"
        )
        end = "下面仅是格式示例，不能照抄example.com或示例事实。条件完整时直接检索并仅返回JSON。"
    else:
        instructions = (
            "Return one concise JSON object for historical event-date research, without a report, prices, returns, or investment advice. "
            "Ask at most three focused questions per round for missing company, exact security/ticker, exchange MIC/share class, event type, "
            "year range, fiscal quarters, or scheduled-future scope. Do not ask again about supplied conditions or guess a share class. "
            "Once complete, open sources online and prefer original official publications; say when verification is unavailable.\n"
            "Use only the fields below; extra keys are invalid. Exchange MIC belongs in company.exchange_mic, never company.exchange or company.mic. "
            "There is no security_type field: clarify share class in conversation and leave security identity for human review. "
            "Scheduled future events use scope.include_scheduled, never include_future. scope.include_keywords guides retrieval only; "
            "scope.exclude_keywords blocks including matching event titles, but preserve those candidates for human review and an exclusion reason. "
            "For only Q2, explicitly write scope.fiscal_quarters:[2]. "
            "Legacy omission means Q1-Q4 and must not silently widen a Q2 request. Ask before output if a critical scope condition is missing.\n"
            "research_as_of is the actual lookup date. Include only in-scope events. Gaps distinguish not searched, not found, partial, conflict, "
            "and evidence-backed not held; omitted coverage means not searched. Unknown dates use date:null with the target year; conflicting dates use "
            "date:null,date_conflict:true and preserve alternatives in source notes. Unknown actual time uses time:null. An official schedule is not "
            "an actual start minute; a conference call is not the earnings first-publication time. For time conflict use time_conflict:true," 
            "time:null,time_basis:未知 and release_session:未知 for earnings. Status and period_kind default to 未知; do not infer occurred or "
            "regular from a URL or generic quarter wording. Each source supports only the facts it states. A URL is not verification. "
            "Regular fiscal-quarter summaries require supported fiscal year/quarter, period kind, and announcement date. "
            "A regular period needs a source supporting 财期类型; do not infer it from Q2 alone. Precise reactions additionally "
            "require a verified first-publication time. Unknown facts may still permit date observations and individual price windows. "
            "Keep official source language intact. Ask and explain in English; JSON keys and enums use the fixed values below.\n"
            "Field contract generated from the actual input schema (required vs optional; null means unknown, not unsearched):\n"
        )
        end = "This is a format example only. Replace example.com and every sample fact. Once conditions are complete, return JSON only."
    text = instructions + shared + "\n\n" + end + "\n" + json.dumps(example, ensure_ascii=False, indent=2)
    return {
        "kind": kind,
        "language": language,
        "schema_version": model.model_fields["schema_version"].annotation.__args__[0],
        "prompt": text,
        "json_schema": model.model_json_schema(),
        "input_schema_version": example["schema_version"],
        "input_example": example,
        "input_json_schema": schema,
    }


def clone_event_analysis(s, source_batch, request_id):
    """Continue a cancelled analysis with its original frozen research parameters."""
    if source_batch.kind != "event_dates":
        raise ValueError("只能续作事件日期观察批次")
    _lock(s, ["analysis", request_id])
    existing = s.scalar(select(Batch).where(Batch.request_id == request_id))
    if existing:
        if existing.parent_id != source_batch.id:
            raise RuntimeError("续作 request_id 已被其他批次使用")
        return existing
    original = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == source_batch.id))
    if original is None:
        raise LookupError("原事件分析请求不存在")
    child = Batch(
        request_id=request_id,
        scope_key=digest(["continue-event", source_batch.id, request_id]),
        kind="event_dates",
        title=source_batch.title,
        params=dict(original.params),
        parent_id=source_batch.id,
    )
    s.add(child)
    s.flush()
    request = AnalysisRequest(batch_id=child.id, params=dict(original.params))
    s.add(request)
    s.flush()
    for original_scope in s.scalars(
        select(RequestScope).where(RequestScope.batch_id == source_batch.id)
    ).all():
        scope = RequestScope(
            batch_id=child.id,
            symbol=original_scope.symbol,
            security_id=original_scope.security_id,
            start_date=original_scope.start_date,
            end_date=original_scope.end_date,
        )
        s.add(scope)
        s.flush()
        from iirp.event_pipeline import plan_event_compute
        plan_event_compute(s, request, scope, cache_only=True)
    return child
