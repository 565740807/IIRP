"""Synthetic import acceptance in the same isolated test fixture infrastructure."""

from decimal import Decimal

import pytest
from iirp import lifecycle
from iirp.business_models import (
    Batch,
    EarningsEvent,
    ImportPreview,
    MarketBar,
    Security,
    SourceObservation,
)
from iirp.business_worker import execute_business
from iirp.db import session
from iirp.imports import commit, parse, preview
from iirp.market_data import price_bars
from iirp.models import Job, SourceObject
from iirp.queue import claim
from sqlalchemy import func, select
from test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401


def values(
    csv="date,open,high,low,close,volume\n2024-01-03,100,105,99,103,10\n2024-01-04,103,104,98,99,12\n",
    **kwargs,
):
    return {
        "kind": "market",
        "ticker": "SYNTH",
        "source_url": "https://example.test/explicit-synthetic-fixture",
        "price_basis": "SPLIT_ONLY",
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
    security_id = security()
    p = preview(values())
    assert p["valid_rows"] == 2 and p["errors"] == []
    with session() as s:
        assert s.scalar(select(func.count()).select_from(MarketBar)) == 0
    result, job = complete(p)
    assert commit(p["id"])["batch_id"] == result["batch_id"]
    with session() as s:
        assert s.get(Job, job.id).status == "SUCCEEDED"
        assert s.get(Batch, result["batch_id"]).status == "SUCCEEDED"
        bars, dataset = price_bars(s, security_id)
        assert [Decimal(b["close"]) for b in bars] == [Decimal("103"), Decimal("99")]
        assert dataset.manifest["source_hash"] == s.get(ImportPreview, p["id"]).source_hash
        assert s.scalar(select(func.count()).select_from(SourceObservation)) == 1


def test_invalid_csv_never_silently_drops_rows():
    security()
    p = preview(
        values(
            "date,open,high,low,close\n2024-01-03,1,2,1,2\n2024-01-03,1,2,1,2\n2024-01-06,1,2,1,2\n2024-01-04,1,0,2,2\n"
        )
    )
    assert len(p["errors"]) == 3
    with pytest.raises(ValueError):
        commit(p["id"])
    with session() as s:
        assert s.scalar(select(func.count()).select_from(MarketBar)) == 0


def test_unverified_import_is_observation_only():
    identity = security()
    p = preview(values(price_basis="UNVERIFIED"))
    complete(p)
    with session() as s:
        bars, dataset = price_bars(s, identity)
        assert not bars and dataset is None
        assert s.scalar(select(MarketBar.status).limit(1)) == "UNCONFIRMED"


def test_preview_version_change_rejects_overwrite():
    security()
    old = preview(values())
    new = preview(values())
    complete(new)
    _, job = complete(old)
    with session() as s:
        current = s.get(Job, job.id)
        assert current.status == "FAILED" and "重新预览" in current.error
        assert s.scalar(select(func.count()).select_from(MarketBar)) == 2


def test_cancel_import_preserves_preview_and_no_facts():
    security()
    p = preview(values())
    result = commit(p["id"])
    lifecycle.control_batch(result["batch_id"], "cancel")
    lifecycle.plan_tick()
    assert claim({"local_import"}) is None
    with session() as s:
        assert s.get(ImportPreview, p["id"]).source_hash
        assert s.scalar(select(func.count()).select_from(MarketBar)) == 0


def test_fiscal_csv_preserves_candidates_without_granting_verification():
    security()
    csv = "announced_date,fiscal_year,fiscal_quarter,time_precision,announced_at\n2024-01-03,2024,1,date_only,\n2024-04-03,2024,2,after_close,\n"
    p = preview(values(csv, kind="earnings"))
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
            kind="earnings",
        )
    )
    assert errors


def test_partial_csv_cannot_mix_adjustment_basis_with_saved_history():
    identity = security()
    complete(preview(values()))
    with session() as s:
        _, original = price_bars(s, identity)
        original_id = original.id
    p = preview(values("date,open,high,low,close\n2024-01-04,50,50,50,50\n"))
    assert any("不同来源" in x for x in p["differences"])
    _, job = complete(p)
    with session() as s:
        bars, current = price_bars(s, identity)
        assert current.id == original_id
        assert [Decimal(b["close"]) for b in bars] == [Decimal("103"), Decimal("99")]
        assert s.get(Job, job.id).status == "PARTIAL"


def test_complete_csv_replacement_has_own_basis_and_preserves_previous_version():
    identity = security()
    complete(preview(values()))
    with session() as s:
        _, original = price_bars(s, identity)
        old_id, old_key = original.id, original.basis_key
    complete(
        preview(
            values(
                "date,open,high,low,close\n2024-01-03,51.5,51.5,51.5,51.5\n2024-01-04,49.5,49.5,49.5,49.5\n"
            )
        )
    )
    with session() as s:
        rows, current = price_bars(s, identity)
        assert current.id != old_id and current.basis_key != old_key
        assert current.manifest["provider"] == "manual_csv"
        assert [Decimal(x["close"]) for x in rows] == [Decimal("51.5"), Decimal("49.5")]
        previous, _ = price_bars(s, identity, old_id)
        assert [Decimal(x["close"]) for x in previous] == [Decimal("103"), Decimal("99")]
