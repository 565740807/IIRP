"""Response models of the Insider reads: feed, entity history and transaction detail.

Field values that are user-facing text are message codes (see ``iirp.messages``).
Transaction rows carry the parsed SEC facts; parser versions add optional facts
over time, so rows allow extra fields beyond the ones declared here.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Output(BaseModel):
    """Response model: fields with defaults are always present in the JSON."""
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class OpenOutput(BaseModel):
    """Response model that also passes through extra facts."""
    model_config = ConfigDict(extra="allow", json_schema_serialization_defaults_required=True)


class InsiderOwner(Output):
    """A reporting owner with the roles stated in that filing."""
    id: str
    name: str
    roles: list[str] = Field(default_factory=list)
    entity_type: str = "unknown"


class DateAnomaly(Output):
    """Transaction dated after its own SEC acceptance date (D19); kept as reported."""
    code: str
    label: str
    transaction_date: str
    accepted_date: str


class InsiderTransaction(OpenOutput):
    """One row of Table I or II of a Form 3/4/5 filing."""
    id: str
    issuer_id: str
    accession: str
    version_id: str | None = None
    form: str | None = None
    table: str | None = None
    code: str | None = None
    kind: str | None = None
    action_category: str | None = None
    direction: str | None = None
    security_title: str | None = None
    ticker: str | None = None
    issuer_ticker_raw: str | None = None
    issuer_name: str | None = None
    shares: str | None = None
    quantity_unit: str | None = None
    price: str | None = None
    price_per_share: str | None = None
    amount: str | None = None
    known_amount: str | None = None
    currency: str | None = None
    transaction_date: str | None = None
    accepted_at: str | None = None
    group_accepted_at: str | None = None
    group_accession: str | None = None
    owner: str | None = None
    owner_id: str | None = None
    owner_ids: list[str] = Field(default_factory=list)
    owners: list[InsiderOwner] = Field(default_factory=list)
    status: str | None = None
    eligible_for_totals: bool = False
    summary_exclusion_reason: str | None = None
    replaces_id: str | None = None
    is_amendment_update: bool = False
    date_anomaly: DateAnomaly | None = None
    source_row_index: int | None = None
    warnings: list[Any] = Field(default_factory=list)
    direct_or_indirect: str | None = None
    nature_of_ownership: str | None = None
    shares_after: str | None = None
    shares_before: str | None = None
    holding_change: str | None = None
    raw_10b5_1_flag: str | None = None


class TradeSummary(Output):
    """Rows of one action (table, security, currency, code) added up."""
    table: str | None = None
    security_title: str | None = None
    currency: str | None = None
    code: str | None = None
    kind: str | None = None
    shares: str | None = None
    known_amount: str | None = None
    rows: int
    missing_price_rows: int = 0
    review_rows: int = 0
    known_price_rows: int = 0
    known_shares_rows: int = 0
    reported_value_review_rows: int = 0
    missing_source_value_rows: int = 0
    owner_count: int = 0
    person_count: int = 0
    institution_count: int = 0
    unknown_owner_count: int = 0


class TraderGroup(Output):
    """The same owners, transaction date and action inside one company/date group."""
    id: str
    owners: list[InsiderOwner] = Field(default_factory=list)
    transaction_date: str | None = None
    summary: list[TradeSummary] = Field(default_factory=list)
    rows: int


class FeedGroup(Output):
    """One company and SEC acceptance date (US Eastern), as of one revision."""
    id: str
    issuer_id: str
    company: str | None = None
    ticker: str | None = None
    issuer_ticker_raw: str | None = None
    accepted_at: str | None = None
    accepted_date: str | None = None
    transaction_dates: list[str] = Field(default_factory=list)
    owners: int = 0
    filings: int = 0
    total_transactions: int = 0
    matching_transactions: int = 0
    amendment_count: int = 0
    amendment_updates: int = 0
    revision_id: str
    revision_created_at: str
    summary: list[TradeSummary] = Field(default_factory=list)
    transactions: list[InsiderTransaction] = Field(default_factory=list)
    trader_groups: list[TraderGroup] = Field(default_factory=list)
    next_trader_cursor: str | None = None
    next_cursor: str | None = None


class PendingStage(Output):
    id: str
    label: str
    count: int


class PendingSummary(Output):
    """Filings seen at SEC but not yet parsed, by stage."""
    total: int = 0
    scope: str | None = None
    stages: list[PendingStage] = Field(default_factory=list)
    preview_limit: int = 5
    preview_count: int = 0


class PendingFiling(Output):
    accession: str
    company: str | None = None
    form: str | None = None
    accepted_at: str | None = None
    filing_date: str | None = None
    stage: str
    status: str


class FeedCoverage(Output):
    status: str
    pending_filings: int
    missing_acceptance_rows: int
    message: str


class FeedOutput(Output):
    groups: list[FeedGroup]
    order: str = "transaction"
    session_id: str
    as_of: str
    next_cursor: str | None
    total_groups: int
    data_status: str
    coverage: FeedCoverage
    new_count: int = 0
    pending_filings: list[PendingFiling] = Field(default_factory=list)
    pending_summary: PendingSummary = Field(default_factory=PendingSummary)


class FeedGroupOutput(Output):
    """Either trader groups (cursor "g:…") or transaction rows of one group."""
    items: list[TraderGroup | InsiderTransaction]
    revision_id: str
    total: int
    next_cursor: str | None


class FeedUpdatesOutput(Output):
    session_id: str
    target_session_id: str
    version: str
    new_count: int
    changed_ids: list[str] = Field(default_factory=list)
    removed_ids: list[str] = Field(default_factory=list)
    groups: list[FeedGroup] = Field(default_factory=list)
    next_cursor: str | None = None
    as_of: str
    pending_summary: PendingSummary = Field(default_factory=PendingSummary)
    pending_filings: list[PendingFiling] = Field(default_factory=list)


class EntityRef(Output):
    id: str
    kind: str
    name: str
    ticker: str | None = None
    issuer_ticker_raw: str | None = None


class EntityCompany(Output):
    issuer_id: str
    name: str | None = None
    ticker: str | None = None


class EntityCoverage(Output):
    status: str
    start_date: str | None = None
    end_date: str | None = None
    requested_count: int | None = None
    observed_count: int = 0
    complete: bool = False
    message: str | None = None


class SideTotal(Output):
    """Open-market purchases (buy) or sales (sell) in the range: owners, shares, known amount."""
    side: str
    rows: int
    owners: int = 0
    shares: str | None = None
    known_amount: str | None = None
    missing_price_rows: int = 0
    currencies: int = 0
    currency: str | None = None


class EntityHistory(Output):
    id: str
    kind: str
    name: str | None = None
    entity: EntityRef
    items: list[InsiderTransaction] = Field(default_factory=list)
    status: str | None = None
    message: str | None = None
    session_id: str | None = None
    as_of: str | None = None
    total: int = 0
    next_cursor: str | None = None
    summary: list[TradeSummary] = Field(default_factory=list)
    headline: list[SideTotal] = Field(default_factory=list)
    summary_owner_scope: str | None = None
    summary_note: str | None = None
    transaction_start: str | None = None
    transaction_end: str | None = None
    filings: int | None = None
    date_basis: str = "transaction"
    companies: list[EntityCompany] = Field(default_factory=list)
    coverage: EntityCoverage


class EntityHistoryOutput(Output):
    """A company's or person's transactions in a stable reading session."""
    items: list[InsiderTransaction]
    data: EntityHistory


class PricePoint(OpenOutput):
    x: int | None = None
    date: str | None = None
    close: str | float | None = None
    value: str | float | None = None
    status: str | None = None


class PriceWindow(OpenOutput):
    """Closes from t−n to t+n around one time basis (transaction or disclosure)."""
    baseline_date: str | None = None
    points: list[PricePoint] = Field(default_factory=list)


class PriceContext(OpenOutput):
    """Price change around a transaction; status explains when it is unavailable."""
    status: str | None = None
    reason: str | None = None
    disclosure_lag_calendar_days: int | None = None
    price_fetched_at: str | None = None
    price_basis: str | None = None
    cutoff_date: str | None = None
    transaction: PriceWindow | None = None
    disclosure: PriceWindow | None = None
    next_open: dict[str, Any] | None = None


class TransactionDetail(Output):
    transaction: InsiderTransaction
    price_context: PriceContext


class TransactionDetailOutput(Output):
    items: list[dict[str, Any]] = Field(default_factory=list)
    data: TransactionDetail
