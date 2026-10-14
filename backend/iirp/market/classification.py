"""Index membership marks and the stock → sector classification.

Membership: a company belongs to an index when its CIK is in that index's saved list,
or its ticker is (``.`` and ``-`` treated alike, as in the Insider overview filter).
The lists are unofficial weekly copies (``iirp.market.constituents``).

Classification, in order:

1. In the S&P 500 list: the list's GICS sector, and its GICS sub-industry mapped to
   one of the 25 industry groups (config/gics.toml). Source ``sp500_gics``.
2. Otherwise Yahoo's sector, saved by the security identity check (the same get_info
   response, no extra request), mapped through ``[yahoo_sectors]`` in
   config/sector-map.toml. Yahoo's industries are not GICS, so there is no industry
   group. Source ``yahoo``.
3. Otherwise no sector. Source ``none``.

The Nasdaq-100 list's ICB classification is kept as given but not used here. A
security whose identity was checked before the sector was kept has no answer from
Yahoo yet; the read says so (``refresh``) and the page asks once for an identity
refresh. Nothing is backfilled in bulk.
"""

import tomllib
import uuid
from functools import lru_cache

from sqlalchemy import func, select

from iirp.config import ROOT
from iirp.messages import msg
from iirp.models import ACTIVE, IndexConstituent, Job, Security, Subscription

INDEXES = ("sp500", "nasdaq100")


def _ticker(value):
    return (value or "").strip().upper().replace(".", "-")


def check_gics(config: dict, groups_by_id: dict[str, str]) -> dict:
    """Every industry group of the sector map once, with its sector; no sub-industry twice."""
    problem = "config/gics.toml: "
    sectors = set(config["sectors"].values())
    if sectors != set(groups_by_id.values()) or len(config["sectors"]) != len(sectors):
        raise ValueError(problem + "sectors must name each sector of config/sector-map.toml once")
    if set(config["groups"]) != set(groups_by_id):
        raise ValueError(problem + "groups must be exactly the industry groups of config/sector-map.toml")
    seen = set()
    for group, item in config["groups"].items():
        if item["sector"] != groups_by_id[group]:
            raise ValueError(problem + f"{group} belongs to {groups_by_id[group]}")
        for name in item["sub_industries"]:
            if name in seen:
                raise ValueError(problem + f"sub-industry {name!r} appears twice")
            seen.add(name)
    for name, group in config.get("renamed", {}).items():
        if name in seen or group not in groups_by_id:
            raise ValueError(problem + f"renamed {name!r} must be an old name of a known group")
    return config


@lru_cache
def gics() -> dict:
    """config/gics.toml, checked against the sector map once."""
    from iirp.analysis.sectors import industry_groups

    with (ROOT / "config/gics.toml").open("rb") as source:
        config = tomllib.load(source)
    return check_gics(config, {group["id"]: group["sector"] for group in industry_groups()})


@lru_cache
def sub_industry_groups() -> dict[str, str]:
    """GICS sub-industry name → industry group id (current names, then pre-2023 names)."""
    config = gics()
    result = {name: group for group, item in config["groups"].items() for name in item["sub_industries"]}
    return {**config.get("renamed", {}), **result}


class Memberships:
    """Index lists loaded once per request; ``of`` answers for one company."""

    def __init__(self, s):
        self.by_cik: dict[str, set[str]] = {}
        self.by_ticker: dict[str, set[str]] = {}
        for index, ticker, cik in s.execute(
                select(IndexConstituent.index_name, IndexConstituent.ticker, IndexConstituent.cik)):
            self.by_ticker.setdefault(_ticker(ticker), set()).add(index)
            if cik:
                self.by_cik.setdefault(cik, set()).add(index)

    def of(self, cik=None, ticker=None, tickers=()) -> list[str]:
        found = set(self.by_cik.get(cik, ())) if cik else set()
        for value in (ticker, *tickers):
            if value:
                found |= self.by_ticker.get(_ticker(value), set())
        return [index for index in INDEXES if index in found]

    def mark(self, rows, *, cik="issuer_id", ticker="ticker"):
        """Add ``indexes`` to each row dict from its CIK and ticker fields."""
        for row in rows:
            row["indexes"] = self.of(row.get(cik), row.get(ticker))
        return rows


def _member(s, symbol):
    return s.scalar(select(IndexConstituent).where(
        IndexConstituent.index_name == "sp500",
        func.replace(IndexConstituent.ticker, ".", "-") == _ticker(symbol)).limit(1))


def _security(s, symbol):
    return s.scalar(select(Security).where(Security.symbol == _ticker(symbol)).order_by(Security.id).limit(1))


def _refresh_job(s, security):
    if security is None:
        return None
    return s.scalar(select(Job.id).where(Job.kind == "market_identity", Job.status.in_(ACTIVE),
                                         Job.target["security_id"].astext == security.id).limit(1))


def _sector(sector_id):
    from iirp.analysis.sectors import sector_map

    item = next((item for item in sector_map()["sector"] if item["id"] == sector_id), None)
    return {"id": sector_id, "etf": item["etf"]} if item else None


def _group(group_id):
    from iirp.analysis.sectors import industry_groups

    group = next((group for group in industry_groups() if group["id"] == group_id), None)
    if group is None:
        return None
    return {"id": group_id, "etf": group["primary"], "coverage": group["coverage"]}


def classify(s, symbol: str) -> dict:
    """The sector (with its ETF), the industry group (with its primary ETF, if any) and the source."""
    from iirp.analysis.sectors import sector_map
    from iirp.models import LongPriceSeries
    symbol = _ticker(symbol)
    removed = s.scalar(select(LongPriceSeries.removed_on).where(LongPriceSeries.symbol == symbol))
    result = {"symbol": symbol, "source": "none", "sector": None, "industry_group": None,
              "gics_sector": None, "gics_sub_industry": None, "yahoo_sector": None, "yahoo_industry": None,
              "indexes": Memberships(s).of(ticker=symbol), "removed_on": removed, "refresh": "none"}
    member = _member(s, symbol)
    if member is not None and member.industry in gics()["sectors"]:
        group = sub_industry_groups().get(member.sub_industry or "")
        result.update(source="sp500_gics", sector=_sector(gics()["sectors"][member.industry]),
                      industry_group=_group(group) if group else None,
                      gics_sector=member.industry, gics_sub_industry=member.sub_industry)
        return result
    security = _security(s, symbol)
    metadata = security.metadata_json if security else {}
    result.update(yahoo_sector=metadata.get("sector"), yahoo_industry=metadata.get("industry"))
    sector = sector_map()["yahoo_sectors"].get(metadata.get("sector") or "")
    if sector:
        result.update(source="yahoo", sector=_sector(sector))
        return result
    if _refresh_job(s, security):
        result["refresh"] = "pending"
    elif security is None or "sector" not in metadata:
        # Identity checked before the sector was kept (or never): ask once, when needed.
        result["refresh"] = "needed"
    return result


def request_refresh(s, symbol: str) -> dict:
    """Queue one identity refresh for this symbol if the classification is waiting for it."""
    current = classify(s, symbol)
    if current["refresh"] != "needed":
        return current
    from iirp.market.stock_quotes import symbols_for
    from iirp.market.yahoo import digest

    symbol = symbols_for([symbol])[0]
    security = _security(s, symbol)
    if security is None:
        security = Security(symbol=symbol)
        s.add(security)
        s.flush()
    target = {"symbol": security.symbol, "security_id": security.id}
    job = Job(kind="market_identity", title=msg("job.title.default", kind="market_identity", symbol=security.symbol),
              target=target, idempotency_key=digest(["market_identity", target, str(uuid.uuid4())]), priority=-10)
    s.add(job)
    s.flush()
    s.add(Subscription(job_id=job.id, source="manual"))
    s.flush()
    return classify(s, symbol)


class SavedSectors:
    """The saved classification of many companies at once, in ``classify``'s order.

    The S&P 500 list's GICS sector and industry group (the company matched by CIK,
    else by ticker), else Yahoo's sector saved by the identity check (no industry
    group), else none. Nothing is refreshed or requested: a company without a saved
    sector stays unclassified.
    """

    def __init__(self, s, symbols=()):
        from iirp.analysis.sectors import sector_map

        sectors, groups = gics()["sectors"], sub_industry_groups()
        self.by_cik: dict[str, tuple] = {}
        self.by_ticker: dict[str, tuple] = {}
        for ticker, cik, industry, sub_industry in s.execute(
                select(IndexConstituent.ticker, IndexConstituent.cik, IndexConstituent.industry,
                       IndexConstituent.sub_industry).where(IndexConstituent.index_name == "sp500")):
            if industry not in sectors:
                continue
            value = (sectors[industry], groups.get(sub_industry or ""))
            self.by_ticker[_ticker(ticker)] = value
            if cik:
                self.by_cik[cik] = value
        yahoo = sector_map()["yahoo_sectors"]
        self.by_symbol: dict[str, tuple] = {}
        wanted = sorted({_ticker(symbol) for symbol in symbols if symbol})
        if wanted:
            # The first security of a symbol answers, as in ``classify``.
            for symbol, metadata in s.execute(select(Security.symbol, Security.metadata_json)
                                              .where(Security.symbol.in_(wanted)).order_by(Security.id)):
                sector = yahoo.get((metadata or {}).get("sector") or "")
                self.by_symbol.setdefault(symbol, (sector, None) if sector else (None, None))

    def of(self, cik=None, ticker=None) -> tuple[str | None, str | None]:
        """(sector id, industry group id); either may be None."""
        if cik and cik in self.by_cik:
            return self.by_cik[cik]
        key = _ticker(ticker)
        if key in self.by_ticker:
            return self.by_ticker[key]
        return self.by_symbol.get(key, (None, None))
