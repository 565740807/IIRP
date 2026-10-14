"""The followed people: the default list file, filling it once, adding, removing and restoring.

Uses a disposable iirp_v1_test_* database.
"""

import tomllib

import pytest
from iirp.config import ROOT
from iirp.db import session
from iirp.models import NotablePerson, Owner, Preferences
from iirp.notable import people
from sqlalchemy import func, select

from tests.jobs.test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_client,
    lifecycle_database,
)
from tests.notable.fixtures import politician


def count():
    with session() as s:
        return s.scalar(select(func.count()).select_from(NotablePerson))


def test_default_file_is_valid_and_complete():
    entries = people.read_defaults()
    by_source = {source: [item for item in entries if item["source"] == source] for source in people.SOURCES}
    # 35 SEC filers, 16 House members, the President and 15 cabinet-level officials.
    assert [len(by_source[source]) for source in people.SOURCES] == [35, 16, 16]
    assert len({item["key"] for item in entries}) == len(entries)
    assert "sec:0001767470" in {item["key"] for item in entries}
    assert {"house:Nancy Pelosi|CA11", "oge:Donald J Trump|president"} <= {item["key"] for item in entries}
    text = (ROOT / "config/notable-people.toml").read_text()
    assert "every six months; last reviewed 2026-10" in text
    assert people.main([str(ROOT / "config/notable-people.toml")]) == 0


def entry(**fields):
    return {"source": "sec", "cik": "1767470", "name": "Ryan Cohen", "category": "investor",
            "note": {"en": "A.", "zh": "甲。"}, **fields}


@pytest.mark.parametrize("person,problem", [
    (entry(cik="17674700000"), "cik must be 1 to 10 digits"),
    (entry(cik="17A"), "cik must be 1 to 10 digits"),
    (entry(cik=1767470), "cik must be 1 to 10 digits"),
    (entry(name=""), "name is missing"),
    ({**entry(source="house", district="CA11"), "cik": None, "name": "Nancy Pelosi"}, None),
    ({**entry(source="house"), "name": "Nancy Pelosi"}, "district is missing"),
    ({**entry(source="house", district="California 11"), "name": "Nancy Pelosi"}, "district must look like CA11"),
    ({**entry(source="oge"), "name": "Donald J Trump"}, "position is missing"),
    (entry(source="senate"), "source must be one of"),
    (entry(category="celebrity"), "category must be one of"),
    (entry(note={"en": "A."}), "note needs en and zh"),
])
def test_default_file_checks(person, problem):
    found = people.check_defaults([person])
    if problem is None:
        assert found == []
    else:
        assert any(problem in item for item in found), found


def test_default_file_rejects_duplicates_in_any_spelling(tmp_path):
    # Leading zeros do not make another filer.
    assert any("listed twice" in item for item in people.check_defaults([entry(), entry(cik="0001767470")]))
    path = tmp_path / "people.toml"
    path.write_text('[[person]]\nsource = "sec"\ncik = "1"\nname = "A"\ncategory = "investor"\n'
                    'note = { en = "A.", zh = "甲。" }\n' * 2)
    with pytest.raises(ValueError, match="listed twice"):
        people.read_defaults(path)
    assert people.main([str(path)]) == 1
    with open(ROOT / "config/notable-people.toml", "rb") as source:
        assert people.check_defaults(tomllib.load(source)["person"]) == []


def test_defaults_fill_an_empty_list_only_once():
    with session() as s, s.begin():
        assert people.seed(s) == 67
    assert count() == 67
    with session() as s, s.begin():
        s.query(NotablePerson).delete()
    # Emptied by the user: the database keeps the user's choice.
    with session() as s, s.begin():
        assert people.seed(s) == 0
    assert count() == 0
    with session() as s:
        assert s.get(Preferences, 1).values["notable"]["filled_at"]


def test_a_list_kept_from_before_is_never_refilled():
    with session() as s, s.begin():
        s.add(NotablePerson(key="sec:0000000001", source="sec", name="Someone"))
    with session() as s, s.begin():
        assert people.seed(s) == 0
    assert count() == 1


def test_add_remove_and_restore(lifecycle_client):  # noqa: F811
    client = lifecycle_client
    # Starting the app filled the list from the default file.
    listed = client.get("/api/v1/notable/people").json()
    assert len(listed["items"]) == 67 and listed["default_count"] == 67
    assert listed["items"][0]["key"] == "sec:0000315090"
    assert listed["items"][0]["note"]["zh"] and listed["items"][0]["watched"] is True
    removed = client.delete("/api/v1/notable/people", params={"key": "sec:0001767470"}).json()
    assert "sec:0001767470" not in [item["key"] for item in removed["items"]]
    with session() as s, s.begin():
        s.add(Owner(id="0000000042", name="DOE JANE"))
        politician(s, name="Jane Roe", district="TX01")
    added = client.post("/api/v1/notable/people", json={"key": "sec:0000000042"}).json()
    jane = next(item for item in added["items"] if item["key"] == "sec:0000000042")
    assert (jane["name"], jane["category"], jane["default"], jane["local"]) == ("DOE JANE", None, False, True)
    assert client.post("/api/v1/notable/people", json={"key": "house:Jane Roe|TX01"}).status_code == 200
    # A politician no disclosure index names cannot be added; a malformed key is refused.
    unknown = client.post("/api/v1/notable/people", json={"key": "house:John Doe|ZZ99"})
    assert unknown.status_code == 422 and unknown.json()["detail"]["code"] == "notable.person_unknown"
    assert client.post("/api/v1/notable/people", json={"key": "sec:12x45"}).json()["detail"]["code"] == "notable.key_invalid"
    # Restoring adds back the removed default and keeps the user's additions.
    restored = client.post("/api/v1/notable/people/restore").json()
    assert restored["added"] == 1
    keys = [item["key"] for item in restored["items"]]
    assert len(keys) == 69 and {"sec:0001767470", "sec:0000000042", "house:Jane Roe|TX01"} <= set(keys)
    assert client.post("/api/v1/notable/people/restore").json()["added"] == 0


def test_all_scope_lists_every_politician_and_the_default_filers(lifecycle_client):  # noqa: F811
    client = lifecycle_client
    with session() as s, s.begin():
        politician(s, name="Jane Roe", district="TX01")
        politician(s, name="Nancy Pelosi", district="CA11")
    client.delete("/api/v1/notable/people", params={"key": "sec:0001767470"})
    items = {item["key"]: item for item in client.get("/api/v1/notable/people", params={"scope": "all"}).json()["items"]}
    assert items["house:Jane Roe|TX01"]["watched"] is False and items["house:Jane Roe|TX01"]["category"] == "house"
    assert items["house:Nancy Pelosi|CA11"]["watched"] is True and items["house:Nancy Pelosi|CA11"]["local"] is True
    assert items["sec:0001767470"]["watched"] is False and items["sec:0001767470"]["default"] is True


def test_search_offers_politicians_and_filers_to_follow(lifecycle_client):  # noqa: F811
    with session() as s, s.begin():
        politician(s, name="Nancy Pelosi", district="CA11")
        politician(s, source="oge", name="Donald J Trump", district=None, position="president")
        s.add(Owner(id="0001767470", name="Cohen Ryan"))
    found = lifecycle_client.get("/api/v1/search", params={"q": "pelosi"}).json()["items"]
    assert [(item["kind"], item["notable_key"], item["href"]) for item in found] == [
        ("politician", "house:Nancy Pelosi|CA11", "/notable/house/Nancy%20Pelosi%7CCA11")]
    trump = lifecycle_client.get("/api/v1/search", params={"q": "trump"}).json()["items"]
    assert trump[0]["detail"] == "president"
    cohen = lifecycle_client.get("/api/v1/search", params={"q": "cohen"}).json()["items"]
    assert [(item["kind"], item["notable_key"]) for item in cohen] == [("person", "sec:0001767470")]
