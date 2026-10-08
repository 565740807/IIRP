"""Local reads and durable commands for research; no GET invokes a provider."""

import csv
import io
import json
from datetime import date

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from iirp.analysis import requests as analysis_requests
from iirp.api.insider_schemas import (
    EntityHistoryOutput,
    FeedGroupOutput,
    FeedUpdatesOutput,
    TransactionDetailOutput,
)
from iirp.api.schemas import (
    AnalysisInput,
    AnalysisOutput,
    AnalysisRefreshOutput,
    BatchAction,
    BatchesOutput,
    CollectionInput,
    CollectionOutput,
    FreshnessInput,
    FreshnessOutput,
    GenericOutput,
    PreferenceInput,
    PreferenceOutput,
    PriceRangeInput,
    PriceRangeView,
    RecentAnalysesOutput,
    RequestIdentity,
    StrategyInput,
    StrategyOutput,
    TransactionSecurityInput,
)
from iirp.insider import transactions
from iirp.jobs import batch_views
from iirp.jobs import batches as job_batches
from iirp.jobs import preferences as job_preferences
from iirp.market import reads as market_reads
from iirp.messages import msg

router = APIRouter(prefix="/api/v1")


@router.get("/feed/updates", response_model=FeedUpdatesOutput)
def feed_updates(
    session_id: str = Query(min_length=1, max_length=36),
    include_groups: bool = False,
    target_session_id: str = Query(default="", max_length=36),
    cursor: str = Query(default="", max_length=12),
):
    """Count changes since a reading watermark, or read one bounded delta page."""
    from iirp.db import session
    from iirp.insider.feed_updates import feed_updates as read_updates

    try:
        with session() as s, s.begin():
            return read_updates(s, session_id, include_groups=include_groups,
                                target_session_id=target_session_id, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/feed/groups/{group_id}", response_model=FeedGroupOutput)
def feed_group(group_id: str, session_id: str, cursor: str = "", trader_key: str = ""):
    from iirp.db import session
    from iirp.insider.feed import feed_group as read_group

    try:
        with session() as s, s.begin():
            return read_group(s, session_id, group_id, cursor, trader_key=trader_key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def invoke(command, *args, **kwargs):
    try:
        return command(*args, **kwargs)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/collections", response_model=CollectionOutput, status_code=202)
def create_collection(body: CollectionInput):
    from iirp.jobs.providers import SEC_USER_AGENT_HINT, sec_configured

    if body.kind.startswith("sec_") and not sec_configured():
        raise HTTPException(409, SEC_USER_AGENT_HINT)
    return invoke(job_batches.create_collection, body.model_dump(mode="json"))


@router.get("/price-range", response_model=PriceRangeView)
def price_range(parameters: str = Query(default="{}", max_length=8192)):
    try:
        parsed = PriceRangeInput.model_validate_json(parameters)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return invoke(market_reads.preview_price_range, **parsed.model_dump(mode="json"))


@router.post("/price-range", response_model=PriceRangeView)
def price_range_document(body: PriceRangeInput):
    """The same read-only planner with a typed body for API clients."""
    return invoke(market_reads.preview_price_range, **body.model_dump(mode="json"))


@router.get("/batches", response_model=BatchesOutput)
def batches(category: str = "all", cursor: str = "", policy_key: str = "", view: str = "all", kind: str = "all"):
    return invoke(batch_views.list_batches, category, cursor, policy_key, view, kind)


@router.get("/batches/{batch_id}", response_model=CollectionOutput)
def batch(batch_id: str):
    return invoke(batch_views.get_batch, batch_id)


@router.get("/batches/{batch_id}/jobs", response_model=GenericOutput)
def jobs(batch_id: str, cursor: str = "", limit: int = 50):
    return invoke(batch_views.batch_jobs, batch_id, cursor, limit)


@router.post("/batches/{batch_id}/actions", response_model=CollectionOutput)
def batch_action(batch_id: str, body: BatchAction):
    return invoke(job_batches.control_batch, batch_id, body.action)


@router.post("/analyses", response_model=AnalysisOutput, status_code=202)
def analysis_create(body: AnalysisInput):
    return invoke(analysis_requests.create_analysis, body.model_dump(mode="json"))


@router.get("/analyses", response_model=RecentAnalysesOutput)
def recent_analyses(kind: str = "monthly", ticker: str = ""):
    return invoke(analysis_requests.recent_analyses, kind, ticker)


@router.get("/analyses/{analysis_id}", response_model=AnalysisOutput)
def analysis(analysis_id: str):
    return invoke(analysis_requests.get_analysis, analysis_id)


@router.post("/analyses/{analysis_id}/refresh", response_model=AnalysisRefreshOutput, status_code=202)
def analysis_refresh(analysis_id: str, force: bool = False):
    return invoke(analysis_requests.refresh_analysis, analysis_id, force=force)


@router.get("/analyses/{analysis_id}/export")
def export(analysis_id: str, table: str = Query(default="detail", pattern="^(stats|detail)$")):
    content = invoke(analysis_requests.export_analysis, analysis_id, table)
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="iirp-{analysis_id}-{table}.csv"'},
    )


@router.get("/collection-policy", response_model=StrategyOutput)
def policies():
    return invoke(job_preferences.get_strategies)


@router.patch("/collection-policy", response_model=StrategyOutput)
def policy_update(body: StrategyInput):
    return invoke(job_preferences.update_strategy, body.key, body.enabled)


@router.get("/preferences", response_model=PreferenceOutput)
def preferences():
    return invoke(job_preferences.get_preferences)


@router.patch("/preferences", response_model=PreferenceOutput)
def preferences_update(body: PreferenceInput):
    return invoke(job_preferences.update_preferences, body.model_dump())


@router.get("/coverage", response_model=GenericOutput)
def coverage(ticker: str = "", security_id: str = "", start_date: str = "", end_date: str = ""):
    return invoke(market_reads.get_coverage, ticker, security_id, start_date, end_date)


@router.get("/companies/{entity_id}", response_model=EntityHistoryOutput)
def company(
    entity_id: str,
    start_date: str = "",
    end_date: str = "",
    recent_count: int = 0,
    date_basis: str = "transaction",
    cursor: str = "",
    limit: int = 200,
    action: str = "all",
):
    return invoke(transactions.entity_history,
        "company",
        entity_id,
        start_date,
        end_date,
        recent_count,
        date_basis,
        cursor,
        limit,
        action=action,
    )


@router.get("/people/{entity_id}", response_model=EntityHistoryOutput)
def person(
    entity_id: str,
    issuer_id: str = "",
    start_date: str = "",
    end_date: str = "",
    recent_count: int = 0,
    date_basis: str = "transaction",
    cursor: str = "",
    limit: int = 200,
    action: str = "all",
):
    return invoke(transactions.entity_history,
        "person",
        entity_id,
        start_date,
        end_date,
        recent_count,
        date_basis,
        cursor,
        limit,
        issuer_id,
        action,
    )


@router.get("/transactions/{transaction_id}", response_model=TransactionDetailOutput)
def transaction(transaction_id: str, mapping_version: int | None = None, cutoff_date: date | None = None,
                n: int | None = None):
    from iirp.api.insider import _n

    return invoke(transactions.transaction_detail, transaction_id, mapping_version,
                  cutoff_date.isoformat() if cutoff_date else None, _n(n))


@router.get("/transactions/{transaction_id}/export")
def transaction_export(transaction_id: str, format: str = "json", mapping_version: int | None = None, cutoff_date: date | None = None):
    if format not in {"json", "csv"}:
        raise HTTPException(422, msg("transaction.export_format_invalid"))
    result = invoke(transactions.transaction_detail, transaction_id, mapping_version, cutoff_date.isoformat() if cutoff_date else None)["data"]
    transaction = result["transaction"]
    context = result["price_context"]
    if not transaction.get("security_id"):
        raise HTTPException(409, msg("transaction.export_unverified"))
    filename = f"insider-{transaction_id}-mapping-{transaction['mapping_version']}"
    if format == "json":
        body = json.dumps(result, ensure_ascii=False, default=str)
        return Response(body, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{filename}.json"'})
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["transaction_id", "mapping_version", "security_id", "price_fetched_at", "price_basis", "cutoff_date", "time_basis", "baseline_date", "position", "date", "close", "value", "status"])
    for basis in ("transaction", "disclosure"):
        item = context.get(basis) or {}
        for point in item.get("points", []):
            writer.writerow([transaction_id, transaction["mapping_version"], transaction["security_id"], context.get("price_fetched_at"), context.get("price_basis"), context.get("cutoff_date"), basis, item.get("baseline_date"), point.get("x"), point.get("date"), point.get("close"), point.get("value"), point.get("status")])
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'})


@router.get("/market/{symbol}", response_model=GenericOutput)
def market(symbol: str):
    return invoke(market_reads.market_detail, symbol)


@router.post("/cache/cleanup", response_model=GenericOutput, status_code=202)
def cleanup():
    return invoke(job_batches.cleanup_cache)


@router.post("/amendments/{relation_id}/resolve", response_model=GenericOutput)
def resolve_amendment(
    relation_id: str, action: str, original_event_id: str = "", evidence: str = ""
):
    return invoke(transactions.resolve_amendment, relation_id, action, original_event_id, evidence)


@router.post("/transactions/{transaction_id}/security", response_model=GenericOutput)
def map_security(transaction_id: str, body: TransactionSecurityInput):
    return invoke(transactions.map_transaction_security, transaction_id, body.model_dump())


@router.post(
    "/transactions/{transaction_id}/price-window", response_model=CollectionOutput, status_code=202
)
def price_window(transaction_id: str, body: RequestIdentity):
    return invoke(transactions.transaction_window, transaction_id, body.request_id)


@router.get("/freshness", response_model=FreshnessOutput)
def freshness_status():
    from iirp.jobs.auto_update import get_freshness

    return get_freshness()


@router.post("/freshness/ensure", response_model=FreshnessOutput, status_code=202)
def freshness_ensure(values: FreshnessInput):
    from iirp.jobs.auto_update import ensure_fresh

    return ensure_fresh(values.model_dump())
