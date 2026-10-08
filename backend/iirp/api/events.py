"""Earnings and custom events: prompt templates, pasted JSON, saved sets, analyses (S3)."""

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from iirp.api.schemas import TickerProgress
from iirp.events import prompts as event_prompts
from iirp.events import service as event_service

router = APIRouter(prefix="/api/v1/events", tags=["event research"])

Kind = Literal["earnings", "custom"]
Language = Literal["zh", "en"]
Session = Literal["before_open", "during", "after_close", "unknown"]


class EventStrict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventPromptInput(EventStrict):
    text: str = Field(min_length=1, max_length=event_prompts.MAX_TEXT)


class EventPromptOutput(BaseModel):
    kind: Kind
    language: Language
    text: str
    is_default: bool
    default_text: str
    updated_at: str | None


class EventDefaultsOutput(BaseModel):
    window_sessions: dict[str, int]
    min: int
    max: int
    presets: list[int]
    benchmark: str


class EventValidateInput(EventStrict):
    kind: Kind
    text: str = Field(max_length=512 * 1024)


class EventInputIssue(BaseModel):
    index: int | None = Field(description="1-based position in events; null for the whole document")
    field: str | None
    message: str


class EventItem(BaseModel):
    ticker: str
    date: str
    session: Session
    name: str
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None
    note: str | None = None
    reaction_date: str | None = Field(None, description="R per D23; null outside the exchange calendar")


class EventValidateOutput(BaseModel):
    valid: bool
    errors: list[EventInputIssue]
    events: list[EventItem]
    tickers: list[str]


class EventSetInput(EventStrict):
    kind: Kind
    text: str = Field(max_length=512 * 1024)
    title: str | None = Field(None, max_length=200)
    request_id: str | None = Field(None, min_length=1, max_length=100)
    analyze: bool = False
    n: int | None = None
    benchmark: str | None = Field(event_service.DEFAULT_BENCHMARK, max_length=20,
                                  description="^GSPC, ^IXIC or an ETF; null for no comparison")


class EventSetUpdate(EventStrict):
    title: str | None = Field(None, max_length=200)
    text: str | None = Field(None, max_length=512 * 1024)


class EventAnalysisReference(BaseModel):
    id: str
    created_at: str
    n: int | None
    status: str


class EventSetSummary(BaseModel):
    id: str
    kind: Kind
    title: str
    event_count: int
    tickers: list[str]
    first_date: str | None
    last_date: str | None
    created_at: str
    updated_at: str


class EventSetOutput(EventSetSummary):
    events: list[EventItem]
    analyses: list[EventAnalysisReference]


class EventSetsOutput(BaseModel):
    items: list[EventSetSummary]


class EventDeleted(BaseModel):
    deleted: str


class EventAnalysisInput(EventStrict):
    request_id: str = Field(min_length=1, max_length=128)
    n: int | None = None
    benchmark: str | None = Field(event_service.DEFAULT_BENCHMARK, max_length=20)


class EventVariantInput(EventStrict):
    """Another n and/or benchmark for the same frozen events; omitted fields keep theirs."""
    request_id: str = Field(min_length=1, max_length=128)
    n: int | None = None
    benchmark: str | None = Field(None, max_length=20)


class EventAnalysisCreated(BaseModel):
    analysis_id: str
    batch_id: str
    reused: bool


class EventSetCreated(BaseModel):
    set: EventSetOutput
    analysis: EventAnalysisCreated | None = None


class EventWindow(BaseModel):
    value: str | None = Field(description="Decimal ratio, e.g. -0.0123; null when pending or missing")
    start_date: str
    end_date: str
    status: Literal["ok", "pending", "missing_price"]
    benchmark: str | None = Field(None, description="The benchmark's change over the same dates")
    excess: str | None = Field(None, description="value - benchmark")


class EventReactionCandle(BaseModel):
    """The reaction day relative to C(R-1): open is the gap, close the reaction."""
    open: str | None
    high: str | None
    low: str | None
    close: str


class EventPathPoint(BaseModel):
    offset: int
    date: str
    value: str | None = Field(description="C(t)/C(R-1) - 1")
    benchmark: str | None


class EventCandle(BaseModel):
    offset: int
    date: str
    open: str | None
    high: str | None
    low: str | None
    close: str | None


class EventRow(BaseModel):
    ticker: str
    date: str
    session: Session
    name: str
    note: str | None
    fiscal_year: int | None
    fiscal_quarter: int | None
    reaction_date: str
    baseline_date: str
    windows: dict[str, EventWindow]
    reaction_candle: EventReactionCandle | None
    path: list[EventPathPoint]
    candles: list[EventCandle]
    notes: list[str]


class EventStatistics(BaseModel):
    n: int
    median: str | None = None
    q25: str | None = None
    q75: str | None = None
    mean: str | None = None
    min: str | None = None
    max: str | None = None
    up: int
    flat: int
    up_low: str | None = Field(None, description="Wilson 95% interval of the up share")
    up_high: str | None = None
    coin_flip: bool | None = Field(None, description="The interval contains one half")
    abs_median: str | None = Field(None, description="Median of the absolute changes")
    paired_n: int = 0
    beat: int = Field(0, description="Events whose change exceeded the benchmark's")
    median_excess: str | None = None
    mean_excess: str | None = None
    benchmark_median: str | None = None


class EventPathStatistics(BaseModel):
    offset: int
    n: int
    median: str | None
    q25: str | None
    q75: str | None
    benchmark_median: str | None


class EventBenchmark(BaseModel):
    symbol: str
    status: str


class EventQuarterSummary(BaseModel):
    fiscal_quarter: int
    event_count: int
    summary: dict[str, EventStatistics]


class EventTickerResult(BaseModel):
    kind: Literal["event_windows"]
    event_kind: Kind
    symbol: str
    n: int
    cutoff_date: str
    calendar: str
    calculation_version: str
    price_fetched_at: str | None
    benchmark: EventBenchmark | None
    event_count: int
    summary: dict[str, EventStatistics] = Field(description="before, reaction, gap and after windows")
    path: list[EventPathStatistics]
    quarters: list[EventQuarterSummary] | None = None
    rows: list[EventRow]


class EventTickerView(BaseModel):
    symbol: str
    status: str
    wait_reason: str | None
    price_start: str | None
    price_end: str | None
    expires_at: str | None
    result: EventTickerResult | None


class EventAnalysisOutput(BaseModel):
    id: str
    batch_id: str
    status: str
    title: str
    event_kind: Kind
    event_set_id: str | None
    n: int
    benchmark: str | None
    cutoff_date: str
    event_count: int
    created_at: str
    tickers: list[EventTickerView]
    progress: list[TickerProgress]
    freshness: dict[str, Any]


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/defaults", response_model=EventDefaultsOutput)
def defaults():
    values = event_service.window_defaults()
    return {"window_sessions": {k: values[k] for k in ("insider", "earnings", "custom")},
            "min": values["min"], "max": values["max"], "presets": event_service.PRESETS,
            "benchmark": event_service.DEFAULT_BENCHMARK}


@router.get("/prompts/{kind}", response_model=EventPromptOutput)
def prompt(kind: Kind, language: Language = "zh"):
    return _call(event_prompts.get_prompt, kind, language)


@router.put("/prompts/{kind}", response_model=EventPromptOutput)
def save_prompt(kind: Kind, body: EventPromptInput, language: Language = "zh"):
    return _call(event_prompts.save_prompt, kind, language, body.text)


@router.delete("/prompts/{kind}", response_model=EventPromptOutput)
def reset_prompt(kind: Kind, language: Language = "zh"):
    return _call(event_prompts.reset_prompt, kind, language)


@router.post("/validate", response_model=EventValidateOutput)
def validate(body: EventValidateInput):
    return _call(event_service.validate, body.kind, body.text)


@router.get("/sets", response_model=EventSetsOutput)
def sets(kind: Kind | None = None):
    return _call(event_service.list_sets, kind)


@router.post("/sets", response_model=EventSetCreated, status_code=201)
def create_set(body: EventSetInput):
    return _call(event_service.create_set, body.model_dump())


@router.get("/sets/{set_id}", response_model=EventSetOutput)
def get_set(set_id: str):
    return _call(event_service.get_set, set_id)


@router.put("/sets/{set_id}", response_model=EventSetOutput)
def update_set(set_id: str, body: EventSetUpdate):
    return _call(event_service.update_set, set_id, body.model_dump())


@router.delete("/sets/{set_id}", response_model=EventDeleted)
def delete_set(set_id: str):
    return _call(event_service.delete_set, set_id)


@router.post("/sets/{set_id}/analyses", response_model=EventAnalysisCreated, status_code=202)
def create_analysis(set_id: str, body: EventAnalysisInput):
    return _call(event_service.create_analysis, set_id, body.model_dump())


@router.get("/analyses/{analysis_id}", response_model=EventAnalysisOutput)
def analysis(analysis_id: str):
    return _call(event_service.get_analysis, analysis_id)


@router.post("/analyses/{analysis_id}/refresh", response_model=EventAnalysisOutput)
def refresh(analysis_id: str, force: bool = False):
    return _call(event_service.refresh_analysis, analysis_id, force)


@router.post("/analyses/{analysis_id}/variant", response_model=EventAnalysisCreated, status_code=202)
def variant(analysis_id: str, body: EventVariantInput):
    return _call(event_service.analysis_variant, analysis_id, body.model_dump(exclude_unset=True))


@router.get("/analyses/{analysis_id}/export")
def export(analysis_id: str, table: str = Query(default="detail", pattern="^(stats|detail)$")):
    content = _call(event_service.export_analysis, analysis_id, table)
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="iirp-events-{analysis_id}-{table}.csv"'},
    )
