import tomllib
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IIRP_", env_file=ROOT / ".env", extra="ignore")
    database_url: str = Field(repr=False)
    runtime_dir: Path = ROOT / "runtime"
    port: int = 18081
    mode: str = "development"
    sec_user_agent: str = Field(default="", repr=False)
    min_free_bytes: int = 10 * 1024**3


@lru_cache
def settings() -> Settings:
    return Settings()


@lru_cache
def refresh() -> dict:
    """Refresh cadence (D22) from config/refresh.toml."""
    with (ROOT / "config/refresh.toml").open("rb") as source:
        return tomllib.load(source)
