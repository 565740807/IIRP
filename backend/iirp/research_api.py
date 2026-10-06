"""Local reads and durable commands for research; no GET invokes a provider."""

import csv
import io
import json
from datetime import date

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from iirp.contracts import (
    AnalysisInput,
    AnalysisOutput,
    AnalysisRefreshOutput,
    BatchAction,
    BatchesOutput,
    CollectionInput,
    CollectionOutput,
    FeedGroupOutput,
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

router = APIRouter(prefix="/api/v1")


@router.get("/feed/groups/{group_id}", response_model=FeedGroupOutput)
def feed_group(group_id: str, session_id: str, cursor: str = "", trader_key: str = ""):
    from iirp.db import session
    from iirp.sec_facts import feed_group as read_group

    try:
        with session() as s, s.begin():
            return read_group(s, session_id, group_id, cursor, trader_key=trader_key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def invoke(name, *args, **kwargs):
    from iirp import lifecycle

    try:
        return getattr(lifecycle, name)(*args, **kwargs)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/collections", response_model=CollectionOutput, status_code=202)
def create_collection(body: CollectionInput):
    from iirp.providers import SEC_USER_AGENT_HINT, sec_configured

    if body.kind.startswith("sec_") and not sec_configured():
        raise HTTPException(409, SEC_USER_AGENT_HINT)
    return invoke("create_collection", body.model_dump(mode="json"))


@router.get("/price-range", response_model=PriceRangeView)
def price_range(parameters: str = Query(default="{}", max_length=8192)):
    try:
        parsed = PriceRangeInput.model_validate_json(parameters)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return invoke("preview_price_range", **parsed.model_dump(mode="json"))


@router.post("/price-range", response_model=PriceRangeView)
def price_range_document(body: PriceRangeInput):
    """The same read-only planner with a typed body for API clients."""
    return invoke("preview_price_range", **body.model_dump(mode="json"))


@router.get("/batches", response_model=BatchesOutput)
def batches(category: str = "all", cursor: str = "", policy_key: str = "", view: str = "all"):
    return invoke("list_batches", category, cursor, policy_key, view)


@router.get("/batches/{batch_id}", response_model=CollectionOutput)
def batch(batch_id: str):
    return invoke("get_batch", batch_id)


@router.get("/batches/{batch_id}/jobs", response_model=GenericOutput)
def jobs(batch_id: str, cursor: str = "", limit: int = 50):
    return invoke("batch_jobs", batch_id, cursor, limit)


@router.post("/batches/{batch_id}/actions", response_model=CollectionOutput)
def batch_action(batch_id: str, body: BatchAction):
    return invoke("control_batch", batch_id, body.action)


@router.post("/analyses", response_model=AnalysisOutput, status_code=202)
def analysis_create(body: AnalysisInput):
    return invoke("create_analysis", body.model_dump(mode="json"))


@router.get("/analyses", response_model=RecentAnalysesOutput)
def recent_analyses(kind: str = "monthly", ticker: str = ""):
    return invoke("recent_analyses", kind, ticker)


@router.get("/analyses/{analysis_id}", response_model=AnalysisOutput)
def analysis(analysis_id: str, result_ids: str = ""):
    return invoke("get_analysis", analysis_id, result_ids)


@router.post("/analyses/{analysis_id}/refresh", response_model=AnalysisRefreshOutput, status_code=202)
def analysis_refresh(analysis_id: str, force: bool = False):
    return invoke("refresh_analysis", analysis_id, force=force)


@router.get("/analyses/{analysis_id}/export")
def export(analysis_id: str, result_ids: str = "", format: str = Query(default="csv", pattern="^(csv|json)$")):
    content = invoke("export_analysis", analysis_id, result_ids, format)
    return Response(
        content,
        media_type="application/json" if format == "json" else "text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="iirp-{analysis_id}.{format}"'},
    )


@router.get("/collection-policy", response_model=StrategyOutput)
def policies():
    return invoke("get_strategies")


@router.patch("/collection-policy", response_model=StrategyOutput)
def policy_update(body: StrategyInput):
    return invoke("update_strategy", body.key, body.enabled)


@router.get("/preferences", response_model=PreferenceOutput)
def preferences():
    return invoke("get_preferences")


@router.patch("/preferences", response_model=PreferenceOutput)
def preferences_update(body: PreferenceInput):
    return invoke("update_preferences", body.model_dump())


@router.get("/coverage", response_model=GenericOutput)
def coverage(ticker: str = "", security_id: str = "", start_date: str = "", end_date: str = ""):
    return invoke("get_coverage", ticker, security_id, start_date, end_date)


@router.get("/companies/{entity_id}", response_model=GenericOutput)
def company(
    entity_id: str,
    start_date: str = "",
    end_date: str = "",
    recent_count: int = 0,
    date_basis: str = "transaction",
    cursor: str = "",
    limit: int = 50,
    action: str = "all",
):
    return invoke(
        "entity_history",
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


@router.get("/people/{entity_id}", response_model=GenericOutput)
def person(
    entity_id: str,
    issuer_id: str = "",
    start_date: str = "",
    end_date: str = "",
    recent_count: int = 0,
    date_basis: str = "transaction",
    cursor: str = "",
    limit: int = 50,
    action: str = "all",
):
    return invoke(
        "entity_history",
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


@router.get("/transactions/{transaction_id}", response_model=GenericOutput)
def transaction(transaction_id: str, mapping_version: int | None = None, cutoff_date: date | None = None):
    return invoke("transaction_detail", transaction_id, mapping_version, cutoff_date.isoformat() if cutoff_date else None)


@router.get("/transactions/{transaction_id}/export")
def transaction_export(transaction_id: str, format: str = "json", mapping_version: int | None = None, cutoff_date: date | None = None):
    if format not in {"json", "csv"}:
        raise HTTPException(422, "交易价格导出格式只支持 JSON 或 CSV")
    result = invoke("transaction_detail", transaction_id, mapping_version, cutoff_date.isoformat() if cutoff_date else None)["data"]
    transaction = result["transaction"]
    context = result["price_context"]
    if not transaction.get("security_id"):
        raise HTTPException(409, "交易证券尚未核对，不能导出价格分析")
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
    return invoke("market_detail", symbol)


@router.post("/cache/cleanup", response_model=GenericOutput, status_code=202)
def cleanup():
    return invoke("cleanup_cache")


@router.post("/amendments/{relation_id}/resolve", response_model=GenericOutput)
def resolve_amendment(
    relation_id: str, action: str, original_event_id: str = "", evidence: str = ""
):
    return invoke("resolve_amendment", relation_id, action, original_event_id, evidence)


@router.post("/transactions/{transaction_id}/security", response_model=GenericOutput)
def map_security(transaction_id: str, body: TransactionSecurityInput):
    return invoke("map_transaction_security", transaction_id, body.model_dump())


@router.post(
    "/transactions/{transaction_id}/price-window", response_model=CollectionOutput, status_code=202
)
def price_window(transaction_id: str, body: RequestIdentity):
    return invoke("transaction_window", transaction_id, body.request_id)


@router.get("/freshness", response_model=FreshnessOutput)
def freshness_status():
    from iirp.freshness import get_freshness

    return get_freshness()


@router.post("/freshness/ensure", response_model=FreshnessOutput, status_code=202)
def freshness_ensure(values: FreshnessInput):
    from iirp.freshness import ensure_fresh

    return ensure_fresh(values.model_dump())
