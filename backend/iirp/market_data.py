"""Explicit Yahoo adapter and versioned daily prices. Network never runs in a DB transaction."""

import hashlib
import json
import math
import time
import uuid
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from importlib.metadata import version

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from iirp.business_models import (
    CorporateAction,
    CoverageSegment,
    DatasetBar,
    MarketBar,
    PriceDataset,
    Security,
    SecurityIdentifier,
)
from iirp.config import ROOT, settings
from iirp.models import now

PRICE_BASIS = "SPLIT_ONLY"
MARKETS = {
    "^GSPC": "标普 500",
    "^IXIC": "纳斯达克综合",
    "^DJI": "道琼斯",
    "GC=F": "黄金期货",
    "^VIX": "VIX",
}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()
    ).hexdigest()


def number(value):
    if value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def plain(value):
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(x) for x in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        return plain(value.item())
    if hasattr(value, "to_dict"):
        return plain(value.to_dict())
    return value


def source_contract():
    path = ROOT / "config/provider-contracts.json"
    if not path.exists():
        return {"verified": False, "reason": "Yahoo 仅拆股价格口径待核对"}
    contract = json.loads(path.read_text())["yfinance"]
    return {
        **contract,
        "verified": bool(
            contract.get("verified") and contract.get("library_version") == version("yfinance")
        ),
    }


def fetch_market(kind, target):
    import yfinance as yf

    yf.set_tz_cache_location(str(settings().runtime_dir / "provider-cache"))
    symbol = target["symbol"]
    from iirp.market_http import timed_session

    timing = timed_session()
    timing.reset()
    adapter_started = time.perf_counter()
    ticker = yf.Ticker(symbol, session=timing.session)
    if kind == "market_identity":
        info = ticker.get_info()
        keys = (
            "symbol",
            "shortName",
            "longName",
            "quoteType",
            "currency",
            "exchange",
            "fullExchangeName",
            "timeZoneFullName",
            "firstTradeDateEpochUtc",
            "lastFiscalYearEnd",
            "nextFiscalYearEnd",
            "mostRecentQuarter",
        )
        metadata = {key: plain(info.get(key)) for key in keys}
        if (
            not metadata.get("symbol")
            or not metadata.get("quoteType")
            or not metadata.get("currency")
        ):
            raise ValueError("来源未提供足够证券身份字段，需核对股类、币种和交易所")
        return {
            "metadata": metadata,
            "symbol": symbol,
            "fetched_at": now().isoformat(),
            "library_version": version("yfinance"),
            "timing": {**timing.snapshot(), "adapter_seconds": time.perf_counter() - adapter_started,
                       "adapter_calls": 1},
        }
    if kind == "market_quote":
        # Homepage snapshots need the current provider quote, independently of the
        # frozen completed-session cutoff used by research. Include one following
        # date for a futures session that has already opened across midnight.
        observed_day = now().date()
        start = observed_day - timedelta(days=40)
        end = observed_day + timedelta(days=1)
    else:
        start = date.fromisoformat(target["start_date"])
        end = date.fromisoformat(target["end_date"])
    parameters = dict(
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        interval="1d",
        actions=True,
        auto_adjust=False,
        back_adjust=False,
        repair=False,
        keepna=True,
        rounding=False,
        prepost=False,
        timeout=12,
        raise_errors=True,
    )
    frame = ticker.history(**parameters)
    adapter_seconds = time.perf_counter() - adapter_started
    # HistoryMetadata is a lazy Mapping. Iterating all keys can materialize
    # tradingPeriods and trigger an unrelated intraday request behind our budget.
    stable_keys = (
        "currency",
        "symbol",
        "exchangeName",
        "fullExchangeName",
        "instrumentType",
        "firstTradeDate",
        "regularMarketTime",
        "timezone",
        "exchangeTimezoneName",
        "regularMarketPrice",
        "chartPreviousClose",
        "currentTradingPeriod",
    )
    raw_metadata = ticker.history_metadata if not frame.empty else None
    metadata = (
        {key: plain(raw_metadata.get(key)) for key in stable_keys}
        if raw_metadata is not None
        else {}
    )
    records = []
    for stamp, row in frame.iterrows():
        values = {"date": stamp.date().isoformat()}
        for source, dest in [
            ("Open", "open"),
            ("High", "high"),
            ("Low", "low"),
            ("Close", "close"),
            ("Adj Close", "adj_close"),
            ("Volume", "volume"),
            ("Dividends", "dividends"),
            ("Stock Splits", "splits"),
        ]:
            val = number(row.get(source))
            values[dest] = str(val) if val is not None else None
        records.append(values)
    return {
        "provider": "yfinance",
        "library_version": version("yfinance"),
        "symbol": symbol,
        "parameters": parameters,
        "records": records,
        "metadata": metadata,
        "fetched_at": now().isoformat(),
        "contract": source_contract(),
        "source_kind": "采集返回记录",
        # The publisher additionally checks complete exchange-session coverage.
        # Missing action columns are unknown, never an implicit zero.
        "actions_complete": "Stock Splits" in frame.columns,
        "timing": {**timing.snapshot(), "adapter_seconds": adapter_seconds, "adapter_calls": 1,
                   "measurement": "HTTP client elapsed includes connection and transfer; adapter includes library work"},
    }


def resolve_metadata(s, security, response):
    info = response["metadata"]
    quote_type = info.get("quoteType", "UNKNOWN")
    exchange = info.get("exchange")
    # Do not confuse index/futures sessions with the stock calendar.
    calendars = {
        "NMS": "XNYS",
        "NGM": "XNYS",
        "NCM": "XNYS",
        "NYQ": "XNYS",
        "ASE": "XNYS",
        "PCX": "XNYS",
        "BTS": "XNYS",
        "BATS": "XNYS",
    }
    security.name = info.get("longName") or info.get("shortName") or security.symbol
    security.instrument = quote_type
    security.currency = info.get("currency")
    security.exchange = exchange
    security.calendar = calendars.get(exchange) if quote_type in ("EQUITY", "ETF") else None
    # Quote refreshes cannot erase separately verified SEC identity/fiscal evidence.
    security.metadata_json = {**security.metadata_json, **info}
    security.status = (
        "VERIFIED" if security.calendar and security.currency == "USD" else "NEEDS_REVIEW"
    )
    if security.symbol in MARKETS:
        security.status = "VERIFIED_MARKET"
    if security.symbol in {"^GSPC", "^IXIC"} and info.get("symbol") == security.symbol and quote_type == "INDEX" and security.currency == "USD":
        # These US equity price indexes use the same daily close sessions as
        # the supported US stocks. Other indexes/futures keep quote-only status.
        security.calendar, security.status = "XNYS", "VERIFIED"
    from iirp.business_models import SecurityIdentifier

    identifier = s.scalar(
        select(SecurityIdentifier).where(SecurityIdentifier.security_id == security.id)
    )
    if not identifier:
        # This mapping is observed now; it is not proof of historic ticker ownership.
        s.add(
            SecurityIdentifier(
                security_id=security.id,
                provider="yfinance",
                symbol=info.get("symbol") or security.symbol,
                valid_from=now().date(),
            )
        )


def latest_dataset(s, security_id):
    return s.scalar(
        select(PriceDataset)
        .where(PriceDataset.security_id == security_id, PriceDataset.status == "PUBLISHED")
        .order_by(PriceDataset.published_at.desc(), PriceDataset.created_at.desc())
        .limit(1)
    )


def price_bars(s, security_id, dataset_id=None, *, ranges=None):
    dataset = s.get(PriceDataset, dataset_id) if dataset_id else latest_dataset(s, security_id)
    if not dataset:
        return [], None
    query = select(MarketBar.session_date, MarketBar.open, MarketBar.high, MarketBar.low,
                   MarketBar.close, MarketBar.adj_close, MarketBar.volume, MarketBar.status,
                   MarketBar.reason).join(DatasetBar, DatasetBar.bar_id == MarketBar.id).where(DatasetBar.dataset_id == dataset.id)
    if ranges is not None:
        from iirp.research_dependencies import range_predicate
        query = query.where(range_predicate(DatasetBar.session_date, ranges))
    rows = s.execute(query.order_by(DatasetBar.session_date)).all()
    return [
        {
            "date": row.session_date.isoformat(),
            **{
                k: str(getattr(row, k)) if getattr(row, k) is not None else None
                for k in ("open", "high", "low", "close", "adj_close", "volume")
            },
            "status": row.status,
            "reason": row.reason,
        }
        for row in rows
    ], dataset


def validate_bar(row, security, completed):
    day = date.fromisoformat(row["date"])
    values = {
        k: number(row.get(k)) for k in ("open", "high", "low", "close", "adj_close", "volume")
    }
    if day > completed:
        return values, "UNCONFIRMED", "当日尚未完成或未来会话"
    o, h, low, c = (values[k] for k in ("open", "high", "low", "close"))
    if any(x is None for x in (o, h, low, c)):
        return values, "MISSING_FIELDS", "缺少 OHLC 必需字段"
    if security.instrument in ("EQUITY", "ETF") and min(o, h, low, c) <= 0:
        return values, "INVALID", "股票/ETF 价格必须为正"
    if (
        h < max(o, low, c)
        or low > min(o, h, c)
        or (values["volume"] is not None and values["volume"] < 0)
    ):
        return values, "INVALID", "OHLC 关系或成交量不合法"
    return values, "VALID", None


def coverage_for(s, security, start, end, *, dataset_id=None):
    from iirp.analytics.calendar import sessions

    if not security or security.status != "VERIFIED" or not security.calendar:
        return {
            "start_date": str(start),
            "end_date": str(end),
            "status": "IDENTITY_PENDING",
            "reasons": ["证券股类、币种或交易日历待核对"],
        }
    expected = sessions(start, end, security.calendar)
    bars, dataset = price_bars(s, security.id, dataset_id, ranges=[(start, end)])
    selected = {b["date"]: b for b in bars if start.isoformat() <= b["date"] <= end.isoformat()}
    valid = {d for d, b in selected.items() if b["status"] == "VALID"}
    valid &= {d.isoformat() for d in expected}
    missing = [d.isoformat() for d in expected if d.isoformat() not in valid]
    reasons = sorted({b["reason"] for b in selected.values() if b.get("reason")})
    segments = s.scalars(
        select(CoverageSegment)
        .where(
            CoverageSegment.security_id == security.id,
            CoverageSegment.end_date >= start,
            CoverageSegment.start_date <= end,
        )
        .order_by(CoverageSegment.checked_at.desc())
        .limit(20)
    ).all()
    reasons += list(
        dict.fromkeys(x.details.get("reason") for x in segments if x.details.get("reason"))
    )
    return {
        "start_date": str(start),
        "end_date": str(end),
        "expected_sessions": len(expected),
        "available_sessions": len(selected),
        "valid_sessions": len(set(d.isoformat() for d in expected) & valid),
        "missing_dates": missing,
        "status": "COMPLETE" if not missing else "PARTIAL" if valid else "NOT_FETCHED",
        "basis": dataset.basis if dataset else "UNVERIFIED_PROVIDER_RECORDS",
        "dataset_id": dataset.id if dataset else None,
        "as_of": dataset.published_at.isoformat() if dataset else None,
        "first_valid_date": min(valid) if valid else None,
        "last_valid_date": max(valid) if valid else None,
        "reasons": list(dict.fromkeys(reasons)),
    }


def price_identity_conflicts(s, security, response, provider):
    """Only explicit contradictions reject a response; absent metadata is unknown.

    Aliases must come from this security's observed provider mapping. Case and
    whitespace are presentation differences; punctuation or another share class
    is never guessed to identify the same security.
    """
    def normalize(value):
        return str(value).strip().upper() if value is not None else ""

    aliases = {normalize(security.symbol), normalize(security.metadata_json.get("symbol"))}
    aliases.update(normalize(symbol) for symbol in s.scalars(
        select(SecurityIdentifier.symbol).where(
            SecurityIdentifier.security_id == security.id,
            SecurityIdentifier.provider == provider,
            SecurityIdentifier.valid_from <= now().date(),
            (SecurityIdentifier.valid_to.is_(None)) | (SecurityIdentifier.valid_to >= now().date()),
        )
    ))
    metadata = response.get("metadata") or {}
    conflicts = []
    for field, value in (("symbol", response.get("symbol")), ("symbol", metadata.get("symbol")),
                         ("currency", metadata.get("currency")),
                         ("instrument", metadata.get("instrumentType")),
                         ("instrument", metadata.get("quoteType"))):
        # Currency units can be case-sensitive (GBp versus GBP); do not
        # normalize pence into pounds. Symbols and instrument enums are not.
        actual = str(value).strip() if field == "currency" and value is not None else normalize(value)
        stored = getattr(security, field)
        expected = str(stored).strip() if field == "currency" and stored is not None else normalize(stored)
        if not actual or not expected or (field == "instrument" and "UNKNOWN" in (actual, expected)):
            continue
        mismatch = actual not in aliases if field == "symbol" else actual != expected
        if mismatch:
            conflicts.append(f"来源 {field}={actual} 与已核对证券 {expected} 冲突")
    return conflicts


def reconcile_splits(records, expected, baseline, *, reliable=True):
    """Reconcile immutable action evidence, distinguishing unknown from zero.

    A complete, explicit action series over the requested completed sessions can
    prove a cancellation. A positive observation proves an addition/revision even
    in a partial response. Uncertain cancellation blocks all price links from the
    response; its source/observations remain available for review.
    """
    result = dict(baseline)
    values = {day: number(row.get("splits")) for day, row in records.items() if day in expected}
    complete = bool(expected) and expected <= values.keys() and reliable and all(
        value is not None and value >= 0 for value in values.values()
    )
    uncertain = not reliable or any(value is None or value < 0 for value in values.values())
    changes = []
    for day in expected:
        before = baseline.get(str(day))
        value = values.get(day)
        if value is not None and value > 0 and value != 1:
            after = str(value.normalize())
            result[str(day)] = after
        elif complete:
            after = None
            result.pop(str(day), None)
        else:
            after = before
            # A missing day, missing field, or explicit zero in a truncated
            # response cannot revoke a previously established action.
            if before and (value is None or value in (0, 1)):
                uncertain = True
        if before != after:
            changes.append({"date": str(day), "before": before, "after": after})
    return result, sorted(changes, key=lambda item: item["date"]), uncertain, complete


def persist_prices(s, job, response, source_hash):
    """Save observations and publish only a coherent, validated price basis.

    The caller owns the lease fence and source-object transaction. Rebuilding
    starts with an empty manifest; unchanged numeric rows may be reused only
    after this basis has independently observed them again.
    """
    from iirp.analytics.calendar import last_completed_session, previous_session, sessions

    security = s.get(Security, job.target["security_id"])
    s.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(digest(security.id)[:15], 16)}
    )
    start, end = (date.fromisoformat(job.target[key]) for key in ("start_date", "end_date"))
    provider = response.get("provider", "yfinance")
    contract = response.get("contract", {})
    verified = contract.get("verified") is True
    basis_key = digest({"provider": provider, "contract": contract, "currency": security.currency})
    completed = last_completed_session(calendar=security.calendar or "XNYS")
    expected = set(sessions(start, min(end, completed), security.calendar or "XNYS"))
    old = latest_dataset(s, security.id)
    previous_links = (
        dict(
            s.execute(
                select(DatasetBar.session_date, DatasetBar.bar_id).where(
                    DatasetBar.dataset_id == old.id
                )
            ).all()
        )
        if old
        else {}
    )
    pending = s.scalar(
        select(PriceDataset)
        .where(PriceDataset.security_id == security.id, PriceDataset.status == "BUILDING")
        .order_by(PriceDataset.created_at.desc())
        .limit(1)
    )

    # Load the bounded verification set once. Per-day SELECT+flush used to make
    # network-efficient history requests pay thousands of database round trips.
    existing_bars = {(row.session_date, row.record_hash): row for row in s.scalars(
        select(MarketBar).where(MarketBar.security_id == security.id, MarketBar.provider == provider,
                               MarketBar.session_date >= start, MarketBar.session_date <= end))}
    previous_bars = {row.id: row for row in s.scalars(select(MarketBar).join(
        DatasetBar, DatasetBar.bar_id == MarketBar.id).where(DatasetBar.dataset_id == old.id))} if old else {}
    existing_actions = {(row.session_date, row.kind, row.value) for row in s.scalars(
        select(CorporateAction).where(CorporateAction.security_id == security.id,
            CorporateAction.session_date >= start, CorporateAction.session_date <= end))}
    new_rows = []
    prior_days = sessions(previous_session(start, security.calendar or "XNYS"), end, security.calendar or "XNYS")
    predecessors = dict(zip(prior_days[1:], prior_days[:-1]))
    baseline_manifest = pending.manifest if pending else old.manifest if old else {}
    verified_splits = dict(baseline_manifest.get("verified_splits", {}))
    identity_conflicts = price_identity_conflicts(s, security, response, provider)
    records, reasons = {}, list(identity_conflicts)
    identity_conflict = bool(identity_conflicts)
    for record in response.get("records", []):
        day = date.fromisoformat(record["date"])
        if not start <= day <= end:
            continue
        if day in records:
            raise ValueError("来源同一交易日重复，整块保留待核对")
        records[day] = record
        for field, kind in (("splits", "split"), ("dividends", "dividend")):
            value = number(record.get(field))
            if not value:
                continue
            if value < 0:
                reasons.append(f"{day} 公司行动数值不合法，需核对")
                continue
            # CorporateAction is an observation table, including unverified
            # sources. Only the published/building manifest establishes which
            # split events have been verified for this adjustment epoch.
            if (day, kind, value) not in existing_actions:
                existing_actions.add((day, kind, value))
                s.add(
                    CorporateAction(
                        security_id=security.id,
                        session_date=day,
                        kind=kind,
                        value=value,
                        source_hash=source_hash,
                    )
                )

    verified_splits, split_changes, uncertain_actions, actions_complete = reconcile_splits(
        records, expected, verified_splits, reliable=response.get("actions_complete") is not False,
    )
    publication_verified = verified and not identity_conflict and not uncertain_actions
    if uncertain_actions:
        reasons.append("拆股字段缺失或响应不完整，无法确认调整口径；保留旧版待完整复核")
    basis_changed = bool(old and old.basis_key != basis_key)
    split_changed = bool(split_changes and (pending or (
        old and previous_links and min(previous_links) < max(date.fromisoformat(x["date"]) for x in split_changes)
    )))
    rebase = publication_verified and (basis_changed or split_changed)
    observed, valid_observed, closes = {}, set(), {}
    for day, record in sorted(records.items()):
        values, status, reason = validate_bar(record, security, completed)
        if day not in expected and day <= completed:
            status, reason = "INVALID", "来源日期不属于证券交易日历"
        if any(
            number(record.get(field)) is not None and number(record.get(field)) < 0
            for field in ("splits", "dividends")
        ):
            status, reason = "NEEDS_REVIEW", "公司行动数值不合法，原价格保留待核对"
        if status == "VALID" and security.instrument in ("EQUITY", "ETF"):
            prior_day = predecessors.get(day) or previous_session(day, security.calendar or "XNYS")
            prior_close = closes.get(prior_day)
            # Old-basis boundaries cannot be used to validate a rebuilding
            # basis. Known split boundaries are checked by the full rebuild.
            if prior_close is None and not (rebase or pending) and prior_day in previous_links:
                previous = previous_bars[previous_links[prior_day]]
                if previous.status == "VALID":
                    prior_close = previous.close
            split_boundary = number(record.get("splits")) not in (None, Decimal(0), Decimal(1))
            if (
                prior_close
                and not split_boundary
                and abs(values["close"] / prior_close - 1) >= Decimal("0.5")
            ):
                status, reason = "NEEDS_REVIEW", "相邻交易日收盘变化达到 50%，保留原值待核对"
        if reason:
            reasons.append(f"{day}：{reason}")
        record_hash = digest({**values, "status": status})
        row = existing_bars.get((day, record_hash))
        if row is None:
            row = MarketBar(
                id=str(uuid.uuid4()),
                security_id=security.id,
                session_date=day,
                provider=provider,
                source_hash=source_hash,
                record_hash=record_hash,
                **values,
                status=status,
                reason=reason,
            )
            new_rows.append(row)
            existing_bars[(day, record_hash)] = row
        if day in expected:
            observed[day] = row.id
            if status == "VALID":
                valid_observed.add(day)
                closes[day] = values["close"]

    s.add_all(new_rows)
    s.flush()
    if not records:
        reasons.append("来源返回空数据；无法据此判断未上市或无交易")
    published_now = False
    stale_rebase = bool(pending and job.target.get("rebase") and job.target["rebase"] != pending.id)
    if publication_verified and stale_rebase:
        reasons.append("该任务引用的待核对价格版本已被替代；来源已保存，旧结果不混入当前版本")
    elif publication_verified:
        # A new verified source contract restarts the proof from an empty
        # manifest. A late response explicitly targeting an older build is
        # rejected above and cannot switch the active rebuild back again.
        prior_pending_range = []
        if pending and (pending.basis_key != basis_key or split_changed):
            prior_pending_range = [
                date.fromisoformat(pending.manifest["rebase_start"]),
                date.fromisoformat(pending.manifest["rebase_end"]),
            ]
            pending.status = "SUPERSEDED"
            pending.manifest = {
                **pending.manifest,
                "superseded_by_basis": basis_key,
                "superseded_by_splits": verified_splits,
            }
            pending = None
            rebase = True
        if rebase and pending is None:
            bounds = [*previous_links, *prior_pending_range, start, min(end, completed)]
            pending = PriceDataset(
                security_id=security.id,
                basis=PRICE_BASIS,
                basis_key=basis_key,
                manifest={
                    "rebase_start": str(min(bounds)),
                    "rebase_end": str(max(bounds)),
                    "reason": "拆股或来源口径变化，历史范围整体复核",
                    "provider": provider,
                    "contract": contract,
                    "previous_id": old.id if old else None,
                    "verification_sources": [],
                    "verified_splits": verified_splits,
                    "split_changes": split_changes,
                    "action_evidence_complete": actions_complete,
                },
            )
            s.add(pending)
            s.flush()
        if pending:
            pending.manifest = {
                **pending.manifest,
                "rebase_start": str(
                    min(start, date.fromisoformat(pending.manifest["rebase_start"]))
                ),
                "rebase_end": str(
                    max(min(end, completed), date.fromisoformat(pending.manifest["rebase_end"]))
                ),
                "verification_sources": list(
                    dict.fromkeys(
                        [
                            *pending.manifest.get("verification_sources", []),
                            source_hash,
                        ]
                    )
                ),
            }
            # Populate every re-observed date, including rows equal to the old
            # published numeric value. Never copy old links without this proof.
            if observed:
                statement = insert(DatasetBar).values(
                    [
                        {"dataset_id": pending.id, "session_date": day, "bar_id": bar_id}
                        for day, bar_id in observed.items()
                    ]
                )
                s.execute(
                    statement.on_conflict_do_update(
                        index_elements=[DatasetBar.dataset_id, DatasetBar.session_date],
                        set_={"bar_id": statement.excluded.bar_id},
                    )
                )
            required = set(
                sessions(
                    date.fromisoformat(pending.manifest["rebase_start"]),
                    date.fromisoformat(pending.manifest["rebase_end"]),
                    security.calendar or "XNYS",
                )
            )
            valid_dates = set(
                s.scalars(
                    select(DatasetBar.session_date)
                    .join(MarketBar, MarketBar.id == DatasetBar.bar_id)
                    .where(DatasetBar.dataset_id == pending.id, MarketBar.status == "VALID")
                )
            )
            pending.manifest = {
                **pending.manifest,
                "missing_dates": sorted(str(day) for day in required - valid_dates),
            }
            if required and required <= valid_dates:
                pending.status, pending.published_at = "PUBLISHED", now()
                published_now = True
            else:
                reasons.append("替换价格版本尚有缺失或不合格交易日，继续保留原已发布版本")
        else:
            changes = {
                day: bar_id
                for day, bar_id in observed.items()
                if previous_links.get(day) != bar_id
                # Bad revisions do not replace a previously qualified close.
                and (day in valid_observed or day not in previous_links)
            }
            if (changes or split_changes) and (valid_observed or old):
                links = {**previous_links, **changes}
                dataset = PriceDataset(
                    security_id=security.id,
                    basis=PRICE_BASIS,
                    basis_key=basis_key,
                    status="PUBLISHED",
                    published_at=now(),
                    manifest={
                        "provider": provider,
                        "contract": contract,
                        "previous_id": old.id if old else None,
                        "verification_sources": [source_hash],
                        "verified_splits": verified_splits,
                        "split_changes": split_changes,
                        "action_evidence_complete": actions_complete,
                    },
                )
                s.add(dataset)
                s.flush()
                s.execute(
                    insert(DatasetBar),
                    [
                        {"dataset_id": dataset.id, "session_date": day, "bar_id": bar_id}
                        for day, bar_id in links.items()
                    ],
                )
                published_now = True
    elif not verified:
        reasons.append("价格口径待核对，采集返回记录已保存")

    missing = sorted(str(day) for day in expected - set(records))
    invalid_dates = sorted(str(day) for day in expected & set(records) - valid_observed)
    reason = "；".join(dict.fromkeys(reasons))
    s.add(
        CoverageSegment(
            security_id=security.id,
            provider=provider,
            kind="daily",
            start_date=start,
            end_date=end,
            status="COMPLETE" if publication_verified and not missing and not reason else "PARTIAL",
            source_hash=source_hash,
            details={
                "expected_sessions": len(expected),
                "available_sessions": len(expected & set(records)),
                "valid_sessions": len(valid_observed),
                "missing_dates": missing,
                "invalid_dates": invalid_dates,
                "reason": reason,
                "provider_as_of": response.get("fetched_at"),
                "basis_key": basis_key,
                "identity_conflicts": identity_conflicts,
                "action_evidence_complete": actions_complete,
                "split_changes": split_changes,
            },
        )
    )
    return {
        "records": len(records),
        "missing_dates": missing,
        "invalid_dates": invalid_dates,
        "reason": reason,
        "rebase": rebase,
        "published": published_now,
        "eligible": publication_verified
        and bool(valid_observed)
        and not stale_rebase
        and not (pending and pending.status == "BUILDING"),
    }


def quote_from_history(symbol, response):
    from iirp.market_quotes import quote_from_snapshot

    return quote_from_snapshot(symbol, response)
