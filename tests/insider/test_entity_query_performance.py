# Entity local-read query bounds; source changes use synthetic isolated fixtures.
from iirp.db import engine, session
from iirp.insider.entities import read_entity_history
from iirp.models import Filing
from sqlalchemy import event

from tests.sec.test_sec_facts import FIXTURE, clean, isolated_database, save  # noqa: F401


def test_entity_ticker_uses_latest_visible_filing_and_stays_frozen():
    with session() as s, s.begin():
        save(s)
        newer = "0000000123-26-000002"
        save(s, xml=FIXTURE.read_bytes().replace(b">DEMO<", b">HIDDEN<"), accession=newer,
             accepted="2026-09-02T18:00:00-04:00")
        s.get(Filing, newer).visible = False
        s.flush()
        page = read_entity_history(s, "company", "123", recent_count=50)
        assert page["data"]["entity"]["ticker"] == "DEMO"
        s.get(Filing, newer).visible = True
        s.flush()
        frozen = read_entity_history(s, "company", "123", recent_count=50,
                                     cursor=page["data"]["session_id"] + ":0")
        assert frozen["data"]["entity"]["ticker"] == "DEMO"
        fresh = read_entity_history(s, "company", "123", recent_count=50)
        assert fresh["data"]["entity"]["ticker"] == "HIDDEN"


def test_immutable_row_loading_is_one_bulk_query_for_all_page_rows():
    with session() as s, s.begin():
        for suffix in range(1, 21):
            save(s, accession=f"0000000123-26-{suffix:06d}")
        page = read_entity_history(s, "company", "123", recent_count=50)
        observed = []
        def capture(conn, cursor, statement, parameters, context, many):
            observed.append(statement)
        event.listen(engine(), "before_cursor_execute", capture)
        try:
            result = read_entity_history(s, "company", "123", recent_count=50, limit=100,
                                         cursor=page["data"]["session_id"] + ":0")
        finally:
            event.remove(engine(), "before_cursor_execute", capture)
        assert len(result["items"]) == 40
        assert len(observed) == 3
        assert sum("FROM filing_version" in query for query in observed) == 1
