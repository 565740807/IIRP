"""The Insider overview reads its rows from the covering partial index alone."""

import re

from iirp.db import session
from iirp.insider.overview import _rows, overview_rows_clause
from iirp.models.insider import OVERVIEW_COLUMNS, OVERVIEW_ROWS
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from tests.insider.test_overview import AT, seed
from tests.jobs.test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401


class Capture:
    """Stands in for a session: keeps the statement instead of running it."""

    def execute(self, statement):
        self.statement = statement
        return self

    def mappings(self):
        return []


def overview_statement(**kwargs):
    capture = Capture()
    _rows(capture, **{"index": "all", "days": 90, "role": "all", "exclude_plans": False,
                      "at": AT, **kwargs})
    return capture.statement


def test_index_predicate_is_literal_sql_in_the_query():
    sql = str(overview_statement().compile(dialect=postgresql.dialect()))
    assert str(overview_rows_clause()) in sql
    assert re.sub(r"transaction_event\.", "", str(overview_rows_clause())) == OVERVIEW_ROWS


def test_index_covers_every_column_the_query_reads():
    for kwargs in ({}, {"role": "executive", "exclude_plans": True}, {"role": "director"},
                   {"role": "ten_percent", "index": "sp500"}):
        columns = {column.name for column in overview_statement(**kwargs)._all_selected_columns
                   if getattr(column, "table", None) is not None
                   and column.table.name == "transaction_event"}
        compiled = str(overview_statement(**kwargs).compile(dialect=postgresql.dialect()))
        referenced = set(re.findall(r"transaction_event\.(\w+)", compiled))
        assert columns <= referenced
        assert referenced - {"transaction_date", "status", "data"} <= set(OVERVIEW_COLUMNS)


def test_generic_plan_uses_an_index_only_scan():
    with session() as s, s.begin():
        for number in range(30):
            seed(s, issuer=f"{number + 1:010d}", ticker=f"T{number}", price="10")
    # An index-only scan needs the visibility map that VACUUM sets.
    with session() as s:
        s.connection(execution_options={"isolation_level": "AUTOCOMMIT"}).execute(
            text("VACUUM ANALYZE transaction_event"))
    with session() as s, s.begin():
        compiled = overview_statement().compile(dialect=postgresql.dialect(paramstyle="numeric_dollar"))
        values = [compiled.params[name] for name in compiled.positiontup]
        s.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
        s.execute(text("SET LOCAL enable_seqscan = off"))
        s.execute(text("SET LOCAL enable_bitmapscan = off"))
        connection = s.connection()
        connection.exec_driver_sql("PREPARE overview_rows AS " + str(compiled))
        arguments = ", ".join("'" + str(value).replace("'", "''") + "'" for value in values)
        plan = "\n".join(connection.exec_driver_sql(f"EXPLAIN EXECUTE overview_rows({arguments})").scalars())
        connection.exec_driver_sql("DEALLOCATE overview_rows")
    assert "Index Only Scan using ix_event_overview on transaction_event" in plan
