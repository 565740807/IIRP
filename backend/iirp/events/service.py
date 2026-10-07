"""Saved event sets and their price-reaction analyses (S3).

Earnings and custom events share one flow: the user pastes the short JSON of
``event_input``, saves it as a set, and an analysis fetches each ticker's
prices once (the 24-hour cache, D14) and computes the D23 windows.

An analysis is a batch with one scope per ticker. Its request freezes the
events, n and the cutoff, so it stays readable after the set is edited or
deleted. Each scope publishes its ticker's result as soon as its prices are
cached; the result expires with that cache.
"""

import tomllib
import uuid
from datetime import date, timedelta
from functools import lru_cache

from sqlalchemy import select

from iirp.analysis.calendar import ET, last_completed_session
from iirp.analysis.event_windows import (
    CALCULATION_VERSION,
    analyze_events,
    reaction_day,
    sessions_needed,
)
from iirp.config import ROOT
from iirp.db import session
from iirp.events.input import EventInputError, parse_events
from iirp.market.yahoo import digest
from iirp.messages import NotFoundError, UserError, msg
from iirp.models import (
    ACTIVE,
    AnalysisRequest,
    AnalysisResult,
    Batch,
    EventSet,
    RequestScope,
    Security,
    now,
)

KIND = "event_dates"


@lru_cache
def window_defaults():
    """Per-feature n (D16) from config/analysis-defaults.toml."""
    with (ROOT / "config/analysis-defaults.toml").open("rb") as source:
        values = tomllib.load(source)
    return {**values["window_sessions"], **values["window_sessions_limits"]}


def _n(kind, value):
    limits = window_defaults()
    n = limits[kind] if value is None else value
    if type(n) is not int or not limits["min"] <= n <= limits["max"]:
        raise UserError("events.n_invalid", min=limits["min"], max=limits["max"])
    return n


def _required(s, model, identifier):
    row = s.get(model, identifier)
    if row is None:
        raise NotFoundError("events.not_found")
    return row


def _lock(s, value):
    from iirp.jobs.batches import advisory

    advisory(s, ["events", value])


def _preview_rows(events):
    rows = []
    for event in events:
        try:
            reaction = reaction_day(event["date"], event["session"]).isoformat()
        except Exception:  # Outside the exchange calendar's range.
            reaction = None
        rows.append({**event, "reaction_date": reaction})
    return rows


def validate(kind, text):
    """Check pasted JSON without saving anything."""
    try:
        events = parse_events(text, kind)
    except EventInputError as exc:
        return {"valid": False, "errors": exc.errors, "events": [], "tickers": []}
    return {"valid": True, "errors": [], "events": _preview_rows(events),
            "tickers": sorted({event["ticker"] for event in events})}


def _parse(kind, text):
    try:
        return parse_events(text, kind)
    except EventInputError as exc:
        raise ValueError(str(exc)) from None


def _set_view(s, row, details=True):
    result = {
        "id": row.id,
        "kind": row.kind,
        "title": row.title,
        "event_count": len(row.events),
        "tickers": sorted({event["ticker"] for event in row.events}),
        "first_date": row.events[0]["date"] if row.events else None,
        "last_date": row.events[-1]["date"] if row.events else None,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }
    if details:
        analyses = s.execute(
            select(AnalysisRequest.id, AnalysisRequest.created_at,
                   AnalysisRequest.params["n"].astext.label("n"), Batch.status)
            .join(Batch, Batch.id == AnalysisRequest.batch_id)
            .where(AnalysisRequest.params["kind"].astext == KIND,
                   AnalysisRequest.params["event_set_id"].astext == row.id)
            .order_by(AnalysisRequest.created_at.desc()).limit(10)).all()
        result["events"] = _preview_rows(row.events)
        result["analyses"] = [{"id": a.id, "created_at": a.created_at.isoformat(),
                               "n": int(a.n) if a.n else None, "status": a.status} for a in analyses]
    return result


def list_sets(kind=None):
    with session() as s:
        query = select(EventSet).order_by(EventSet.updated_at.desc())
        if kind:
            query = query.where(EventSet.kind == kind)
        return {"items": [_set_view(s, row, details=False) for row in s.scalars(query)]}


def get_set(identifier):
    with session() as s:
        return _set_view(s, _required(s, EventSet, identifier))


def _title(kind, title, events):
    title = (title or "").strip()
    if title:
        return title[:200]
    tickers = sorted({event["ticker"] for event in events})
    return msg(f"events.set_title.{kind}" + ("_more" if len(tickers) > 3 else ""),
               tickers=tickers[:3], first=events[0]["date"][:4], last=events[-1]["date"][:4])


def create_set(values):
    """Save a pasted list; optionally start its analysis in the same command."""
    kind = values["kind"]
    events = _parse(kind, values["text"])
    with session() as s, s.begin():
        request_id = values.get("request_id")
        if request_id:
            _lock(s, ["set", request_id])
            existing = s.scalar(select(EventSet).where(EventSet.request_id == request_id))
            if existing:
                if existing.kind != kind or existing.events != events:
                    raise RuntimeError(msg("events.request_id_reused"))
                return _created(s, existing, values)
        row = EventSet(kind=kind, title=_title(kind, values.get("title"), events),
                       events=events, request_id=request_id)
        s.add(row)
        s.flush()
        return _created(s, row, values)


def _created(s, row, values):
    result = {"set": _set_view(s, row)}
    if values.get("analyze"):
        result["analysis"] = _create_analysis(
            s, row, request_id=f"{values.get('request_id') or uuid.uuid4().hex}:analysis",
            n=values.get("n"))
        result["set"] = _set_view(s, row)
    return result


def update_set(identifier, values):
    with session() as s, s.begin():
        row = s.scalar(select(EventSet).where(EventSet.id == identifier).with_for_update())
        if row is None:
            raise NotFoundError("events.not_found")
        if values.get("text") is not None:
            row.events = _parse(row.kind, values["text"])
        if values.get("title") is not None:
            row.title = _title(row.kind, values["title"], row.events)
        row.updated_at = now()
        s.flush()
        return _set_view(s, row)


def delete_set(identifier):
    """Delete a saved set; analyses already made keep their frozen events."""
    with session() as s, s.begin():
        s.delete(_required(s, EventSet, identifier))
    return {"deleted": identifier}


def _security(s, symbol):
    from iirp.jobs.batches import advisory

    advisory(s, ["security", symbol])
    security = s.scalar(select(Security).where(Security.symbol == symbol).order_by(Security.id).limit(1))
    if security is None:
        security = Security(symbol=symbol)
        s.add(security)
        s.flush()
    return security


def _scope_range(events, n, calendar="XNYS"):
    """[R-1-n of the earliest event, R+n of the latest], never after today (D23)."""
    first, last = sessions_needed(events, n, calendar)
    today = now().astimezone(ET).date()
    return first, max(first, min(last, today))


def _new_analysis(s, params, *, request_id, title, parent_id=None):
    batch = Batch(request_id=request_id, scope_key=digest(params), kind=KIND, title=title,
                  params=params, parent_id=parent_id)
    s.add(batch)
    s.flush()
    request = AnalysisRequest(batch_id=batch.id, params=params)
    s.add(request)
    for symbol in dict.fromkeys(event["ticker"] for event in params["events"]):
        security = _security(s, symbol)
        start, end = _scope_range([e for e in params["events"] if e["ticker"] == symbol], params["n"])
        s.add(RequestScope(batch_id=batch.id, symbol=symbol, security_id=security.id,
                           start_date=start, end_date=end))
    s.flush()
    from iirp.jobs.signals import signal_batch

    signal_batch(s, batch.id)
    return {"analysis_id": request.id, "batch_id": batch.id, "reused": False}


def _create_analysis(s, row, *, request_id, n=None):
    _lock(s, ["analysis", request_id])
    existing = s.scalar(select(Batch).where(Batch.request_id == request_id))
    if existing:
        request = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == existing.id))
        if request is None or request.params.get("event_set_id") != row.id:
            raise RuntimeError(msg("events.analysis_request_id_reused"))
        return {"analysis_id": request.id, "batch_id": existing.id, "reused": True}
    params = {
        "kind": KIND,
        "event_kind": row.kind,
        "event_set_id": row.id,
        "title": row.title,
        "n": _n(row.kind, n),
        "cutoff_date": last_completed_session(as_of=now()).isoformat(),
        "calculation_version": CALCULATION_VERSION,
        "events": row.events,
    }
    return _new_analysis(s, params, request_id=request_id, title=row.title)


def create_analysis(identifier, values):
    with session() as s, s.begin():
        row = _required(s, EventSet, identifier)
        return _create_analysis(s, row, request_id=values["request_id"], n=values.get("n"))


def repeat_analysis(s, request, request_id, *, parent_id=None):
    """Same frozen events and n, cut off at the latest completed session."""
    _lock(s, ["analysis", request_id])
    existing = s.scalar(select(Batch).where(Batch.request_id == request_id))
    if existing:
        return s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == existing.id))
    params = {**request.params, "cutoff_date": last_completed_session(as_of=now()).isoformat()}
    created = _new_analysis(s, params, request_id=request_id, title=request.params["title"],
                            parent_id=parent_id)
    return s.get(AnalysisRequest, created["analysis_id"])


def clone_event_analysis(s, source_batch, request_id):
    """Continue a cancelled analysis (the "continue remaining" action)."""
    original = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == source_batch.id))
    if original is None:
        raise NotFoundError("events.original_missing")
    request = repeat_analysis(s, original, request_id, parent_id=source_batch.id)
    return s.get(Batch, request.batch_id)


def _publish(s, request, scope, security, cache):
    """Compute this ticker's windows from its cache and save the result."""
    from iirp.market.cache import CACHE_HOURS, price_bars
    from iirp.storage.maintenance import lock_analysis_references

    params = request.params
    calendar = security.calendar or "XNYS"
    events = [event for event in params["events"] if event["ticker"] == scope.symbol]
    bars, _ = price_bars(s, security.id, cache.id, ranges=[(scope.start_date, scope.end_date)]) \
        if cache else ([], None)
    data = analyze_events(events, bars, n=params["n"], cutoff=date.fromisoformat(params["cutoff_date"]),
                          calendar=calendar, kind=params["event_kind"])
    data["symbol"] = scope.symbol
    data["price_fetched_at"] = cache.fetched_at.isoformat() if cache else None
    lock_analysis_references(s)
    expires = cache.expires_at if cache else now() + timedelta(hours=CACHE_HOURS)
    row = AnalysisResult(
        analysis_id=request.id, security_id=security.id,
        input_key=digest([request.id, security.id, cache.id if cache else None, CALCULATION_VERSION]),
        inputs={"dataset_id": cache.id if cache else None, "calculation_version": CALCULATION_VERSION,
                "price_fetched_at": cache.fetched_at.isoformat() if cache else None,
                "source": msg("market.source_cache")},
        expires_at=expires)
    row.data = data
    s.add(row)
    s.flush()
    return row


def plan_event_scope(s, scope, batch, capacity):
    """One ticker: verify identity, ensure one cached fetch, then publish its result."""
    from iirp.jobs.batches import add_job
    from iirp.market.cache import current_cache, ensure_prices, fetch_state

    request = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == batch.id))
    security = s.get(Security, scope.security_id)
    published = s.scalar(select(AnalysisResult.id).where(
        AnalysisResult.analysis_id == request.id, AnalysisResult.security_id == security.id,
        AnalysisResult.expires_at > now()).limit(1))
    if published:
        scope.status, scope.wait_reason = "READY", None
        return
    if security.status == "PENDING":
        job = add_job(s, scope, "market_identity", {"symbol": security.symbol, "security_id": security.id})
        scope.status = "RUNNING" if job.status in ACTIVE else "PARTIAL"
        scope.wait_reason = job.error or msg("scope.identity_checking")
        return
    if security.status != "VERIFIED" or not security.calendar:
        scope.status, scope.wait_reason = "PARTIAL", msg("scope.identity_review")
        return
    ensured = ensure_prices(s, scope, security, scope.start_date, scope.end_date,
                            title=msg("job.title.event_prices", symbol=security.symbol))
    status, reason = fetch_state(ensured)
    if status != "READY":
        scope.status, scope.wait_reason = status, reason
        return
    _publish(s, request, scope, security, current_cache(s, security.id))
    scope.status, scope.wait_reason = "READY", None
    scope.checkpoint = {key: value for key, value in (scope.checkpoint or {}).items()
                        if key != "results_expired_at"}


def analysis_view(s, request):
    from iirp.analysis.freshness import freshness

    batch = s.get(Batch, request.batch_id)
    params = request.params
    results = {row.security_id: row for row in s.scalars(select(AnalysisResult).where(
        AnalysisResult.analysis_id == request.id, AnalysisResult.expires_at > now()))}
    tickers = []
    for scope in s.scalars(select(RequestScope).where(RequestScope.batch_id == batch.id)):
        result = results.get(scope.security_id)
        tickers.append({
            "symbol": scope.symbol,
            "status": "READY" if result else scope.status,
            "wait_reason": None if result else scope.wait_reason,
            "price_start": scope.start_date.isoformat() if scope.start_date else None,
            "price_end": scope.end_date.isoformat() if scope.end_date else None,
            "expires_at": result.expires_at.isoformat() if result else None,
            "result": result.data if result else None,
        })
    order = list(dict.fromkeys(event["ticker"] for event in params["events"]))
    tickers.sort(key=lambda item: order.index(item["symbol"]) if item["symbol"] in order else len(order))
    return {
        "id": request.id,
        "batch_id": batch.id,
        "status": batch.status,
        "title": params["title"],
        "event_kind": params["event_kind"],
        "event_set_id": params.get("event_set_id"),
        "n": params["n"],
        "cutoff_date": params["cutoff_date"],
        "event_count": len(params["events"]),
        "created_at": request.created_at.isoformat(),
        "tickers": tickers,
        "freshness": freshness(s, request),
    }


def get_analysis(identifier):
    with session() as s:
        request = _required(s, AnalysisRequest, identifier)
        if request.params.get("kind") != KIND:
            raise NotFoundError("events.not_event_analysis")
        return analysis_view(s, request)


def refresh_analysis(identifier, force=False):
    """Fetch again when results expired (automatic) or when asked (``force``)."""
    from iirp.analysis.freshness import refresh_analysis as refresh

    created = refresh(identifier, force)
    return get_analysis(created["id"])
