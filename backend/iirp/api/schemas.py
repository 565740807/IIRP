"""The single public request/response contract, exported through OpenAPI."""

import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from iirp.analysis.research import ResearchResult
from iirp.insider.tickers import normalized_ticker
from iirp.messages import UserError


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Output(BaseModel):
    """Response model: fields with defaults are always present in the JSON."""
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class CollectionInput(Strict):
    request_id: str = Field(min_length=1, max_length=128)
    kind: Literal[
        "market_history", "market_quotes", "sec_latest", "sec_history", "sec_filing"
    ]
    tickers: list[str] = Field(default_factory=list, max_length=20)
    historical_years: int | None = Field(default=None, ge=1)
    start_date: date | None = None
    end_date: date | None = None
    filing_url: str | None = Field(default=None, max_length=2000)
    issuer_id: str | None = None
    owner_id: str | None = None
    history_months: int | None = Field(default=None, ge=1)
    recent_count: int | None = Field(default=None, ge=1, le=500)
    date_basis: Literal["transaction", "accepted"] = "transaction"
    purpose: str = Field(default="research", max_length=64)
    intent: Literal["fetch", "fill_missing", "refresh"] = "fetch"

    @model_validator(mode="after")
    def validate_scope(self):
        self.tickers = list(dict.fromkeys(x.strip().upper() for x in self.tickers if x.strip()))
        if any(not normalized_ticker(x) or not re.fullmatch(r"[A-Z0-9^][A-Z0-9.^=\-]{0,19}", x) for x in self.tickers):
            raise UserError("input.ticker_invalid")
        if self.kind == "market_history" and not self.tickers:
            raise UserError("input.ticker_required")
        if self.kind == "sec_filing":
            from iirp.sec.parse import validate_sec_url

            if not self.filing_url:
                raise UserError("input.filing_url_required")
            validate_sec_url(self.filing_url)
            if not re.search(r"/\d{10}-\d{2}-\d{6}\.txt$", self.filing_url):
                raise UserError("input.filing_url_accession")
        elif self.filing_url:
            raise UserError("input.filing_url_unexpected")
        if bool(self.start_date) != bool(self.end_date):
            raise UserError("common.dates_together")
        if self.start_date and self.start_date > self.end_date:
            raise UserError("common.start_after_end")
        return self


class AnalysisInput(Strict):
    """Monthly or interval research. Years are this year plus ``historical_years``
    complete past years; an interval end before its start crosses the year end."""
    request_id: str = Field(min_length=1, max_length=128)
    kind: Literal["monthly", "interval"] = "monthly"
    tickers: list[str] = Field(min_length=1, max_length=20)
    historical_years: int = Field(default=8, ge=1, le=30)
    # Defaults to this year (for an interval, the latest one already begun).
    current_year: int | None = Field(default=None, ge=1900, le=9998)
    start_mmdd: str = Field(default="09-20", pattern=r"^\d{2}-\d{2}$")
    end_mmdd: str = Field(default="10-15", pattern=r"^\d{2}-\d{2}$")
    # S&P 500 unless null (no comparison).
    benchmark: str | None = "^GSPC"

    @model_validator(mode="after")
    def validate_parameters(self):
        from iirp.analysis.benchmarks import validate_benchmark

        self.benchmark = validate_benchmark(self.benchmark)
        self.tickers = list(dict.fromkeys(x.strip().upper() for x in self.tickers if x.strip()))
        if not self.tickers or any(
            not normalized_ticker(x) or not re.fullmatch(r"[A-Z0-9^][A-Z0-9.^=\-]{0,19}", x) for x in self.tickers
        ):
            raise UserError("input.ticker_enter_valid")
        for x in (self.start_mmdd, self.end_mmdd):
            date.fromisoformat("2000-" + x)
        if self.kind == "interval" and self.start_mmdd == self.end_mmdd:
            raise UserError("input.interval_empty")
        return self


class BatchAction(Strict):
    action: Literal["pause", "resume", "cancel", "retry_failed", "continue_remaining"]


class CoverageView(BaseModel):
    start_date: str | None = None
    end_date: str | None = None
    expected_sessions: int = 0
    available_sessions: int = 0
    valid_sessions: int = 0
    missing_dates: list[str] = Field(default_factory=list)
    status: str = "NOT_FETCHED"
    basis: str = "UNVERIFIED_PROVIDER_RECORDS"
    cache_id: str | None = None
    as_of: str | None = None
    expires_at: str | None = None
    first_valid_date: str | None = None
    last_valid_date: str | None = None
    reasons: list[str] = Field(default_factory=list)


class ScopeView(BaseModel):
    id: str
    symbol: str
    security_id: str | None = None
    status: str
    wait_reason: str | None = None
    coverage: CoverageView
    jobs_done: int = 0
    jobs_total: int = 0
    progress: dict[str, Any] = Field(default_factory=dict)


class PriceRangeView(BaseModel):
    target_start_date: str
    target_end_date: str
    collection_start_date: str
    collection_end_date: str
    completed_through: str
    historical_years: int
    basis: Literal["complete_natural_years", "research_conditions", "explicit_dates", "legacy_scope"]
    explanation: str
    buffer_explanation: str | None = None


class PriceRangeInput(Strict):
    historical_years: int | None = Field(default=None, ge=1)
    start_date: date | None = None
    end_date: date | None = None
    analysis: AnalysisInput | None = None

    @model_validator(mode="after")
    def dates(self):
        if bool(self.start_date) != bool(self.end_date):
            raise UserError("common.dates_together")
        return self


class BatchView(BaseModel):
    analysis_id: str | None = None
    id: str
    kind: str
    title: str
    status: str
    params: dict[str, Any]
    price_range: PriceRangeView | None = None
    items: list[ScopeView]
    created_at: datetime
    updated_at: datetime
    parent_id: str | None = None
    requested_action: str | None = None
    trigger: str = "manual"
    policy_key: str | None = None
    history_count: int = 1
    activity_status: str | None = None


class CollectionOutput(BaseModel):
    batch_id: str
    reused: bool
    batch: BatchView


class BatchesOutput(BaseModel):
    items: list[BatchView]
    next_cursor: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)


class AnalysisItem(Output):
    symbol: str
    security_id: str
    result_id: str
    created_at: datetime | None = None
    # When the prices behind this result were fetched; it expires with them.
    data_published_at: datetime | None = None
    expires_at: datetime | None = None
    data: ResearchResult


class TickerProgress(Output):
    """One ticker's step: download prices → compute → done (or failed / waiting)."""
    symbol: str
    step: Literal["queued", "download", "compute", "done", "failed"]
    reason: str | None = None


class CacheSource(BaseModel):
    symbol: str
    fetched_at: str
    expires_at: str


class ResearchFreshness(BaseModel):
    sources: list[CacheSource] = Field(default_factory=list)
    origin_id: str
    latest_id: str
    latest_completed_session: str
    research_cutoff: str | None = None
    price_fetched_at: datetime | None = None
    price_expires_at: datetime | None = None
    expired: bool = False
    refresh_checked_at: datetime | None = None
    note: str


class AnalysisRefreshOutput(BaseModel):
    """Command receipt shared by native and custom-event research kinds."""
    id: str
    batch_id: str
    status: str
    freshness: ResearchFreshness | None = None


class RecentAnalysisView(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    params: dict[str, Any]
    created_at: str
    status: str


class RecentAnalysesOutput(BaseModel):
    data: dict[str, Any]
    items: list[RecentAnalysisView]


class TickerComparison(BaseModel):
    count: int
    positive: int
    high: str | None
    high_ticker: str | None
    low: str | None
    low_ticker: str | None


class AnalysisOutput(BaseModel):
    comparisons: dict[str, TickerComparison] = Field(default_factory=dict)
    id: str
    batch_id: str
    status: str
    params: dict[str, Any]
    results: list[AnalysisItem]
    progress: list[TickerProgress] = Field(default_factory=list)
    batch: BatchView
    freshness: ResearchFreshness | None = None


class StrategyView(BaseModel):
    key: str
    enabled: bool
    version: int
    options: dict[str, Any]
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    # Enabled but unable to run, e.g. SEC without a real contact User-Agent.
    blocked_reason: str | None = None


class StrategyInput(Strict):
    key: Literal["sec", "market", "backup", "maintenance"]
    enabled: bool


class StrategyOutput(BaseModel):
    items: list[StrategyView]


class PreferenceInput(Strict):
    automatic_history: bool = True
    historical_years: int = Field(default=8, ge=1)
    history_months: int = Field(default=3, ge=1)
    comparison: Literal["same_progress", "complete"] = "same_progress"


class PreferenceOutput(BaseModel):
    values: PreferenceInput
    version: int


class GenericOutput(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class TransactionSecurityInput(Strict):
    security_id: str = Field(min_length=1, max_length=36)
    evidence: str = Field(min_length=5, max_length=2000)


class RequestIdentity(Strict):
    request_id: str = Field(min_length=1, max_length=128)


class QuoteSession(Output):
    start: str
    end: str
    timezone: str | None = None


class HomeQuote(Output):
    """One index (or gold futures) quote as saved from its latest fetch.

    ``freshness`` is judged at read time: ``live`` in session and on time,
    ``closed`` outside the session (``as_of`` is the last trading day),
    ``delayed`` when overdue, stale at the source or the last refresh failed
    (``reason`` says which), ``missing`` before the first fetch.
    """
    symbol: str
    name: str
    value: float | None = None
    change: float | None = None
    change_percent: float | None = None
    previous_close: str | None = None
    baseline_date: str | None = None
    as_of: str | None = None
    source_time: str | None = None
    fetched_at: str | None = None
    next_refresh_at: str | None = None
    market_open: bool | None = None
    session: QuoteSession | None = None
    status: str
    unit: str | None = None
    baseline: str | None = None
    freshness: Literal["live", "closed", "delayed", "missing"]
    reason: str | None = None


class MarketBar(Output):
    date: str
    open: str | None = None
    high: str | None = None
    low: str | None = None
    close: str | None = None


class MarketChart(Output):
    """Where the daily bars come from: ``history`` (the six-month fetch, 24 hours),
    ``cache`` (a cached price range), ``quote`` (the quote's last 40 days) or none."""
    source: Literal["history", "cache", "quote"] | None = None
    start: str | None = None
    end: str | None = None
    fetched_at: str | None = None
    expires_at: str | None = None
    days: int


class MarketHistoryState(Output):
    """``missing``: the page should ask for six months; ``fetching``; ``failed`` today; ``fresh``."""
    status: Literal["fresh", "fetching", "failed", "missing"]
    reason: str | None = None


class MarketDetailOutput(Output):
    symbol: str
    quote: HomeQuote
    instrument: str | None = None
    delay: str | None = None
    source: str | None = None
    bars: list[MarketBar]
    chart: MarketChart
    history: MarketHistoryState


class BrowserRefresh(Output):
    """How often an open page re-reads and asks for fresh data (config/refresh.toml)."""
    home_poll_seconds: int
    feed_poll_seconds: int
    ensure_seconds: int


class HomeOutput(Output):
    observed_at: datetime
    market: list[HomeQuote]
    refresh: BrowserRefresh


class SecUserAgentState(Output):
    configured: bool
    status: Literal["CONFIGURED", "NEEDS_CONFIG"]
    message: str
    source: Literal["config_file", "web", "none"]


class SecContactInput(Strict):
    name: str = Field(max_length=200)
    email: str = Field(max_length=254)


class SecContactOutput(Output):
    """SEC contact for the User-Agent; ``config_file`` (deploy/.env) wins and is read-only here."""
    configured: bool
    source: Literal["config_file", "web", "none"]
    editable: bool
    name: str | None
    email: str | None


class WorkerState(Output):
    """Worker liveness from its heartbeat; online is null when the read failed."""
    online: bool | None
    last_seen: datetime | None = None
    error: str | None = None


class BackupEntry(Output):
    model_config = ConfigDict(extra="allow")
    name: str
    completed_at: str | None = None
    restore_verified_at: str | None = None
    object_count: int | None = None
    object_bytes: int | None = None
    dump_bytes: int | None = None


class StoragePath(Output):
    path: str
    logical_bytes: int | None = None
    allocated_bytes: int | None = None
    files: int | None = None


class MaintenanceRecord(Output):
    kind: str
    status: str
    created_at: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class StorageState(Output):
    """Recorded sizes (no directory walk); capacity verdicts are null when unknown."""
    model_config = ConfigDict(extra="allow")
    objects: int | None = None
    bytes: int | None = None
    evidence: int | None = None
    cache: int | None = None
    logs: int | None = None
    backups: int | None = None
    restores: int | None = None
    database: int | None = None
    database_server: int | None = None
    expiring_objects: int | None = None
    expiring_bytes: int | None = None
    analysis_cache_bytes: int | None = None
    backup_list: list[BackupEntry] = Field(default_factory=list)
    restore_list: list[dict[str, Any]] = Field(default_factory=list)
    recent_maintenance: list[MaintenanceRecord] = Field(default_factory=list)
    paths: dict[str, StoragePath] = Field(default_factory=dict)
    backup_estimated_temporary_bytes: int | None = None
    restore_estimated_temporary_bytes: int | None = None
    estimate_scope: str | None = None
    inventory_status: str | None = None
    inventory_measured_at: str | None = None
    inventory_attempted_at: str | None = None
    inventory_error: str | None = None
    inventory_age_seconds: float | None = None
    exact_status: str | None = None
    exact_measured_at: str | None = None
    exact_error: str | None = None
    exact_seconds: float | None = None
    free_bytes: int | None = None
    min_free_bytes: int | None = None
    disk_pressure: bool | None = None
    backup_capacity_sufficient: bool | None = None
    restore_capacity_sufficient: bool | None = None


class SystemOutput(Output):
    version: str
    mode: str
    worker: WorkerState
    migration: str
    automatic_collection_scope: str
    sec_user_agent: SecUserAgentState
    storage: StorageState


class SearchItem(Output):
    id: str
    kind: Literal["company", "person", "security"]
    name: str
    href: str
    ticker: str | None = None
    security_id: str | None = None
    issuer_id: str | None = None


class SearchData(Output):
    message: str


class SearchOutput(Output):
    """Local matches only; searching never starts a download."""
    items: list[SearchItem]
    data: SearchData


class FreshnessInput(Strict):
    reason: Literal["open", "resume", "manual", "visible"] = "open"
    sources: list[Literal["sec", "market"]] = Field(
        default_factory=lambda: ["sec", "market"], min_length=1, max_length=2
    )
    force: bool = False
    market_visible: bool = False


class FreshnessOutput(BaseModel):
    observed_at: str | None = None
    sources: dict[str, Any]
    batch_ids: list[str] = Field(default_factory=list)
