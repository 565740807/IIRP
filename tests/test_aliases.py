"""The alias table (config/aliases.toml) and the site search's alias hits."""

import pytest
from fastapi.testclient import TestClient
from iirp import aliases
from iirp.db import session
from iirp.models import IndexConstituent, Issuer, Security, now

from tests.jobs.test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401


def test_the_shipped_table_is_well_formed():
    table = aliases.read()
    assert len(table) >= 100
    assert table["google"] == ("Google", ("GOOGL", "GOOG"))
    assert table[aliases.fold("谷歌")][1] == ("GOOGL", "GOOG")
    assert table[aliases.fold("伯克希尔")][1] == ("BRK-B",)


@pytest.mark.parametrize(("table", "problem"), [
    ({"Google": ["GOOGL"], "google": ["GOOG"]}, "duplicate alias"),
    ({"  Google ": ["GOOGL"], "GOOGLE": ["GOOG"]}, "duplicate alias"),
    ({"谷歌": []}, "one or more codes"),
    ({"谷歌": "GOOGL"}, "one or more codes"),
    ({"谷歌": ["googl"]}, "malformed"),
    ({"伯克希尔": ["BRK.B"]}, "malformed"),
    ({"谷歌": ["GOOGL", "GOOGL"]}, "repeats"),
    ({" ": ["GOOGL"]}, "empty alias"),
])
def test_malformed_tables_are_rejected(table, problem):
    with pytest.raises(aliases.AliasError, match=problem):
        aliases.parse(table)


def test_codes_outside_the_index_lists_are_rejected(tmp_path):
    table = aliases.parse({"谷歌": ["GOOGL", "GOOG"], "伯克希尔": ["BRK-B"]})
    aliases.check({"GOOGL", "GOOG", "BRK.B"}, table)
    with pytest.raises(aliases.AliasError, match="GOOG"):
        aliases.check({"GOOGL", "BRK.B"}, table)
    bad = tmp_path / "aliases.toml"
    bad.write_text('[aliases]\n"x" = ["A"]\n[other]\n')
    with pytest.raises(aliases.AliasError, match="one \\[aliases\\] table"):
        aliases.read(bad)


def test_matching_ignores_case_and_finds_parts():
    table = aliases.parse({"谷歌": ["GOOGL", "GOOG"], "Google": ["GOOGL", "GOOG"], "John Deere": ["DE"],
                           "迪尔": ["DE"]})
    assert aliases.codes("GOOGLE", table) == ["GOOGL", "GOOG"]
    assert aliases.codes("goo", table) == ["GOOGL", "GOOG"]
    assert aliases.codes("john  deere", table) == ["DE"]
    # One character finds only an alias equal to it.
    assert aliases.codes("谷", table) == [] and aliases.codes("g", table) == []
    assert aliases.matches("迪尔", table) == [("迪尔", ("DE",))]
    assert aliases.codes("", table) == []


def test_the_site_search_finds_a_company_by_its_alias(monkeypatch):
    from iirp.api.app import app

    with session() as s, s.begin():
        s.add(Issuer(id="0001652044", name="Alphabet Inc.", ticker="GOOGL"))
        s.add_all([Security(symbol="GOOGL", name="Alphabet Inc.", issuer_id="0001652044", instrument="EQUITY",
                            calendar="XNYS", status="VERIFIED"),
                   Security(symbol="GOOG", name="Alphabet Inc.", instrument="EQUITY", calendar="XNYS",
                            status="VERIFIED"),
                   Security(symbol="BRK-B", name="Berkshire Hathaway Inc.", instrument="EQUITY", calendar="XNYS",
                            status="VERIFIED")])
        s.add_all([IndexConstituent(index_name="sp500", ticker=ticker, cik=cik, name=name, classification="GICS",
                                    updated_at=now())
                   for ticker, cik, name in (("GOOGL", "0001652044", "Alphabet Inc. (Class A)"),
                                             ("GOOG", "0001652044", "Alphabet Inc. (Class C)"),
                                             ("BRK.B", "0001067983", "Berkshire Hathaway"))])
        # The other list still names the former company for one share class.
        s.add(Issuer(id="0001288776", name="GOOGLE INC.", ticker="GOOG"))
        s.add(IndexConstituent(index_name="nasdaq100", ticker="GOOG", cik="0001288776", name="Alphabet Inc. (Class C)",
                               classification="ICB", updated_at=now()))
    table = aliases.parse({"谷歌": ["GOOGL", "GOOG"], "伯克希尔": ["BRK-B"]})
    monkeypatch.setattr(aliases, "aliases", lambda: table)
    with TestClient(app, headers={"X-IIRP-Client": "web"}) as client:
        items = client.get("/api/v1/search", params={"q": "谷歌"}).json()["items"]
        # Both share classes belong to one company: one item, the company page.
        assert items == [{"id": "0001652044", "kind": "company", "name": "Alphabet Inc.", "href": "/companies/0001652044",
                          "ticker": "GOOGL, GOOG", "security_id": items[0]["security_id"], "issuer_id": "0001652044",
                          "alias": "谷歌", "notable_key": None, "detail": None}]
        # No issuer saved for this code: its monthly analysis.
        berkshire = client.get("/api/v1/search", params={"q": "伯克希尔"}).json()["items"]
        assert [(item["kind"], item["ticker"], item["href"]) for item in berkshire] == [
            ("security", "BRK-B", "/analysis/monthly?tickers=BRK-B")]
        # A plain name search is unchanged and does not repeat what an alias found.
        plain = client.get("/api/v1/search", params={"q": "alphabet"}).json()["items"]
        assert all(item.get("alias") is None for item in plain) and plain
        assert client.get("/api/v1/search", params={"q": "zz-no-such"}).json()["items"] == []
