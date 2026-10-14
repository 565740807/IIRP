"""Other names of stocks (config/aliases.toml): Chinese names, former names, short names.

The table is edited by hand and read from disk; nothing is fetched or
generated. An alias names one or more codes in the index lists' form ("."
read as "-"). Searches compare without case: a term finds the aliases that
contain it, or only the alias equal to it when it is a single character.

``python -m iirp.aliases`` checks the table against the saved index lists.
"""

import re
import sys
import tomllib
from functools import cache
from pathlib import Path

from iirp.config import ROOT

FILE = ROOT / "config/aliases.toml"
CODE = re.compile(r"^[A-Z0-9]{1,6}(-[A-Z0-9]{1,2})?$")


class AliasError(ValueError):
    """The alias table is malformed; the message lists every problem."""


def fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def code_key(text: str) -> str:
    """Codes compare in upper case with "." read as "-" (BRK.B is BRK-B)."""
    return text.strip().upper().replace(".", "-")


def parse(table: dict) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Folded alias → (alias as written, codes); raises AliasError listing every problem."""
    problems, found = [], {}
    for alias, codes in table.items():
        name = fold(alias)
        if not name:
            problems.append(f"empty alias for {codes!r}")
            continue
        if name in found:
            problems.append(f"duplicate alias {alias!r} (also {found[name][0]!r})")
            continue
        if not isinstance(codes, list) or not codes or not all(isinstance(code, str) for code in codes):
            problems.append(f"{alias!r} must name a list of one or more codes")
            continue
        bad = [code for code in codes if not CODE.match(code)]
        if bad:
            problems.append(f"{alias!r} names malformed codes {bad} (upper case, '-' for a class: BRK-B)")
            continue
        if len(set(codes)) != len(codes):
            problems.append(f"{alias!r} repeats a code")
            continue
        found[name] = (alias, tuple(codes))
    if problems:
        raise AliasError("; ".join(problems))
    return found


def read(path: Path = FILE) -> dict[str, tuple[str, tuple[str, ...]]]:
    with path.open("rb") as source:
        document = tomllib.load(source)
    if set(document) != {"aliases"} or not isinstance(document["aliases"], dict):
        raise AliasError("the file holds one [aliases] table")
    return parse(document["aliases"])


@cache
def aliases() -> dict[str, tuple[str, tuple[str, ...]]]:
    return read()


def check(known, table=None) -> None:
    """Raise AliasError when an alias names a code outside ``known`` (the index lists' codes)."""
    table = aliases() if table is None else table
    codes = {code_key(code) for code in known}
    unknown = [f"{alias!r} → {code}" for alias, names in table.values() for code in names if code not in codes]
    if unknown:
        raise AliasError("codes not in the index lists: " + ", ".join(unknown))


def matches(term: str, table=None) -> list[tuple[str, tuple[str, ...]]]:
    """Aliases the term finds, as (alias as written, codes): exact ones first."""
    table = aliases() if table is None else table
    term = fold(term)
    if not term:
        return []
    exact = [item for name, item in table.items() if name == term]
    if len(term) < 2:
        return exact
    return exact + [item for name, item in table.items() if term in name and name != term]


def codes(term: str, table=None) -> list[str]:
    """Every code the term's aliases name, in the order found, without repeats."""
    return list(dict.fromkeys(code for _, names in matches(term, table) for code in names))


def main():
    """Check the table against the index lists saved in the database."""
    from sqlalchemy import select

    from iirp.db import session
    from iirp.models import IndexConstituent

    with session() as s:
        known = set(s.scalars(select(IndexConstituent.ticker)))
    try:
        if not known:
            raise AliasError("no index lists are saved yet")
        check(known)
    except AliasError as exc:
        print(f"config/aliases.toml: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"config/aliases.toml: {len(aliases())} aliases, every code is in the index lists")


if __name__ == "__main__":
    main()
