"""Synthetic earnings CSV import acceptance on an isolated test database.

Prices are a 24-hour provider cache (D14); CSV price imports are rejected.
"""

import pytest
from iirp import lifecycle
from iirp.business_models import (
    Batch,
    EarningsEvent,
    ImportPreview,
    Security,
    SourceObservation,
)
from iirp.business_worker import execute_business
from iirp.db import session
from iirp.imports import commit, parse, preview
from iirp.models import Job, SourceObject
from iirp.queue import claim
from sqlalchemy import func, select
from test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401

EARNINGS = "announced_date,fiscal_year,fiscal_quarter,time_precision,announced_at\n2024-01-03,2024,1,date_only,\n2024-04-03,2024,2,after_close,\n"


def values(csv=EARNINGS, **kwargs):
    return {
        "kind": "earnings",
        "ticker": "SYNTH",
        "source_url": "https://example.test/explicit-synthetic-fixture",
        "csv": csv,
        **kwargs,
    }


def security():
    with session() as s, s.begin():
        row = Security(
            symbol="SYNTH",
            name="Synthetic fixture",
            status="VERIFIED",
            instrument="EQUITY",
            currency="USD",
            exchange="NYQ",
            calendar="XNYS",
        )
        s.add(row)
        s.flush()
        return row.id


def complete(p):
    result = commit(p["id"])
    lifecycle.plan_tick()
    job = claim({"local_import"})
    assert job is not None
    execute_business(job)
    lifecycle.plan_tick()
    return result, job


def test_preview_is_not_business_data_and_duplicate_commit_is_durable():
    security()
    p = preview(values())
    assert p["valid_rows"] == 2 and p["errors"] == []
    with session() as s:
        assert s.scalar(select(func.count()).select_from(EarningsEvent)) == 0
    result, job = complete(p)
    assert commit(p["id"])["batch_id"] == result["batch_id"]
    with session() as s:
        assert s.get(Job, job.id).status in ("SUCCEEDED", "PARTIAL")
        assert s.get(Batch, result["batch_id"]).status in ("SUCCEEDED", "PARTIAL")
        assert s.get(ImportPreview, p["id"]).status == "COMMITTED"
        assert s.scalar(select(func.count()).select_from(EarningsEvent)) == 2
        assert s.scalar(select(func.count()).select_from(SourceObservation)) == 1


def test_price_csv_is_rejected():
    with pytest.raises(ValueError, match="24 小时缓存"):
        parse({**values(), "kind": "market", "csv": "date,open,high,low,close\n2024-01-03,1,2,1,2\n"})


def test_invalid_csv_never_silently_drops_rows():
    security()
    p = preview(values(
        "announced_date,fiscal_year,fiscal_quarter,time_precision\n2024-01-03,2024,1,date_only\n"
        "2024-01-03,2024,1,date_only\n2024-01-05,2024,9,date_only\n2024-01-06,2024,1,someday\n"))
    assert len(p["errors"]) == 3
    with pytest.raises(ValueError):
        commit(p["id"])
    with session() as s:
        assert s.scalar(select(func.count()).select_from(EarningsEvent)) == 0


def test_cancel_import_preserves_preview_and_no_facts():
    security()
    p = preview(values())
    result = commit(p["id"])
    lifecycle.control_batch(result["batch_id"], "cancel")
    lifecycle.plan_tick()
    assert claim({"local_import"}) is None
    with session() as s:
        assert s.get(ImportPreview, p["id"]).source_hash
        assert s.scalar(select(func.count()).select_from(EarningsEvent)) == 0


def test_fiscal_csv_preserves_candidates_without_granting_verification():
    security()
    p = preview(values())
    assert not p["errors"]
    complete(p)
    with session() as s:
        events = s.scalars(select(EarningsEvent).order_by(EarningsEvent.announced_date)).all()
        assert [e.fiscal_quarter for e in events] == [1, 2]
        assert events[0].time_precision == "date_only" and events[0].announced_at is None
        assert all(not e.verified and e.status == "CANDIDATE" and e.revision == 1 for e in events)
        assert all(e.announced_at is None and e.time_precision == "date_only" for e in events)
        assert events[1].evidence[0]["candidate"]["time_precision"] == "after_close"
        assert all(s.get(SourceObject, e.evidence[0]["source_hash"]) is not None for e in events)


@pytest.mark.parametrize("stamp", ["2024-01-03T08:00:00", "2024-01-05T08:00:00+00:00", ""])
def test_exact_earnings_csv_requires_coherent_timestamp(stamp):
    _, _, errors = parse(
        values(
            f"announced_date,fiscal_year,fiscal_quarter,time_precision,announced_at\n2024-01-03,2024,1,exact,{stamp}\n",
        )
    )
    assert errors
