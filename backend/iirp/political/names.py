"""Resolve disclosure names against local issuer and security names only."""
import re
from collections import defaultdict
from difflib import SequenceMatcher

from sqlalchemy import select

from iirp.models import IndexConstituent, Issuer, Security
from iirp.political.config import policy

_SUFFIXES = r"\b(?:INCORPORATED|INC|CORPORATION|CORP|COMPANY|CO|LIMITED|LTD|PLC|COM|CMN)\b"


def normalize_name(value: str) -> str:
    value = value.upper()
    value = re.sub(r"\[[A-Z]{1,3}\]", " ", value)
    value = re.sub(r"\b(?:USD|US\$|PAR VALUE)\s*\d+(?:\.\d+)?", " ", value)
    value = re.sub(r"\b(?:COMMON\s+(?:STOCK|SHARES)|CLASS\s+[AB])\b", " ", value)
    # Correct digits only inside words that also contain letters. Dates, coupon
    # rates, and standalone numbers are never converted into name characters.
    value = re.sub(r"\b[A-Z0-9]+\b", lambda m: m[0].replace("5", "S").replace("0", "O")
                   if re.search(r"[A-Z]", m[0]) and re.search(r"[0-9]", m[0]) else m[0], value)
    value = re.sub(_SUFFIXES, " ", value)
    return " ".join(re.sub(r"[^A-Z0-9 ]", " ", value).split())


class NameMatcher:
    def __init__(self, candidates, *, threshold=None):
        self.threshold = policy()["matching"]["fuzzy_threshold"] if threshold is None else threshold
        self.names = defaultdict(set)
        for name, ticker in candidates:
            key = normalize_name(name or "")
            # A multi-class SEC ticker must not silently pick a share class.
            if key and ticker and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", ticker.upper()):
                self.names[key].add(ticker.upper().replace(".", "-"))
        self.cache = {}

    def match(self, name):
        key = normalize_name(name)
        if key not in self.cache:
            self.cache[key] = self._match(key)
        return self.cache[key]

    def _match(self, key):
        if not key:
            return None, None
        exact = self.names.get(key)
        if exact:
            return (next(iter(exact)), "local_exact") if len(exact) == 1 else (None, None)
        # Every name above the threshold must identify the same security. A
        # best-score winner alone is insufficient when another issuer is nearby.
        matches = set()
        if len(key) >= 6:
            for name, tickers in self.names.items():
                if 2 * min(len(key), len(name)) / (len(key) + len(name)) <= self.threshold:
                    continue
                if SequenceMatcher(None, key, name, autojunk=False).ratio() > self.threshold:
                    matches.update(tickers)
        return (next(iter(matches)), "inferred") if len(matches) == 1 else (None, None)

    def apply(self, row):
        kind = row.get("asset_type")
        if kind in {"fund", "option"} or row.get("ticker"):
            return
        name = row["asset_name"]
        if kind == "bond":
            # Remove only the explicit debt description, keeping the issuer.
            issuer = re.split(r"\d+(?:\.\d+)?\s*%|\b(?:NOTE|NOTES|BOND|BONDS|DEBENTURE|DUE)\b",
                              name, maxsplit=1, flags=re.I)[0]
            ticker, origin = self.match(issuer)
            row["issuer_ticker"], row["issuer_ticker_origin"] = ticker, origin
            return
        if kind not in {"stock", "unknown"}:
            return
        ticker, origin = self.match(name)
        if ticker:
            row["ticker"], row["ticker_origin"] = ticker, origin
            row["asset_type"] = "stock"
            row["warnings"] = [w for w in row.get("warnings", []) if w != "unknown_asset_type"]
            row["confidence"] = "review" if row["warnings"] else "parsed"


def local_matcher(s):
    candidates = list(s.execute(select(IndexConstituent.name, IndexConstituent.ticker)))
    candidates += list(s.execute(select(Issuer.name, Issuer.ticker)))
    candidates += list(s.execute(select(Security.name, Security.symbol).where(
        Security.instrument.not_in(("ETF", "MUTUALFUND", "BOND", "FUND")))))
    return NameMatcher(candidates)
