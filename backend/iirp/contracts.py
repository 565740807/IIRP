"""The single public request/response contract, exported through OpenAPI."""

import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from iirp.analytics.research import ResearchResult
from iirp.ticker_identity import normalized_ticker


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CollectionInput(Strict):
    request_id: str = Field(min_length=1, max_length=128)
    kind: Literal[
        "market_history", "market_quotes", "sec_latest", "sec_history", "sec_filing", "earnings"
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
            raise ValueError("证券代码格式不合法")
        if self.kind in ("market_history", "earnings") and not self.tickers:
            raise ValueError("请选择至少一个证券")
        if self.kind == "sec_filing":
            from iirp.sec_sources import validate_sec_url

            if not self.filing_url:
                raise ValueError("请提供 SEC 完整申报 txt 地址")
            validate_sec_url(self.filing_url)
            if not re.search(r"/\d{10}-\d{2}-\d{6}\.txt$", self.filing_url):
                raise ValueError("请提供以 accession.txt 结尾的完整申报地址")
        elif self.filing_url:
            raise ValueError("只有指定申报类型接受完整申报地址")
        if bool(self.start_date) != bool(self.end_date):
            raise ValueError("起止日期需要一起提供")
        if self.start_date and self.start_date > self.end_date:
            raise ValueError("开始日期不能晚于结束日期")
        return self


class AnalysisInput(Strict):
    research_label: str | None = Field(default=None, max_length=100)
    request_id: str = Field(min_length=1, max_length=128)
    kind: Literal["monthly", "interval", "earnings"] = "monthly"
    tickers: list[str] = Field(min_length=1, max_length=20)
    historical_years: int = Field(default=8, ge=1)
    years: list[int] | None = None
    excluded_years: list[int] = Field(default_factory=list)
    current_year: int | None = Field(default=None, ge=1, le=9998)
    current_fiscal_year: int | None = Field(default=None, ge=1, le=9998)
    month: int = Field(default=1, ge=1, le=12)
    comparison: Literal["same_progress", "complete"] = "same_progress"
    alignment: Literal["calendar", "trading"] = "calendar"
    start_mmdd: str = Field(default="01-01", pattern=r"^\d{2}-\d{2}$")
    end_mmdd: str = Field(default="12-31", pattern=r"^\d{2}-\d{2}$")
    cross_year: bool | None = None
    quarter: int = Field(default=1, ge=1, le=4)
    window: Literal[1, 5, 20, 60] = 5
    date_window: Literal["before5", "day0", "after5", "through5"] = "after5"
    date_category: Literal["unassigned_earnings"] | None = None
    common_years: bool = False
    benchmark: str | None = None

    @model_validator(mode="after")
    def validate_parameters(self):
        from iirp.benchmarks import validate_benchmark

        self.benchmark = validate_benchmark(self.benchmark)
        self.tickers = list(dict.fromkeys(x.strip().upper() for x in self.tickers if x.strip()))
        if not self.tickers or any(
            not normalized_ticker(x) or not re.fullmatch(r"[A-Z0-9^][A-Z0-9.^=\-]{0,19}", x) for x in self.tickers
        ):
            raise ValueError("请输入有效证券代码")
        for x in (self.start_mmdd, self.end_mmdd):
            date.fromisoformat("2000-" + x)
        if self.years is not None and not self.years:
            raise ValueError("至少选择一个历史年份")
        if any(y < 1 or y > 9998 for y in [*(self.years or []), *self.excluded_years]):
            raise ValueError("年份超出日期支持范围")
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
    dataset_id: str | None = None
    as_of: str | None = None
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
            raise ValueError("起止日期需要一起提供")
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


class ResearchCoverage(BaseModel):
    start_date: str
    end_date: str
    expected_sessions: int | None = None
    valid_sessions: int | None = None
    missing_dates: list[str] = Field(default_factory=list)
    first_valid_date: str | None = None
    last_valid_date: str | None = None
    complete: bool


class AnalysisItem(BaseModel):
    symbol: str
    security_id: str
    result_id: str
    input_version: str
    created_at: datetime | None = None
    data_published_at: datetime | None = None
    is_current: bool = False
    coverage_basis: str = "unknown"
    result_cutoff: str | None = None
    carried_from: str | None = None
    coverage: ResearchCoverage | None = None
    data: ResearchResult


class ResearchFreshness(BaseModel):
    origin_id: str
    latest_id: str
    latest_completed_session: str
    research_cutoff: str | None = None
    data_verified_at: datetime | None = None
    refresh_checked_at: datetime | None = None
    note: str


class AnalysisRefreshOutput(BaseModel):
    """Command receipt shared by native and custom-event research kinds."""
    id: str
    batch_id: str
    status: str
    freshness: ResearchFreshness | None = None


class AnalysisVersionView(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    security_id: str
    created_at: str


class RecentAnalysisView(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    params: dict[str, Any]
    created_at: str
    status: str


class RecentAnalysesOutput(BaseModel):
    data: dict[str, Any]
    items: list[RecentAnalysisView]


class AnalysisOutput(BaseModel):
    id: str
    batch_id: str
    status: str
    params: dict[str, Any]
    results: list[AnalysisItem]
    batch: BatchView
    result_versions: list[AnalysisVersionView] = Field(default_factory=list)
    freshness: ResearchFreshness | None = None


class StrategyView(BaseModel):
    key: str
    enabled: bool
    version: int
    options: dict[str, Any]
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None


class StrategyInput(Strict):
    key: Literal["sec", "market", "earnings", "backup", "maintenance"]
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


class ImportInput(Strict):
    kind: Literal["market", "earnings"]
    ticker: str = Field(min_length=1, max_length=20)
    csv: str = Field(min_length=1, max_length=5_000_000)
    source_url: str = Field(min_length=1, max_length=2000)
    price_basis: Literal["SPLIT_ONLY", "UNVERIFIED"] = "UNVERIFIED"


class ImportOutput(BaseModel):
    id: str
    kind: str
    status: str
    valid_rows: int
    errors: list[str]
    preview: list[dict[str, Any]]
    differences: list[str]
    batch_id: str | None = None


class EventCorrection(Strict):
    is_primary: bool = True
    fiscal_year: int = Field(ge=1, le=9998)
    fiscal_quarter: int = Field(ge=1, le=4)
    announced_date: date
    announced_at: datetime | None = None
    time_precision: Literal[
        "exact", "before_open", "after_close", "date_only", "intraday", "conflict"
    ]
    source_url: str = Field(min_length=1, max_length=2000)
    note: str = Field(min_length=1, max_length=2000)
    period_kind: Literal["regular", "transition", "unknown"] | None = None
    period_kind_source_url: str | None = Field(default=None, max_length=2000)
    period_kind_evidence: str | None = Field(default=None, max_length=2000)
    time_evidence: str | None = Field(default=None, max_length=2000)
    revision: int = Field(ge=1)

    @model_validator(mode="after")
    def period_kind_source(self):
        if self.time_precision in {"exact", "before_open", "after_close"} and not (self.time_evidence or "").strip():
            raise ValueError("精确时刻或盘前/盘后须填写来源中支持实际首次公开时间的原文或明确位置；电话会时间和官方排期不能替代")
        if self.period_kind in {"regular", "transition"} and (
            not (self.period_kind_source_url or "").strip()
            or not (self.period_kind_evidence or "").strip()
        ):
            raise ValueError("常规/过渡财期须分别填写财期类型来源链接及原文或明确位置；财年财季本身不证明财期类型")
        return self


class GenericOutput(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class TransactionSecurityInput(Strict):
    security_id: str = Field(min_length=1, max_length=36)
    evidence: str = Field(min_length=5, max_length=2000)


class RequestIdentity(Strict):
    request_id: str = Field(min_length=1, max_length=128)


class FeedOutput(BaseModel):
    groups: list[dict[str, Any]]
    order: str = "transaction"
    session_id: str
    as_of: str
    next_cursor: str | None
    total_groups: int
    data_status: str
    coverage: dict[str, Any]
    new_count: int = 0
    pending_filings: list[dict[str, Any]] = Field(default_factory=list)
    pending_summary: dict[str, Any] = Field(default_factory=dict)


class FeedGroupOutput(BaseModel):
    items: list[dict[str, Any]]
    revision_id: str
    total: int
    next_cursor: str | None


class HomeOutput(BaseModel):
    data_status: str
    market: list[dict[str, Any]]
    worker: dict[str, Any]
    active_jobs: int
    mode: str
    notice: str


class SystemOutput(BaseModel):
    version: str
    mode: str
    worker: dict[str, Any]
    migration: str
    automatic_collection_scope: str
    storage: dict[str, Any]


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
