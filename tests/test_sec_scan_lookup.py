"""SEC latest-scan lookup: answered from a partial index, never a job-table scan.

Disposable iirp_v1_test_* database; all jobs are synthetic and never executed.
"""
from datetime import timedelta

from alembic import command
from alembic.config import Config
from iirp.config import ROOT
from iirp.db import engine, session
from iirp.models import Job, latest_complete_sec_scan, now
from sqlalchemy import insert, text
from test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401

INDEX = "ix_job_sec_latest_complete"


def job(index, *, stamp, complete=False, status="SUCCEEDED", mode="latest", kind="sec_discover"):
    scan = {"mode": mode, "complete": complete, "reason": "page_budget",
            "newest_accepted_at": stamp.isoformat()}
    return {"id": f"00000000-0000-4000-8000-{index:012d}", "kind": kind, "title": f"Synthetic {index}",
            "target": {"mode": mode}, "idempotency_key": f"synthetic-{index}", "status": status,
            "checkpoint": {"sec_scan": scan, "filler": "x" * 400}, "created_at": stamp,
            "updated_at": stamp, "available_at": stamp}


def seed(rows):
    with session() as s, s.begin():
        s.execute(insert(Job), rows)
        s.execute(text("ANALYZE job"))


def plan(statement):
    """EXPLAIN with parameters bound exactly as the application sends them."""
    with engine().connect() as connection:
        compiled = statement.compile(dialect=connection.dialect)
        return "\n".join(row[0] for row in connection.exec_driver_sql(
            "EXPLAIN " + str(compiled), compiled.params))


def test_lookup_returns_newest_complete_latest_scan():
    stamp = now()
    seed([
        job(1, stamp=stamp - timedelta(hours=5), complete=True),
        job(2, stamp=stamp - timedelta(hours=3), complete=True),
        job(3, stamp=stamp - timedelta(hours=1)),  # newer, incomplete
        job(4, stamp=stamp, complete=True, status="FAILED"),
        job(5, stamp=stamp, complete=True, mode="history"),
        job(6, stamp=stamp, complete=True, kind="sec_document"),
    ])
    with session() as s:
        assert s.scalar(latest_complete_sec_scan()).id.endswith("000000000002")
        assert s.scalar(latest_complete_sec_scan(before=stamp - timedelta(hours=4))).id.endswith("000000000001")
        assert s.scalar(latest_complete_sec_scan(before=stamp - timedelta(hours=6))) is None


def test_lookup_uses_partial_index_when_no_scan_has_completed():
    # The live failure: hundreds of thousands of incomplete scans, none complete.
    stamp = now()
    seed([job(index, stamp=stamp - timedelta(minutes=index)) for index in range(3000)])
    for statement in (latest_complete_sec_scan(), latest_complete_sec_scan(before=stamp)):
        explained = plan(statement)
        assert INDEX in explained, explained
        assert "Seq Scan" not in explained, explained
    with session() as s:
        assert s.scalar(latest_complete_sec_scan()) is None


def test_repeated_lookups_keep_plans_that_can_use_the_partial_index():
    # psycopg prepares a statement after five runs; a generic plan (parameters
    # unknown) could not prove the partial-index predicate.
    stamp = now()
    seed([job(index, stamp=stamp - timedelta(minutes=index)) for index in range(3000)])
    with session() as s:
        for _ in range(12):
            assert s.scalar(latest_complete_sec_scan(before=now())) is None
        plans = s.execute(text(
            "SELECT generic_plans, custom_plans FROM pg_prepared_statements "
            "WHERE statement LIKE '%FROM job%ORDER BY job.created_at DESC%'")).all()
    assert plans and all(generic == 0 and custom > 0 for generic, custom in plans), plans


def test_migration_creates_valid_index_and_downgrade_removes_it():
    def state():
        with engine().connect() as connection:
            return connection.scalar(text(
                "SELECT i.indisvalid FROM pg_index i WHERE i.indexrelid = to_regclass(:name)"),
                {"name": INDEX})

    assert state() is True
    configuration = Config(str(ROOT / "alembic.ini"))
    try:
        command.downgrade(configuration, "0022")
        assert state() is None
    finally:
        command.upgrade(configuration, "head")
    assert state() is True
