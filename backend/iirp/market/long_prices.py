"""Durable, low-priority index/ETF history, with explicit retention frontiers.

Provider calls use the existing Yahoo lane and fences. A correction never joins
prices with different split bases: it requests a fresh complete retained range.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from dateutil.relativedelta import relativedelta
from sqlalchemy import delete, exists, func, select, text
from sqlalchemy.dialects.postgresql import insert

from iirp.analysis.calendar import ET, last_completed_session, sessions
from iirp.config import refresh
from iirp.messages import UserError, msg
from iirp.models import (
    ACTIVE,
    Batch,
    CollectionStrategy,
    IndexConstituent,
    IndexConstituentState,
    Job,
    LongPriceBar,
    LongPriceSeries,
    Preferences,
    PriceCache,
    Security,
    Subscription,
    now,
)

FIELDS = ("open", "high", "low", "close", "volume", "dividends", "splits")


def config():
    return refresh()["long_prices"]


def history_start(at=None):
    """January 1 of the earliest of the configured complete past years.

    Keeping whole calendar years lets monthly and rotation statistics count
    every month of the first year; the start moves forward each January.
    """
    return date((at or now()).astimezone(ET).date().year - config()["history_years"], 1, 1)


def retained_start(saved):
    """The configured start, or a later deletion frontier the user chose."""
    frontier = saved.values.get("long_prices", {}).get("retain_from") if saved else None
    return max(history_start(), date.fromisoformat(frontier)) if frontier else history_start()


def trim(s, row, start):
    """Advance a series to a later start, dropping bars and gap notes before it."""
    s.execute(delete(LongPriceBar).where(LongPriceBar.cache_id == row.id, LongPriceBar.session_date < start))
    row.retain_from = start
    row.start_date = max(row.start_date, start) if row.start_date else None
    row.details = {**row.details,
        **{key: [day for day in row.details.get(key, []) if day >= str(start)]
           for key in ("missing_dates", "accepted_missing_dates")},
        "invalid_dates": {day: reason for day, reason in row.details.get("invalid_dates", {}).items()
                          if day >= str(start)}}


def lock(s):
    from iirp.jobs.batches import advisory
    advisory(s, ["long_prices"])


def reconcile(s):
    """Preserve removed members and deletion frontiers, including across restores."""
    from iirp.analysis.sectors import group_etfs, sector_etfs
    from iirp.jobs.batches import advisory
    desired = set(sector_etfs()) | set(group_etfs()) | {"^GSPC", "^IXIC", "^NDX", "SPY", "QQQ"}
    desired |= {ticker.replace(".", "-") for ticker in s.scalars(select(IndexConstituent.ticker))}
    complete_lists = s.scalar(select(func.count()).select_from(IndexConstituentState).where(
        IndexConstituentState.updated_at.is_not(None))) == 2
    first = retained_start(s.get(Preferences, 1))
    known = {row.symbol: row for row in s.scalars(select(LongPriceSeries))}
    for row in known.values():
        if row.retain_from < first:
            # A new year (or a deletion frontier) moves the retained start forward.
            trim(s, row, first)
        elif row.retain_from > first and row.active:
            # Series saved from a later start (an earlier rule, not a user
            # deletion, which is part of ``first``) are fetched again whole.
            row.retain_from, row.needs_full, row.attempted_at = first, True, None
            row.generation += 1
    for symbol in sorted(desired):
        if symbol in known:
            row = known[symbol]
            if not row.active:
                row.active, row.removed_on = True, None
                row.generation += 1
                if row.retain_from > first:
                    row.retain_from, row.needs_full, row.attempted_at = first, True, None
            continue
        advisory(s, ["security", symbol])
        security = s.scalar(select(Security).where(Security.symbol == symbol).order_by(Security.id).limit(1))
        if security is None:
            security = Security(symbol=symbol)
            s.add(security)
            s.flush()
        s.add(LongPriceSeries(symbol=symbol, security_id=security.id, retain_from=first))
    if complete_lists:
        for symbol, row in known.items():
            if symbol not in desired and row.active:
                row.active, row.removed_on = False, now().astimezone(ET).date()
                row.generation += 1
    s.flush()


def has_bars(s, row):
    return s.scalar(select(exists().where(LongPriceBar.cache_id == row.id)))


def schedule(s):
    """Resume a bounded batch, or enqueue missing/new and due daily series."""
    from iirp.jobs.batches import _create
    from iirp.market.yahoo import digest
    if not s.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"),
                    {"key": int(digest(["long_prices"])[:15], 16)}):
        return
    policy = s.get(CollectionStrategy, "market")
    if not policy or not policy.enabled:
        return
    reconcile(s)
    active = s.scalar(select(Batch).where(Batch.params["long_prices"].astext == "true",
        Batch.status.in_((*ACTIVE, "PARTIAL"))).order_by(Batch.created_at.desc()).limit(1))
    if active and active.status in ACTIVE:
        return
    stamp = now().astimezone(ET)
    hour, minute = map(int, config()["time"].split(":"))
    last = last_completed_session(as_of=stamp)
    slot = datetime.combine(last, time(hour, minute), ET)
    due = []
    populated = set(s.scalars(select(LongPriceSeries.id).where(exists().where(LongPriceBar.cache_id == LongPriceSeries.id))))
    for row in s.scalars(select(LongPriceSeries).where(LongPriceSeries.active.is_(True))):
        pending = row.needs_full or row.id not in populated
        # Deleting everything means resume from today, never from the old frontier.
        if row.retain_from > last:
            continue
        if row.attempted_at and (not row.fetched_at or row.attempted_at > row.fetched_at
                or (row.needs_full and row.details.get("missing_dates"))):
            if stamp < row.attempted_at + timedelta(seconds=config()["retry_seconds"]):
                continue
        if pending or (stamp >= slot and (row.complete_through is None or row.complete_through < last)):
            due.append(row.symbol)
    if due:
        _create(s, {"kind": "market_history", "request_id": "long:" + str(uuid4()),
            "purpose": msg("long_prices.purpose"), "tickers": sorted(due),
            "start_date": str(history_start()), "end_date": str(last), "long_prices": True},
            trigger="automatic", policy_key="market")


def plan_scope(s, scope, batch, capacity):
    from iirp.jobs.batches import add_job
    row = s.scalar(select(LongPriceSeries).where(LongPriceSeries.security_id == scope.security_id))
    if not row or not row.active or row.retain_from > last_completed_session():
        scope.status = "READY"
        return
    if scope.status == "READY" and scope.checkpoint.get("generation") == row.generation:
        return
    security = s.get(Security, row.security_id)
    linked = scope.checkpoint.get("job_id")
    prior = s.get(Job, linked) if linked else None
    if prior and prior.target.get("generation") == row.generation:
        if prior.status in ACTIVE:
            scope.status = "RUNNING"
            return
        if prior.status in ("FAILED", "PARTIAL"):
            scope.status, scope.wait_reason = "PARTIAL", prior.error
            if not row.attempted_at or (row.fetched_at and row.attempted_at <= row.fetched_at):
                row.attempted_at = now()
                row.details = {**row.details, "error": prior.error}
            return
        if prior.kind == "market_long_history":
            scope.status = "READY"
            scope.checkpoint = {**scope.checkpoint, "generation": row.generation}
            return
    if capacity[0] <= 0:
        scope.status = "QUEUED"
        return
    end = last_completed_session()
    full = row.needs_full or not has_bars(s, row)
    recent = sessions(end - timedelta(days=40), end)[-config()["recent_sessions"]:]
    start = row.retain_from if full else max(row.retain_from, recent[0])
    if not full and row.complete_through is not None and row.complete_through < recent[0]:
        # After an offline period, include the missed sessions and an overlap;
        # a normal daily run still requests only the configured recent window.
        overlap = sessions(row.complete_through - timedelta(days=40), row.complete_through)[-config()["recent_sessions"]:]
        start = max(row.retain_from, overlap[0])
    target = {"symbol": row.symbol, "security_id": row.security_id, "series_id": row.id,
        "generation": row.generation, "start_date": str(start), "end_date": str(end),
        "full": full, "round": batch.request_id}
    identity = security.status == "PENDING" or (row.symbol in {"^GSPC", "^IXIC", "^NDX"} and security.status == "VERIFIED_MARKET")
    kind = "market_identity" if identity else "market_long_history"
    if not identity and security.status != "VERIFIED":
        scope.status, scope.wait_reason = "PARTIAL", msg("market.identity_pending")
        return
    job = add_job(s, scope, kind, target)
    job.title = msg("long_prices.job", symbol=row.symbol, start=start, end=end)
    scope.checkpoint = {**scope.checkpoint, "job_id": job.id}
    scope.status = "RUNNING"
    capacity[0] -= 1


def differs(stored, values):
    absolute, relative = (Decimal(str(config()[key])) for key in ("absolute_tolerance", "relative_tolerance"))
    return any(abs(getattr(stored, key) - values[key]) > max(absolute, abs(getattr(stored, key)) * relative)
               for key in ("open", "high", "low", "close"))


def persist(s, job, response):
    """Publish only complete, validated exchange sessions under the caller's fence."""
    from iirp.market.yahoo import number, price_identity_conflicts, validate_bar
    lock(s)
    row = s.get(LongPriceSeries, job.target["series_id"], with_for_update=True)
    if not row or not row.active or row.generation != job.target["generation"]:
        return {"cached": True, "records": 0, "superseded": True}
    row.attempted_at = now()
    security = s.get(Security, row.security_id)
    conflicts = price_identity_conflicts(s, security, response, response.get("provider", "yfinance"))
    if conflicts:
        return {"cached": False, "reason": msg("common.items", items=conflicts), "records": 0}
    start = max(row.retain_from, date.fromisoformat(job.target["start_date"]))
    end = min(last_completed_session(calendar=security.calendar or "XNYS"), date.fromisoformat(job.target["end_date"]))
    expected = set(sessions(start, end, security.calendar or "XNYS"))
    records, observed, invalid = {}, set(), {}
    for record in response.get("records", []):
        day = date.fromisoformat(record["date"])
        if day not in expected:
            continue
        if day in observed:
            raise UserError("market.duplicate_session")
        observed.add(day)
        values, status, reason = validate_bar(record, security, end)
        if status != "VALID":
            invalid[str(day)] = reason
            continue
        records[day] = {"cache_id": row.id, "session_date": day,
            **{key: values[key] for key in ("open", "high", "low", "close", "volume")},
            **{key: number(record.get(key)) for key in ("dividends", "splits")},
            "status": "VALID", "reason": None}
    if not records:
        return {"cached": False, "reason": msg("market.empty_response"), "records": 0}
    # Gaps at the beginning may be pre-listing; internal/trailing gaps cannot mark coverage complete.
    gaps = {day for day in expected if day >= min(observed)} - records.keys()
    previous_gaps = set(row.details.get("missing_dates", []))
    if not job.target["full"]:
        # A recent window must preserve older gaps. Repairs and newly bad days
        # require a complete response before accepting the new gap set.
        gaps |= {date.fromisoformat(day) for day in previous_gaps
                 if not str(start) <= day <= str(end)}
        stored = s.scalars(select(LongPriceBar).where(LongPriceBar.cache_id == row.id,
            LongPriceBar.session_date.in_(records))).all()
        if (any(differs(bar, records[bar.session_date]) for bar in stored)
                or previous_gaps != {str(day) for day in gaps}):
            row.needs_full = True
            row.generation += 1
            return {"cached": True, "records": 0, "correction": True}
    if job.target["full"]:
        s.execute(delete(LongPriceBar).where(LongPriceBar.cache_id == row.id))
    statement = insert(LongPriceBar).values(list(records.values()))
    s.execute(statement.on_conflict_do_update(index_elements=[LongPriceBar.cache_id, LongPriceBar.session_date],
        set_={key: getattr(statement.excluded, key) for key in (*FIELDS, "status", "reason")}))
    row.start_date = row.retain_from if job.target["full"] else min(row.start_date or start, start)
    row.end_date = row.complete_through = end
    missing_dates = sorted(str(day) for day in gaps)
    accepted = bool(gaps) and bool(row.fetched_at) and (
        set(missing_dates) == previous_gaps) and (
        job.target["full"] or set(missing_dates) == set(row.details.get("accepted_missing_dates", [])))
    row.fetched_at, row.needs_full = now(), bool(gaps) and not accepted
    reason = msg("long_prices.gaps", count=len(gaps)) if gaps else ""
    row.details = {"records": s.scalar(select(func.count()).select_from(LongPriceBar).where(LongPriceBar.cache_id == row.id)),
                   "missing_dates": missing_dates,
                   "accepted_missing_dates": missing_dates if accepted else [],
                   "invalid_dates": {**({day: reason for day, reason in row.details.get("invalid_dates", {}).items()
                       if not str(start) <= day <= str(end)} if not job.target["full"] else {}), **invalid},
                   "reason": reason, "provider": response.get("provider", "yfinance")}
    return {"cached": not row.needs_full, "records": len(records), "reason": reason}


def local_series(s, security_id, start=None, end=None):
    row = s.scalar(select(LongPriceSeries).where(LongPriceSeries.security_id == security_id))
    if not row or not row.fetched_at or row.start_date is None or row.complete_through is None:
        return None
    if start is not None:
        from iirp.market.cache import covers
        security = s.get(Security, security_id)
        if not covers(row, start, end, security.calendar or "XNYS"):
            return None
    return row if has_bars(s, row) else None


def materialize(s, row):
    """One disposable analysis input, rebuilt from local facts without a provider task."""
    from iirp.market.cache import persist_prices
    records = [{"date": bar.session_date.isoformat(), **{key: str(getattr(bar, key)) if getattr(bar, key) is not None else None for key in FIELDS}}
               for bar in s.scalars(select(LongPriceBar).where(LongPriceBar.cache_id == row.id).order_by(LongPriceBar.session_date))]
    job = SimpleNamespace(target={"security_id": row.security_id, "start_date": str(row.start_date), "end_date": str(row.end_date)})
    persist_prices(s, job, {"provider": "local_long_prices", "records": records})
    cache = s.scalar(select(PriceCache).where(PriceCache.security_id == row.security_id))
    if cache.provider == "local_long_prices":
        cache.details = {**cache.details,
            "accepted_missing_dates": row.details.get("accepted_missing_dates", [])}
    return cache


def manage(s, action):
    lock(s)
    if action not in ("all", "year", "half_year", "quarter", "refill"):
        raise UserError("long_prices.action_invalid")
    today = now().astimezone(ET).date()
    cutoff = history_start() if action == "refill" else today if action == "all" else today - relativedelta(
        months={"year": 12, "half_year": 6, "quarter": 3}[action])
    saved = s.get(Preferences, 1, with_for_update=True)
    if saved is None:
        saved = Preferences(id=1, values={})
        s.add(saved)
    old = saved.values.get("long_prices", {}).get("retain_from")
    if action == "refill":
        # Refilling returns to the configured start, which keeps moving each January.
        saved.values = {key: value for key, value in saved.values.items() if key != "long_prices"}
    else:
        frontier = cutoff if not old else max(cutoff, date.fromisoformat(old))
        saved.values = {**saved.values, "long_prices": {"retain_from": str(frontier)}}
    saved.version = (saved.version or 0) + 1
    rows = s.scalars(select(LongPriceSeries).with_for_update()).all()
    for row in rows:
        row.generation += 1
        row.retain_from = cutoff if action == "refill" else max(row.retain_from, cutoff)
        row.needs_full = action == "refill"
        row.attempted_at = None
        if action != "refill":
            s.execute(delete(LongPriceBar).where(LongPriceBar.cache_id == row.id,
                True if action == "all" else LongPriceBar.session_date < cutoff))
            row.start_date = max(row.start_date, cutoff) if row.start_date else None
        row.details = {**row.details,
            **{key: ([] if action == "refill" else [day for day in row.details.get(key, [])
                if day >= str(row.retain_from)]) for key in ("missing_dates", "accepted_missing_dates")},
            "invalid_dates": {day: reason for day, reason in row.details.get("invalid_dates", {}).items()
                if action != "refill" and day >= str(row.retain_from)}}
        # An old input must not hide a user-requested deletion or refilling frontier.
        s.execute(delete(PriceCache).where(PriceCache.security_id == row.security_id))
    return {"message": msg("long_prices.managed"), "series": len(rows)}


def enqueue_manage(s, action):
    lock(s)
    job = Job(kind="market_long_manage", title=msg("long_prices.manage_job"),
        target={"action": action}, idempotency_key=str(uuid4()), priority=50, progress_total=1)
    s.add(job)
    s.flush()
    s.add(Subscription(job_id=job.id, source="manual"))
    return job.id


def status(s):
    rows = s.scalars(select(LongPriceSeries).order_by(LongPriceSeries.symbol)).all()
    bars, first, last = s.execute(select(func.count(), func.min(LongPriceBar.session_date), func.max(LongPriceBar.session_date))).one()
    size = s.scalar(text("SELECT pg_total_relation_size('long_price_bar') + pg_total_relation_size('long_price_series')"))
    policy = s.get(CollectionStrategy, "market")
    pending = s.scalar(select(func.count()).select_from(Job).where(Job.kind == "market_long_history", Job.status.in_(ACTIVE)))
    populated = set(s.scalars(select(LongPriceSeries.id).where(exists().where(LongPriceBar.cache_id == LongPriceSeries.id))))
    gaps = [{"symbol": row.symbol, "dates": row.details.get("missing_dates", []),
             "accepted": not row.needs_full} for row in rows if row.active and row.details.get("missing_dates")]
    return {"gap_symbols": len(gaps), "gap_days": sum(len(item["dates"]) for item in gaps), "gaps": gaps,
        "symbols": len(rows), "active_symbols": sum(row.active for row in rows), "ready_symbols": sum(row.active and row.id in populated and not row.needs_full for row in rows),
        "bars": bars, "start_date": first, "end_date": last, "bytes": size,
        "updated_at": max((row.fetched_at for row in rows if row.fetched_at), default=None),
        "status": "paused" if not policy or not policy.enabled else "backfilling" if pending or any(
            row.active and (row.needs_full or row.id not in populated) for row in rows) else "ready",
        "history_years": config()["history_years"], "pending_jobs": pending,
        "removed": [{"symbol": row.symbol, "removed_on": row.removed_on} for row in rows if row.removed_on]}
