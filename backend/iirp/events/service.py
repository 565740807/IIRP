"""Saved event sets and their price-reaction analyses.

Earnings and custom events share one flow: the user pastes the short JSON of
``event_input``, saves it as a set, and an analysis fetches each ticker's
prices once (the 24-hour cache) and computes the event windows.

An analysis is a batch with one scope per ticker. Its request freezes the
events, n, the benchmark and the cutoff, so it stays readable after the set is
edited or deleted. Each scope publishes its ticker's result once its prices
(and the benchmark's, unless that cannot be had) are cached; the result
expires with those caches. The same conditions on the same day reuse the
analysis already made instead of starting another.
"""

import csv
import io
import tomllib
import uuid
from datetime import date, timedelta
from functools import lru_cache

from sqlalchemy import select

from iirp.analysis.benchmarks import validate_benchmark
from iirp.analysis.calendar import ET, last_completed_session
from iirp.analysis.event_windows import (
    CALCULATION_VERSION,
    WINDOWS,
    analyze_events,
    reaction_day,
    sessions_needed,
)
from iirp.analysis.sector_benchmarks import validate_sector_benchmark
from iirp.config import ROOT
from iirp.db import session
from iirp.events.input import EventInputError, check_events, parse_events
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
DEFAULT_BENCHMARK = "^GSPC"
PRESETS = [3, 5, 10, 20]


@lru_cache
def analysis_defaults():
    with (ROOT / "config/analysis-defaults.toml").open("rb") as source:
        return tomllib.load(source)


def window_defaults():
    """Per-feature n from config/analysis-defaults.toml."""
    values = analysis_defaults()
    return {**values["window_sessions"], **values["window_sessions_limits"]}


def context_defaults():
    """The background window: default days before/after an event and the page's presets."""
    return analysis_defaults()["event_context"]


def context_window(before=None, after=None):
    values = context_defaults()
    window = (values["before_days"] if before is None else before,
              values["after_days"] if after is None else after)
    if any(type(days) is not int or not 0 <= days <= values["max_days"] for days in window):
        raise UserError("events.context_window_invalid", max=values["max_days"])
    return window


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


def validate(kind, text, before=None, after=None):
    """Check pasted JSON without saving anything; warnings never block saving."""
    window = context_window(before, after)
    try:
        events, warnings = check_events(text, kind, window)
    except EventInputError as exc:
        return {"valid": False, "errors": exc.errors, "warnings": [], "events": [], "tickers": []}
    return {"valid": True, "errors": [], "warnings": warnings, "events": _preview_rows(events),
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


def _title(kind, title, events, language="en"):
    title = (title or "").strip()
    if title:
        return title[:200]
    tickers = sorted({event["ticker"] for event in events})
    from iirp.messages import saved_name
    return saved_name(f"events.set_title.{kind}" + ("_more" if len(tickers) > 3 else ""),
               language=language, tickers=tickers[:3], first=min(e["date"] for e in events)[:4],
                      last=max(e["date"] for e in events)[:4])


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
        row = EventSet(kind=kind, title=_title(kind, values.get("title"), events, values.get("language", "en")),
                       events=events, request_id=request_id)
        s.add(row)
        s.flush()
        return _created(s, row, values)


def _created(s, row, values):
    result = {"set": _set_view(s, row)}
    if values.get("analyze"):
        result["analysis"] = _create_analysis(
            s, row, request_id=f"{values.get('request_id') or uuid.uuid4().hex}:analysis",
            n=values.get("n"), benchmark=values.get("benchmark", DEFAULT_BENCHMARK),
            sector_benchmark=values.get("sector_benchmark", "auto"))
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
            row.title = _title(row.kind, values["title"], row.events, values.get("language", "en"))
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
    """[R-1-n of the earliest event, R+n of the latest], widened to every
    context date so each event's chart covers its background; never after today."""
    first, last = sessions_needed(events, n, calendar)
    dates = [date.fromisoformat(item["date"]) for event in events for item in event.get("context") or ()]
    if dates:
        first, last = min(first, min(dates)), max(last, max(dates))
    today = now().astimezone(ET).date()
    return first, max(first, min(last, today))


def _reusable(s, params):
    """An analysis of the same conditions made today whose results have not lapsed."""
    from iirp.analysis.freshness import freshness

    for batch in s.scalars(select(Batch).where(Batch.kind == KIND, Batch.scope_key == digest(params),
                                               Batch.status.not_in(("FAILED", "CANCELLED")))
                           .order_by(Batch.created_at.desc()).limit(3)):
        request = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == batch.id))
        if request and request.params == params and not freshness(s, request)["expired"]:
            return request
    return None


def _new_analysis(s, params, *, request_id, title, parent_id=None, reuse=True):
    if reuse and (existing := _reusable(s, params)):
        return {"analysis_id": existing.id, "batch_id": existing.batch_id, "reused": True}
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


def _params(*, kind, event_set_id, title, events, n, benchmark, sector_benchmark="auto"):
    return {
        "kind": KIND,
        "event_kind": kind,
        "event_set_id": event_set_id,
        "title": title,
        "n": _n(kind, n),
        "benchmark": validate_benchmark(benchmark),
        "sector_benchmark": validate_sector_benchmark(sector_benchmark),
        "cutoff_date": last_completed_session(as_of=now()).isoformat(),
        "calculation_version": CALCULATION_VERSION,
        "events": events,
    }


def _create_analysis(s, row, *, request_id, n=None, benchmark=DEFAULT_BENCHMARK, sector_benchmark="auto"):
    _lock(s, ["analysis", request_id])
    existing = s.scalar(select(Batch).where(Batch.request_id == request_id))
    if existing:
        request = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == existing.id))
        if request is None or request.params.get("event_set_id") != row.id:
            raise RuntimeError(msg("events.analysis_request_id_reused"))
        return {"analysis_id": request.id, "batch_id": existing.id, "reused": True}
    params = _params(kind=row.kind, event_set_id=row.id, title=row.title, events=row.events,
                     n=n, benchmark=benchmark, sector_benchmark=sector_benchmark)
    _lock(s, ["conditions", digest(params)])
    return _new_analysis(s, params, request_id=request_id, title=row.title)


def create_analysis(identifier, values):
    with session() as s, s.begin():
        row = _required(s, EventSet, identifier)
        return _create_analysis(s, row, request_id=values["request_id"], n=values.get("n"),
                                benchmark=values.get("benchmark", DEFAULT_BENCHMARK),
                                sector_benchmark=values.get("sector_benchmark", "auto"))


def analysis_variant(identifier, values):
    """The same frozen events with another n or benchmark (the quick switches).

    Switching back and forth on one day reopens the analyses already made.
    """
    with session() as s, s.begin():
        request = _required(s, AnalysisRequest, identifier)
        if request.params.get("kind") != KIND:
            raise NotFoundError("events.not_event_analysis")
        _lock(s, ["analysis", values["request_id"]])
        existing = s.scalar(select(Batch).where(Batch.request_id == values["request_id"]))
        if existing:
            found = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == existing.id))
            return {"analysis_id": found.id, "batch_id": existing.id, "reused": True}
        old = request.params
        params = _params(kind=old["event_kind"], event_set_id=old.get("event_set_id"), title=old["title"],
                         events=old["events"], n=values.get("n", old["n"]),
                         benchmark=values.get("benchmark", old.get("benchmark")),
                         sector_benchmark=values.get("sector_benchmark", old.get("sector_benchmark", "auto")))
        _lock(s, ["conditions", digest(params)])
        return _new_analysis(s, params, request_id=values["request_id"], title=old["title"])


def repeat_analysis(s, request, request_id, *, parent_id=None):
    """Same frozen events and n, cut off at the latest completed session."""
    _lock(s, ["analysis", request_id])
    existing = s.scalar(select(Batch).where(Batch.request_id == request_id))
    if existing:
        return s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == existing.id))
    params = {**request.params, "cutoff_date": last_completed_session(as_of=now()).isoformat(),
              "calculation_version": CALCULATION_VERSION}
    # Analyses made before benchmarks existed get the default one.
    params.setdefault("benchmark", DEFAULT_BENCHMARK)
    params.setdefault("sector_benchmark", "auto")
    created = _new_analysis(s, params, request_id=request_id, title=request.params["title"],
                            parent_id=parent_id, reuse=False)
    return s.get(AnalysisRequest, created["analysis_id"])


def clone_event_analysis(s, source_batch, request_id):
    """Continue a cancelled analysis (the "continue remaining" action)."""
    original = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == source_batch.id))
    if original is None:
        raise NotFoundError("events.original_missing")
    request = repeat_analysis(s, original, request_id, parent_id=source_batch.id)
    return s.get(Batch, request.batch_id)


def _benchmark_range(params, calendar="XNYS"):
    """One benchmark range for every ticker of the analysis, so it is fetched once."""
    return _scope_range(params["events"], params["n"], calendar)


def _publish(s, request, scope, security, cache):
    """Compute this ticker's windows from its cache (and the benchmark's) and save the result."""
    from iirp.analysis.benchmarks import benchmark_data, benchmark_snapshot
    from iirp.market.cache import CACHE_HOURS, current_cache, price_bars
    from iirp.models import PriceCache
    from iirp.storage.maintenance import lock_analysis_references

    params = request.params
    calendar = security.calendar or "XNYS"
    events = [event for event in params["events"] if event["ticker"] == scope.symbol]
    bars, _ = price_bars(s, security.id, cache.id, ranges=[(scope.start_date, scope.end_date)]) \
        if cache else ([], None)
    snapshot = benchmark_snapshot(params, security, s)
    benchmark = benchmark_data(s, snapshot, ranges=[(scope.start_date, scope.end_date)])
    from iirp.analysis.sector_benchmarks import sector_snapshot
    sector = sector_snapshot(params, security, s, scope=scope)
    sector_data = benchmark_data(s, sector, ranges=[(scope.start_date, scope.end_date)])
    data = analyze_events(events, bars, n=params["n"], cutoff=date.fromisoformat(params["cutoff_date"]),
                          calendar=calendar, kind=params["event_kind"], benchmark=benchmark, sector=sector_data)
    data["symbol"] = scope.symbol
    data["price_fetched_at"] = cache.fetched_at.isoformat() if cache else None
    lock_analysis_references(s)
    expires = [cache.expires_at if cache else now() + timedelta(hours=CACHE_HOURS)]
    paired = current_cache(s, snapshot["security_id"]) if snapshot and snapshot.get("dataset_id") else None
    if paired:
        expires.append(paired.expires_at)
    sector_cache = s.get(PriceCache, sector["dataset_id"]) if sector and sector.get("dataset_id") else None
    if sector_cache:
        expires.append(sector_cache.expires_at)
    row = AnalysisResult(
        analysis_id=request.id, security_id=security.id,
        input_key=digest([request.id, security.id, cache.id if cache else None,
                          paired.id if paired else None, sector, CALCULATION_VERSION]),
        inputs={"dataset_id": cache.id if cache else None, "calculation_version": CALCULATION_VERSION,
                "benchmark_dataset_id": paired.id if paired else None,
                "sector_dataset_id": sector_cache.id if sector_cache else None, "sector": sector,
                "price_sources": [{"symbol": item.symbol,
                    "fetched_at": source.fetched_at.isoformat(),
                    "expires_at": source.expires_at.isoformat()}
                    for source in (cache, paired, sector_cache) if source
                    for item in (s.get(Security, source.security_id),)],
                "price_fetched_at": cache.fetched_at.isoformat() if cache else None,
                "source": msg("market.source_cache")},
        expires_at=min(expires))
    row.data = data
    s.add(row)
    s.flush()
    return row


def plan_event_scope(s, scope, batch, capacity):
    """One ticker: verify identity, ensure one cached fetch (and the benchmark's),
    then publish its result. A benchmark that cannot be had does not hold the
    stock's result back; the scope then stays PARTIAL with the reason."""
    from iirp.analysis.benchmarks import plan_benchmark
    from iirp.jobs.batch_views import linked_jobs
    from iirp.jobs.batches import add_job
    from iirp.market.cache import current_cache, ensure_prices, fetch_state

    request = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == batch.id))
    security = s.get(Security, scope.security_id)
    published = s.scalar(select(AnalysisResult.id).where(
        AnalysisResult.analysis_id == request.id, AnalysisResult.security_id == security.id,
        AnalysisResult.expires_at > now()).limit(1))
    if published:
        if scope.status not in ("READY", "PARTIAL"):
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
    benchmark_reason = None
    if request.params.get("benchmark"):
        # Asked alongside the stock's prices, not after them.
        benchmark_reason = plan_benchmark(s, scope, request, *_benchmark_range(request.params, security.calendar))
    from iirp.analysis.sector_benchmarks import plan_sector, sector_snapshot
    sector_reason = plan_sector(s, scope, request, scope.start_date, scope.end_date)
    sector = sector_snapshot(request.params, security, s, scope=scope)
    if sector and sector["status"] == "sector_identity_pending":
        scope.status, scope.wait_reason = "RUNNING", sector_reason
        return
    benchmark_reason = sector_reason or benchmark_reason
    if status != "READY":
        scope.status, scope.wait_reason = status, reason
        return
    if benchmark_reason and any(job.status in ACTIVE and job.kind in ("market_history", "market_identity")
                                for job in linked_jobs(s, scope.id)):
        scope.status, scope.wait_reason = "RUNNING", benchmark_reason
        return
    _publish(s, request, scope, security, current_cache(s, security.id))
    scope.status, scope.wait_reason = ("PARTIAL", benchmark_reason) if benchmark_reason else ("READY", None)
    scope.checkpoint = {key: value for key, value in (scope.checkpoint or {}).items()
                        if key != "results_expired_at"}


def analysis_view(s, request, filters=None):
    from iirp.analysis.event_windows import project_result
    from iirp.analysis.freshness import freshness
    from iirp.analysis.requests import ticker_progress

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
            "result": project_result(result.data, **(filters or {})) if result else None,
        })
    order = list(dict.fromkeys(event["ticker"] for event in params["events"]))

    def position(item):
        return order.index(item["symbol"]) if item["symbol"] in order else len(order)

    tickers.sort(key=position)
    from iirp.analysis.distributions import comparison
    across = comparison((item["symbol"], item["result"]["summary"]["reaction"]["median"])
                        for item in tickers if item["result"])
    return {
        "comparison": across,
        "macro_excluded": sum(item["result"]["macro_excluded"] for item in tickers if item["result"]),
        "filtered_event_count": sum(item["result"]["event_count"] for item in tickers if item["result"]),
        "id": request.id,
        "batch_id": batch.id,
        "status": batch.status,
        "title": params["title"],
        "event_kind": params["event_kind"],
        "event_set_id": params.get("event_set_id"),
        "n": params["n"],
        "benchmark": params.get("benchmark"),
        "sector_benchmark": params.get("sector_benchmark"),
        "cutoff_date": params["cutoff_date"],
        "event_count": len(params["events"]),
        "created_at": request.created_at.isoformat(),
        "tickers": tickers,
        "progress": sorted(ticker_progress(s, batch, set(results)), key=position),
        "freshness": freshness(s, request),
    }


def get_analysis(identifier, **filters):
    with session() as s:
        request = _required(s, AnalysisRequest, identifier)
        if request.params.get("kind") != KIND:
            raise NotFoundError("events.not_event_analysis")
        return analysis_view(s, request, filters)


def refresh_analysis(identifier, force=False):
    """Fetch again when results expired (automatic) or when asked (``force``)."""
    from iirp.analysis.freshness import refresh_analysis as refresh

    created = refresh(identifier, force)
    return get_analysis(created["id"])


STATS_COLUMNS = ("n", "median", "mean", "q25", "q75", "min", "max", "up", "flat", "up_low", "up_high",
                 "coin_flip", "abs_median", "paired_n", "beat", "median_excess", "mean_excess",
                 "benchmark_median", "sector_paired_n", "sector_beat", "sector_beat_ratio",
                 "sector_median", "sector_median_excess", "sector_mean_excess")
DETAIL_COLUMNS = ("ticker", "name", "date", "session", "fiscal_year", "fiscal_quarter", "reaction_date",
                  "baseline_date")
EARNINGS_COLUMNS = ("eps_actual", "eps_estimate", "revenue_actual", "revenue_estimate", "guidance")


def export_analysis(identifier, table="detail", **filters):
    """The shown results as CSV: statistics per ticker, group and window, or one row per event.

    Prices are deleted after 24 hours; an export is how a result is kept.
    Ratios are decimals (0.0123 = +1.23%).
    """
    if table not in {"stats", "detail"}:
        raise UserError("analysis.export_format_invalid")
    view = get_analysis(identifier, **filters)
    results = [item["result"] for item in view["tickers"] if item["result"]]
    if not results:
        raise UserError("analysis.result_missing")
    stream = io.StringIO()
    writer = csv.writer(stream)
    head = ["n_sessions", "benchmark", "sector_etf", "sector_source", "sector_mode", "sector_fallback", "sector_reason"]
    benchmark = view["benchmark"] or ""
    if table == "stats":
        writer.writerow(["ticker", "group", "window", *head, *STATS_COLUMNS])
        for result in results:
            groups = [("all", result["summary"])] + [
                (f"Q{quarter['fiscal_quarter']}", quarter["summary"]) for quarter in result.get("quarters") or []] + [
                (f"{kind}_{group['key']}", group["summary"]) for kind in ("surprise", "guidance")
                for group in (result.get("groups") or {}).get(kind, [])]
            for group, summary in groups:
                for window in WINDOWS:
                    sector = result.get("sector") or {}
                    writer.writerow([result["symbol"], group, window, result["n"], benchmark,
                                     *(sector.get(key) for key in ("symbol", "source", "mode", "fallback", "reason")),
                                     *(summary[window].get(column) for column in STATS_COLUMNS)])
    else:
        windows = [f"{window}{suffix}" for window in WINDOWS for suffix in ("", "_benchmark", "_excess", "_sector", "_sector_excess")]
        writer.writerow([*DETAIL_COLUMNS, *head, *windows, *EARNINGS_COLUMNS, "eps_surprise", "revenue_surprise",
                         "surprise_group", "uncertain", "context_items", "major_macro", "status", "notes"])
        for result in results:
            for row in result["rows"]:
                values = [row["windows"][window].get(field) for window in WINDOWS
                          for field in ("value", "benchmark", "excess", "sector", "sector_excess")]
                statuses = sorted({row["windows"][window]["status"] for window in WINDOWS})
                sector = result.get("sector") or {}
                figures = row.get("earnings") or {}
                expected = row["expectations"]
                writer.writerow([*(row.get(column) for column in DETAIL_COLUMNS), result["n"], benchmark,
                                 *(sector.get(key) for key in ("symbol", "source", "mode", "fallback", "reason")),
                                 *values, *(figures.get(column) for column in EARNINGS_COLUMNS),
                                 expected["eps_surprise"], expected["revenue_surprise"], expected["surprise_group"],
                                 bool(row.get("uncertain")), len(row.get("context") or []), bool(row["macro_events"]),
                                 " ".join(statuses), " ".join(row["notes"])])
    return "\ufeff" + stream.getvalue()
