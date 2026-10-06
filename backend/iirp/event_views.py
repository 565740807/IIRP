"""Typed read envelopes; historical documents and financial payloads stay opaque.

Read models allow extra fields deliberately: introducing a DTO must not rewrite
an older frozen document or discard provenance added by another result version.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from iirp.contracts import ResearchFreshness


class EventReadView(BaseModel):
    model_config = ConfigDict(extra="allow")


class EventErrorOutput(EventReadView):
    detail: str | list[dict[str, Any]]


class EventSecurityView(EventReadView):
    id: str
    symbol: str
    name: str
    currency: str | None
    exchange: str | None
    status: str
    calendar: str | None


class EventSetSummary(EventReadView):
    id: str
    title: str
    kind: Literal["custom", "earnings"]
    version: int
    latest_version: int
    version_id: str
    security: EventSecurityView
    created_at: str
    updated_at: str
    research_as_of: str
    event_count: int
    selected_count: int
    verified_count: int
    revision_note: str


class EventSetsOutput(EventReadView):
    items: list[EventSetSummary]


class EventVersionView(EventReadView):
    version: int
    id: str
    created_at: str
    revision_note: str


class EventAnalysisReference(EventReadView):
    id: str
    batch_id: str
    params: dict[str, Any]
    created_at: str


class EventSetOutput(EventSetSummary):
    # These are saved historical objects, not today's import validation schema.
    document: dict[str, Any]
    events: list[dict[str, Any]]
    reviews: list[dict[str, Any]]
    raw_text: str
    content_hash: str
    warnings: list[str]
    versions: list[EventVersionView]
    analyses: list[EventAnalysisReference]


class EventAnalysisCreated(EventReadView):
    analysis_id: str
    batch_id: str
    reused: bool


class EventConfirmOutput(EventReadView):
    set_id: str
    version: int
    version_id: str
    reused: bool
    analysis: EventAnalysisCreated | None = None


class EventProgressView(EventReadView):
    symbol: str
    status: str
    wait_reason: str | None
    ready_events: int | None = None
    event_count: int | None = None
    required_ranges: list[dict[str, Any]] | None = None


class EventResultReference(EventReadView):
    id: str
    created_at: str
    dataset_id: str | None
    expires_at: str | None = None


class EventAnalysisOutput(EventReadView):
    id: str
    batch_id: str
    params: dict[str, Any]
    status: str
    requested_action: str | None
    created_at: str
    progress: list[EventProgressView]
    result_id: str | None
    result_cutoff: str | None
    expires_at: str | None = None
    data: dict[str, Any] | None
    results: list[EventResultReference]
    freshness: ResearchFreshness | None = None
