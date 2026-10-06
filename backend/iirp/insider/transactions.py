"""Company/person history, transaction detail, security mapping and windows."""

from datetime import date, datetime

from sqlalchemy import select

from iirp.db import session
from iirp.jobs.batch_views import batch_view
from iirp.jobs.batches import ET, _create, resolve_defaults, scope_range
from iirp.market.cache import cache_facts, price_bars
from iirp.models import (
    Batch,
    RequestReceipt,
    Security,
    now,
)


def entity_history(
    kind,
    entity_id,
    start_date,
    end_date,
    recent_count,
    date_basis,
    cursor,
    limit,
    issuer_id="",
    action="all",
):
    from iirp.insider.entities import read_entity_history
    from iirp.models import FeedSession

    with session() as s, s.begin():
        # Resolve the first reading's range once. Later pages keep its frozen dates.
        if not start_date and not end_date and not recent_count:
            frozen = s.get(FeedSession, cursor.split(":")[0]) if cursor else None
            if frozen:
                start_date = frozen.filters.get("start")
                end_date = frozen.filters.get("end")
            else:
                values = resolve_defaults(s, {"kind": "sec_history"})
                start, end = scope_range(values)
                start_date, end_date = str(start), str(end)
        return read_entity_history(
            s,
            kind,
            entity_id,
            start_date,
            end_date,
            recent_count,
            date_basis,
            cursor,
            limit,
            issuer_id=issuer_id,
            action=action,
        )


def resolve_amendment(relation_id, action, original_event_id, evidence):
    from iirp.insider import records

    with session() as s, s.begin():
        return records.resolve_amendment(s, relation_id, action, original_event_id, evidence)


def transaction_detail(transaction_id, mapping_version=None, cutoff_date=None):
    from iirp.analysis.transaction_windows import transaction_price_context
    from iirp.insider import records

    with session() as s:
        record = records.transaction_record(s, transaction_id)
        from iirp.models import TransactionEvent

        event = s.get(TransactionEvent, transaction_id)
        ticker = record.get("ticker")
        if ticker:
            candidates = s.scalars(
                select(Security).where(Security.symbol == ticker, Security.issuer_id.is_(None))
            ).all()
            record["securities"] += [
                {
                    "id": sec.id,
                    "symbol": sec.symbol,
                    "status": sec.status,
                    "issuer_confirmation_required": True,
                }
                for sec in candidates
            ]
        latest_mapping = event.data.get("security_mapping", {})
        history = []
        cursor = latest_mapping
        while isinstance(cursor, dict) and cursor:
            history.append({key: value for key, value in cursor.items() if key != "previous"})
            cursor = cursor.get("previous")
        latest_version = int(latest_mapping.get("version", 1)) if latest_mapping else None
        if mapping_version is not None:
            if not isinstance(mapping_version, int) or mapping_version < 1:
                raise ValueError("证券对应关系版本须为正整数")
            selected = next((item for item in history if int(item.get("version", 1)) == mapping_version), None)
            if selected is None:
                raise LookupError("证券对应关系版本不存在")
            mapping = selected
        else:
            mapping = latest_mapping
        record["security_mapping_history"] = history
        record["mapping_version"] = int(mapping.get("version", 1)) if mapping else None
        record["mapping_latest_version"] = latest_version
        sec = s.get(Security, mapping.get("security_id")) if mapping else None
        context = {"status": "IDENTITY_PENDING", "reason": "交易对应证券尚未核对"}
        if sec and sec.status == "VERIFIED" and sec.issuer_id == event.issuer_id:
            record["security_id"] = sec.id
            record["security_mapping"] = mapping
            bars, cache = price_bars(s, sec.id)
            if record.get("transaction_date") and record.get("accepted_at"):
                observation_date = date.fromisoformat(cutoff_date) if cutoff_date else None
                context = transaction_price_context(
                    bars, record["transaction_date"], record["accepted_at"],
                    today=observation_date, calendar=sec.calendar,
                )
                context["mapping_version"] = record["mapping_version"]
                context["security_id"] = sec.id
                context["source"] = cache.provider if cache else None
                context["price_basis"] = "SPLIT_ONLY" if cache else "UNVERIFIED_PROVIDER_RECORDS"
                context["as_of"] = cache.fetched_at.isoformat() if cache else None
                context.update(cache_facts(cache))

        if record.get("transaction_date") and record.get("accepted_at"):
            accepted = datetime.fromisoformat(record["accepted_at"]).astimezone(ET).date()
            context["disclosure_lag_calendar_days"] = (
                accepted - date.fromisoformat(record["transaction_date"])
            ).days
        return {"items": [], "data": {"transaction": record, "price_context": context}}


def map_transaction_security(transaction_id, values):
    from iirp.insider.records import transaction_record
    from iirp.models import SecurityIdentifier, TransactionEvent

    with session() as s, s.begin():
        event = s.get(TransactionEvent, transaction_id, with_for_update=True)
        sec = s.get(Security, values["security_id"], with_for_update=True)
        if not event or not sec:
            raise LookupError("交易或证券不存在")
        if sec.status != "VERIFIED" or sec.issuer_id != event.issuer_id:
            raise ValueError("只能选择已核对身份、且属于同一发行人的具体证券")
        record = transaction_record(s, transaction_id)
        if not event.transaction_date:
            raise ValueError("原申报交易日期未知，不能核对证券有效期")
        if (
            record.get("table") != "I"
            or str(record.get("security_title", "")).strip().casefold() != "common stock"
        ):
            raise ValueError("这条记录并非已明确的普通股；须先核对标的股类或衍生品关系")
        if sec.instrument != "EQUITY" or record.get("ticker") != sec.symbol:
            raise ValueError("申报普通股、证券类别和 ticker 不一致，不能建立对应关系")
        identifier = s.scalar(
            select(SecurityIdentifier).where(
                SecurityIdentifier.security_id == sec.id,
                SecurityIdentifier.symbol == sec.symbol,
                SecurityIdentifier.valid_from <= event.transaction_date,
                (
                    SecurityIdentifier.valid_to.is_(None)
                    | (SecurityIdentifier.valid_to >= event.transaction_date)
                ),
            ).limit(1)
        )
        if identifier is None:
            raise ValueError("交易当日没有有效的证券标识符记录，请先核对历史有效期")
        previous = event.data.get("security_mapping")
        if not (
            previous
            and previous.get("security_id") == sec.id
            and previous.get("evidence") == values["evidence"]
        ):
            event.data = {
                **event.data,
                "security_mapping": {
                    "version": int(previous.get("version", 1)) + 1 if previous else 1,
                    "security_id": sec.id,
                    "symbol": sec.symbol,
                    "evidence": values["evidence"],
                    "identifier_id": identifier.id,
                    "identifier_valid_from": identifier.valid_from.isoformat(),
                    "identifier_valid_to": (
                        identifier.valid_to.isoformat() if identifier.valid_to else None
                    ),
                    "verified_at": now().isoformat(),
                    "previous": previous,
                },
            }
    return transaction_detail(transaction_id)


def transaction_window(transaction_id, request_id):
    from iirp.analysis.calendar import (
        next_regular_open_after,
        next_session,
        reaction_session,
        session_window,
    )
    from iirp.models import TransactionEvent

    with session() as s, s.begin():
        event = s.get(TransactionEvent, transaction_id)
        if not event:
            raise LookupError("交易不存在")
        sec = (
            s.get(Security, event.data.get("security_mapping", {}).get("security_id"))
            if event.data.get("security_mapping")
            else None
        )
        if not sec or sec.status != "VERIFIED" or sec.issuer_id != event.issuer_id:
            raise ValueError("先核对具体股类或衍生品标的证券")
        if not event.transaction_date or not event.accepted_at:
            raise ValueError("交易日期或真实 SEC 接受时间缺失，需先补原文")
        receipt = s.get(RequestReceipt, request_id)
        existing = (
            s.get(Batch, receipt.batch_id) if receipt else
            s.scalar(select(Batch).where(Batch.request_id == request_id))
        )
        if existing:
            if (existing.kind != "market_history"
                    or existing.params.get("purpose") != "insider_window"
                    or existing.params.get("transaction_id") != transaction_id):
                raise ValueError("同一请求标识不能改成不同参数")
            # Replay the accepted scope, including legacy conservative buffers.
            # A new request may use today's planner without rewriting user history.
            return {"batch_id": existing.id, "reused": True, "batch": batch_view(s, existing)}
        left = session_window(
            next_session(event.transaction_date, sec.calendar), 5, 5, sec.calendar
        )
        right = session_window(
            date.fromisoformat(
                reaction_session(event.accepted_at, calendar=sec.calendar)["baseline_date"]
            ),
            5,
            5,
            sec.calendar,
        )
        next_open = session_window(
            next_regular_open_after(event.accepted_at, sec.calendar), 0, 4, sec.calendar
        )
        # Intraday disclosure needs the next opening's fifth session; pre-open,
        # after-close and non-session disclosure do not require an extra day.
        end = max(left[-1], right[-1], next_open[-1])
        start = min(left[0], right[0], next_open[0])
        batch, reused = _create(
            s,
            {
                "request_id": request_id,
                "kind": "market_history",
                "tickers": [sec.symbol],
                "start_date": str(start),
                "end_date": str(end),
                "purpose": "insider_window",
                "transaction_id": transaction_id,
            },
        )
        return {"batch_id": batch.id, "reused": reused, "batch": batch_view(s, batch)}
