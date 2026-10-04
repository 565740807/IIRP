"""Normal coverage targets and development sampling budgets never share a config key."""

import tomllib
from functools import lru_cache

from pydantic import BaseModel, Field

from iirp.config import ROOT


class DevelopmentBudget(BaseModel):
    sec_pages_per_probe: int = Field(ge=1, le=1)
    sec_items_per_page: int = Field(ge=1, le=40)
    market_tickers_per_probe: int = Field(ge=1, le=1)
    market_period: str
    provider_deadline_seconds: int = Field(ge=1, le=25)
    source_cooldown_seconds: int = Field(ge=1)
    failure_cooldown_seconds: int = Field(ge=1)
    scheduled_probe_interval_minutes: int = Field(ge=1)


@lru_cache
def profiles():
    with (ROOT / "config/collection-defaults.toml").open("rb") as source:
        return tomllib.load(source)


@lru_cache
def development_budget():
    return DevelopmentBudget(**profiles()["development_tests"])
