"""The followed people: stable keys, the default list file and the list itself.

A person is kept by a key that survives a new database, never by a generated
row id:

- ``sec:<CIK>`` — an SEC Form 4 filer, the CIK written with ten digits;
- ``house:<name>|<district>`` — a House member, as the House index writes both;
- ``oge:<name>|<position>`` — an OGE 278-T filer, as the OGE index writes both.

``config/notable-people.toml`` fills the list once, when it is empty and has
never been filled; after that the list in the database belongs to the user, and
restoring the defaults only adds back the default people who were removed. The
list and the "filled" mark live in the database, so both are part of every
backup. Reading, adding and removing never ask a source for anything.
"""

import re
import sys
import tomllib
from functools import lru_cache

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert

from iirp.config import ROOT
from iirp.messages import UserError
from iirp.models import NotablePerson, Owner, Preferences, now
from iirp.models.political import PoliticalPerson

SOURCES = ("sec", "house", "oge")
CATEGORIES = ("investor", "investment_firm", "founder_ceo", "house", "president", "cabinet")
DEFAULTS_FILE = ROOT / "config/notable-people.toml"
PREFERENCES_KEY = "notable"
LIMIT = 500
CIK = re.compile(r"\d{1,10}")
DISTRICT = re.compile(r"[A-Z]{2}\d{2}")


def cik_key(value):
    """The ten-digit form of a CIK, or None when it is not one."""
    value = str(value or "").strip()
    return value.zfill(10) if CIK.fullmatch(value) and int(value) > 0 else None


def person_key(source, *, cik=None, name=None, district=None, position=None):
    if source == "sec":
        return f"sec:{cik_key(cik)}"
    return f"{source}:{name}|{district if source == 'house' else position}"


def parse_key(key):
    """The parts of a key: {source, cik} or {source, name, district|position}; a malformed key is refused."""
    source, _, rest = (key or "").partition(":")
    if source == "sec" and cik_key(rest) == rest:
        return {"source": "sec", "cik": rest}
    if source in ("house", "oge") and rest.count("|") == 1:
        name, detail = rest.split("|")
        if name.strip() and detail.strip() and len(key) <= 300:
            return {"source": source, "name": name, ("district" if source == "house" else "position"): detail}
    raise UserError("notable.key_invalid")


def check_defaults(people):
    """Problems in the default list (each a short English line); empty when it is valid."""
    problems, seen = [], set()
    for index, person in enumerate(people, 1):
        where = f"person {index}"
        source = person.get("source")
        if source not in SOURCES:
            problems.append(f"{where}: source must be one of {', '.join(SOURCES)}")
            continue
        if source == "sec":
            # Written as a quoted string, so leading zeros and the digit count stay as filed.
            if not isinstance(person.get("cik"), str) or not cik_key(person["cik"]) or not CIK.fullmatch(person["cik"]):
                problems.append(f"{where}: cik must be 1 to 10 digits")
                continue
            if not str(person.get("name", "")).strip():
                problems.append(f"{where}: name is missing")
        else:
            detail = "district" if source == "house" else "position"
            if not str(person.get("name", "")).strip():
                problems.append(f"{where}: name is missing")
                continue
            if not str(person.get(detail, "")).strip():
                problems.append(f"{where}: {detail} is missing")
                continue
            if source == "house" and not DISTRICT.fullmatch(person[detail]):
                problems.append(f"{where}: district must look like CA11")
            if "|" in person["name"] or "|" in person[detail]:
                problems.append(f"{where}: name and {detail} cannot contain |")
                continue
        if person.get("category") not in CATEGORIES:
            problems.append(f"{where}: category must be one of {', '.join(CATEGORIES)}")
        note = person.get("note")
        if not isinstance(note, dict) or not all(str(note.get(language, "")).strip() for language in ("en", "zh")):
            problems.append(f"{where}: note needs en and zh")
        key = person_key(source, cik=person.get("cik"), name=person.get("name"),
                         district=person.get("district"), position=person.get("position"))
        if key in seen:
            problems.append(f"{where}: {key} is listed twice")
        seen.add(key)
    return problems


def read_defaults(path=DEFAULTS_FILE):
    with open(path, "rb") as source:
        people = tomllib.load(source).get("person", [])
    problems = check_defaults(people)
    if problems:
        raise ValueError("; ".join(problems))
    return [{
        "key": person_key(person["source"], cik=person.get("cik"), name=person.get("name"),
                          district=person.get("district"), position=person.get("position")),
        "source": person["source"],
        "name": person.get("display") or person["name"],
        "category": person["category"],
        "note": {"en": person["note"]["en"], "zh": person["note"]["zh"]},
    } for person in people]


@lru_cache
def defaults():
    """The default people in file order, each with its key, display name, category and note."""
    return read_defaults()


def default_map():
    return {person["key"]: person for person in defaults()}


def _marks(s):
    row = s.get(Preferences, 1)
    return ((row.values or {}).get(PREFERENCES_KEY) or {}) if row else {}


def _lock(s):
    s.execute(text("SELECT pg_advisory_xact_lock(hashtext('iirp.notable'))"))


def seed(s):
    """Fill an empty, never-filled list from the default file once; returns how many were added."""
    from iirp.jobs.batches import defaults as preference_defaults

    _lock(s)
    if _marks(s).get("filled_at"):
        return 0
    added = 0
    if not s.scalar(select(func.count()).select_from(NotablePerson)):
        added = _insert(s, defaults())
    preference_defaults(s)
    row = s.get(Preferences, 1, with_for_update=True)
    row.values = {**row.values, PREFERENCES_KEY: {**_marks(s), "filled_at": now().isoformat()}}
    row.version += 1
    return added


def seed_defaults():
    """Fill the list at startup when it was never filled; database errors are left to readiness checks."""
    from sqlalchemy.exc import SQLAlchemyError

    from iirp.db import session

    try:
        with session() as s, s.begin():
            return seed(s)
    except SQLAlchemyError:
        return 0


def _insert(s, people, at=None):
    at = at or now()
    present = set(s.scalars(select(NotablePerson.key)))
    room = LIMIT - len(present)
    rows = [{"key": person["key"], "source": person["source"], "name": person["name"], "added_at": at}
            for person in people if person["key"] not in present][:max(room, 0)]
    if rows:
        s.execute(insert(NotablePerson).values(rows).on_conflict_do_nothing(index_elements=[NotablePerson.key]))
    return len(rows)


def political_people(s, keys=None):
    """Key → local political person rows; one key can match a person listed under several ids."""
    result = {}
    for person in s.scalars(select(PoliticalPerson).where(PoliticalPerson.source.in_(("house", "oge")))):
        key = person_key(person.source, name=person.name, district=person.district, position=person.position)
        if keys is None or key in keys:
            result.setdefault(key, []).append(person)
    return result


def _local_name(s, key):
    parts = parse_key(key)
    if parts["source"] == "sec":
        owner = s.get(Owner, parts["cik"])
        return owner.name if owner else None
    people = political_people(s, {key}).get(key)
    return people[0].name if people else None


def add(s, key, name=None):
    parts = parse_key(key)
    known = default_map().get(key)
    local = _local_name(s, key)
    if parts["source"] != "sec" and not known and not local:
        # A politician is added only as the disclosure index names them.
        raise UserError("notable.person_unknown")
    display = (known or {}).get("name") or local or (name or "").strip()[:200] or f"CIK {int(parts['cik'])}"
    _lock(s)
    if s.get(NotablePerson, key) is None:
        if s.scalar(select(func.count()).select_from(NotablePerson)) >= LIMIT:
            raise UserError("notable.full", limit=LIMIT)
        s.add(NotablePerson(key=key, source=parts["source"], name=display, added_at=now()))
        s.flush()
    return key


def remove(s, key):
    s.execute(delete(NotablePerson).where(NotablePerson.key == key))


def restore(s):
    """Add back the default people who were removed; the user's own additions stay."""
    _lock(s)
    return _insert(s, defaults())


def keys(s):
    return set(s.scalars(select(NotablePerson.key)))


def since(s):
    """Key → when the person was added (notifications for them start there)."""
    return dict(s.execute(select(NotablePerson.key, NotablePerson.added_at)).all())


def category(source, position=None):
    if source == "house":
        return "house"
    if source == "oge":
        return "president" if (position or "").strip().lower() == "president" else "cabinet"
    return None


def entry(key, *, name=None, added_at=None, local=None, political=None):
    """The view of one person: display name, category and note from the default list when listed."""
    known = default_map().get(key)
    parts = parse_key(key)
    people = political or []
    return {
        "key": key, "source": parts["source"],
        "name": (known or {}).get("name") or name or (people[0].name if people else None) or key,
        "category": (known or {}).get("category") or category(parts["source"], parts.get("position")),
        "note": (known or {}).get("note"),
        "cik": parts.get("cik"), "district": parts.get("district"), "position": parts.get("position"),
        "default": known is not None,
        "local": bool(local or people),
        "watched": added_at is not None,
        "added_at": added_at,
    }


def watched(s):
    """The followed people, in the default list's order, then the user's additions by name."""
    rows = list(s.scalars(select(NotablePerson)))
    political = political_people(s, {row.key for row in rows if row.source != "sec"})
    ciks = [parse_key(row.key)["cik"] for row in rows if row.source == "sec"]
    owners = set(s.scalars(select(Owner.id).where(Owner.id.in_(ciks)))) if ciks else set()
    order = {key: index for index, key in enumerate(default_map())}
    items = [entry(row.key, name=row.name, added_at=row.added_at,
                   local=row.source == "sec" and parse_key(row.key)["cik"] in owners,
                   political=political.get(row.key)) for row in rows]
    return sorted(items, key=lambda item: (order.get(item["key"], len(order)), item["name"].lower()))


def directory(s):
    """Everyone the "all" view covers: every House and OGE filer kept locally, the default
    SEC filers and any followed SEC filer; followed people are marked."""
    followed = {item["key"]: item for item in watched(s)}
    political = political_people(s)
    keys_ = list(followed)
    keys_ += [key for key in default_map() if key not in followed]
    keys_ += [key for key in political if key not in followed and key not in default_map()]
    ciks = [parse_key(key)["cik"] for key in keys_ if key.startswith("sec:")]
    owners = set(s.scalars(select(Owner.id).where(Owner.id.in_(ciks)))) if ciks else set()
    items = []
    for key in keys_:
        if key in followed:
            items.append(followed[key])
            continue
        parts = parse_key(key)
        items.append(entry(key, local=parts["source"] == "sec" and parts["cik"] in owners,
                           political=political.get(key)))
    return items


def scope_people(s, scope):
    """(SEC CIKs, political person ids) of the followed people, or of everyone the "all" view covers."""
    if scope == "all":
        listed = set(keys(s)) | set(default_map())
        ciks = {parse_key(key)["cik"] for key in listed if key.startswith("sec:")}
        ids = set(s.scalars(select(PoliticalPerson.id).where(PoliticalPerson.source.in_(("house", "oge")))))
        return ciks, ids
    return people_ids(s, keys(s))


def people_ids(s, wanted):
    ciks = {parse_key(key)["cik"] for key in wanted if key.startswith("sec:")}
    political = political_people(s, {key for key in wanted if not key.startswith("sec:")})
    return ciks, {person.id for people in political.values() for person in people}


def main(argv=None):
    """Check config/notable-people.toml; prints each problem and exits 1 when there are any."""
    path = (argv or sys.argv[1:] or [str(DEFAULTS_FILE)])[0]
    with open(path, "rb") as source:
        problems = check_defaults(tomllib.load(source).get("person", []))
    for problem in problems:
        print(problem)
    if not problems:
        print(f"{len(read_defaults(path))} people, no problems")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
