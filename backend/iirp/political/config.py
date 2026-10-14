"""One source of collection bounds; development samples do not change coverage."""
import tomllib
from functools import lru_cache

from iirp.config import ROOT


@lru_cache
def policy():
    with (ROOT / "config/political-disclosures.toml").open("rb") as stream:
        return tomllib.load(stream)
