"""Synthetic earnings evidence and real isolated PostgreSQL lifecycle tests.

No external source requests run here and these fixtures claim no live coverage.
"""

import hashlib
import json
import os
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pandas as pd
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from iirp import earnings_planner
from iirp.analytics.calendar import reaction_session, session_window
from iirp.analytics.research import compute_research, plan_scope
from iirp.business_models import Batch, BatchJob, EarningsEvent, Issuer, RequestScope, Security
from iirp.config import ROOT, settings
from iirp.db import engine, session
from iirp.earnings_data import (
    correct_event,
    event_dict,
    extract_release,
    fetch_candidates,
    fetch_evidence,
    persist_candidates,
    persist_evidence,
)
from iirp.earnings_planner import plan_earnings_discovery
from iirp.lifecycle import control_batch
from iirp.models import Base, Job, SourceObject
from psycopg import sql
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

ASOF = datetime(2025, 10, 2, 20, 5, tzinfo=timezone.utc)


@pytest.fixture(scope="module", autouse=True)
def isolated_database():
    url = make_url(settings().database_url)
    name = "iirp_v1_test_earnings_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname="postgres",
        autocommit=True,
    )
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    previous = os.environ.get("IIRP_DATABASE_URL")
    os.environ["IIRP_DATABASE_URL"] = url.set(database=name).render_as_string(hide_password=False)
    engine.cache_clear()
    settings.cache_clear()
    try:
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        yield
    finally:
        engine().dispose()
        engine.cache_clear()
        if previous is None:
            os.environ.pop("IIRP_DATABASE_URL", None)
        else:
            os.environ["IIRP_DATABASE_URL"] = previous
        settings.cache_clear()
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    with engine().begin() as connection:
        connection.execute(
            text(
                "TRUNCATE "
                + ",".join('"' + table.name + '"' for table in Base.metadata.sorted_tables)
                + " CASCADE"
            )
        )
    monkeypatch.setattr(earnings_planner, "now", lambda: ASOF)


def setup(s, symbol="TEST", fiscal_end="12-31", issuer=True, params=None, created=ASOF):
    if issuer and s.get(Issuer, "0000000123") is None:
        s.add(Issuer(id="0000000123", name="Synthetic issuer"))
        s.flush()
    security = Security(
        symbol=symbol,
        name="Synthetic security",
        status="VERIFIED",
        instrument="EQUITY",
        currency="USD",
        exchange="NMS",
        calendar="XNYS",
        issuer_id="0000000123" if issuer else None,
        metadata_json={"verified_fiscal_year_end": fiscal_end} if fiscal_end else {},
    )
    s.add(security)
    s.flush()
    values = {
        "historical_years": 1,
        "analysis_params": {"historical_years": 1, "kind": "earnings"},
        **(params or {}),
    }
    batch = Batch(
        request_id=str(uuid.uuid4()),
        scope_key=uuid.uuid4().hex,
        kind="earnings",
        title="Synthetic earnings",
        params=values,
        created_at=created,
    )
    s.add(batch)
    s.flush()
    scope = RequestScope(
        batch_id=batch.id,
        symbol=symbol,
        security_id=security.id,
        start_date=date(2024, 1, 1),
        end_date=created.date(),
    )
    s.add(scope)
    s.flush()
    return security, batch, scope


def source(s, value):
    payload = json.dumps(value, sort_keys=True).encode()
    digest = hashlib.sha256(payload).hexdigest()
    if not s.get(SourceObject, digest):
        s.add(
            SourceObject(
                sha256=digest,
                relative_path="synthetic/" + digest,
                byte_size=len(payload),
                media_type="application/json",
            )
        )
        s.flush()
    return digest


def event(s, security, day="2025-01-30", year=2024, quarter=4, precision="after_close", **extra):
    item = EarningsEvent(
        security_id=security.id,
        announced_date=date.fromisoformat(day),
        fiscal_year=year,
        fiscal_quarter=quarter,
        announced_at=None,
        time_precision=precision,
        verified=True,
        is_estimate=False,
        status="VERIFIED",
        evidence=[
            {
                "provider": "synthetic", "source_url": "https://example.com/synthetic-date",
                "announced_date": day, "fiscal_year": year, "fiscal_quarter": quarter,
                "time_precision": precision if precision in {"before_open", "after_close"} else "date_only",
                "time_evidence": f"Synthetic issuer states results actually released {precision}" if precision in {"before_open", "after_close"} else None,
                "note": "Synthetic independently stated announcement date",
            },
            {
                "provider": "synthetic", "source_url": "https://example.com/synthetic-quarterly-report",
                "fiscal_year": year, "fiscal_quarter": quarter,
                "period_kind": "regular",
                "period_kind_evidence": "Synthetic filing explicitly identifies an ordinary fiscal quarter",
            },
        ],
        revision=1,
        **extra,
    )
    s.add(item)
    s.flush()
    return item


def finish_candidates(s, job, cursor=None, complete=True, reason=None):
    response = {
        "records": [],
        "cursor": cursor,
        "complete": complete,
        "reason": reason,
        "fingerprint": "synthetic-page-" + str(job.target.get("offset", 0)),
        "searched_to": job.target["start_date"],
    }
    persist_candidates(s, job, response, source(s, response))
    job.status = "SUCCEEDED"


def finish_evidence(s, job, followups=None, complete=True, reason=None):
    job.checkpoint = {
        "earnings_evidence": {
            "followups": followups or [],
            "complete": complete,
            "reason": reason,
            "gaps": [],
            "kind": "listing",
        }
    }
    job.status = "SUCCEEDED"


def test_capacity_one_rolls_candidate_pages_sec_files_and_documents_after_restart():
    with session() as s, s.begin():
        security, batch, scope = setup(s)
        batch_id, scope_id = batch.id, scope.id
        result = plan_earnings_discovery(s, scope, batch, [1])
        assert result["discovery_pending"]
        assert s.scalar(select(func.count()).select_from(Job)) == 1
        first = s.scalar(select(Job))
        finish_candidates(
            s,
            first,
            {
                "offset": 100,
                "previous_oldest": "2024-06-01",
                "previous_fingerprint": "first",
                "page_fingerprints": ["first"],
            },
            False,
        )
    seen = []
    for _ in range(8):
        # New sessions simulate complete coordinator state loss/restart.
        with session() as s, s.begin():
            batch, scope = s.get(Batch, batch_id), s.get(RequestScope, scope_id)
            before = s.scalar(select(func.count()).select_from(Job))
            result = plan_earnings_discovery(s, scope, batch, [1])
            assert s.scalar(select(func.count()).select_from(Job)) - before <= 1
            active = s.scalar(select(Job).where(Job.status == "QUEUED"))
            if active is None:
                break
            seen.append((active.kind, active.target))
            if active.kind == "earnings_candidates":
                finish_candidates(s, active)
            elif not active.target.get("filename") and not active.target.get("submission_url"):
                target = {**active.target, "filename": "CIK0000000123-submissions-001.json"}
                finish_evidence(s, active, [target])
            elif active.target.get("filename"):
                target = {key: value for key, value in active.target.items() if key != "filename"}
                target.update(
                    {
                        "submission_url": "https://www.sec.gov/Archives/edgar/data/123/000000012324000001/0000000123-24-000001.txt",
                        "filing_date": "2024-10-01",
                    }
                )
                finish_evidence(s, active, [target])
            else:
                finish_evidence(s, active)
    assert len(seen) == 4
    assert any(kind == "earnings_candidates" and target["offset"] == 100 for kind, target in seen)
    assert not result["discovery_pending"]
    assert result["partial"] and "missing_event" in result["reason"]
    assert not result["price_ready"]


def test_zero_capacity_keeps_pending_and_does_not_truncate_normal_history():
    with session() as s, s.begin():
        _, batch, scope = setup(
            s, params={"historical_years": 12, "analysis_params": {"historical_years": 12}}
        )
        result = plan_earnings_discovery(s, scope, batch, [0])
        assert result["discovery_pending"]
        assert s.scalar(select(func.count()).select_from(Job)) == 0
        assert scope.checkpoint["earnings"]["envelope"]["start_date"] == "2012-01-01"
        assert len(result["coverage"]["matrix"]) == 48


def test_pause_prevents_expansion_and_resume_keeps_frozen_search_envelope(monkeypatch):
    with session() as s, s.begin():
        _, batch, scope = setup(s)
        plan_earnings_discovery(s, scope, batch, [1])
        batch_id, scope_id = batch.id, scope.id
        envelope = scope.checkpoint["earnings"]["envelope"]
    control_batch(batch_id, "pause")
    monkeypatch.setattr(earnings_planner, "now", lambda: datetime(2026, 1, 10, tzinfo=timezone.utc))
    with session() as s, s.begin():
        result = plan_earnings_discovery(
            s, s.get(RequestScope, scope_id), s.get(Batch, batch_id), [10]
        )
        assert result["reason"] == "batch_control_intent_preserved"
        assert s.scalar(select(func.count()).select_from(Job)) == 1
        assert s.scalar(select(BatchJob.active)) is False
    control_batch(batch_id, "resume")
    with session() as s, s.begin():
        scope = s.get(RequestScope, scope_id)
        plan_earnings_discovery(s, scope, s.get(Batch, batch_id), [1])
        assert scope.checkpoint["earnings"]["envelope"] == envelope
        assert s.scalar(select(Job).where(Job.kind == "earnings_candidates")).status == "QUEUED"


def test_two_batches_share_finished_and_active_pages_at_zero_new_capacity():
    with session() as s, s.begin():
        security, batch, scope = setup(s)
        plan_earnings_discovery(s, scope, batch, [2])
        second = Batch(
            request_id=str(uuid.uuid4()),
            scope_key=uuid.uuid4().hex,
            kind="earnings",
            title="Second manual demand",
            params=batch.params,
            created_at=batch.created_at,
        )
        s.add(second)
        s.flush()
        scope2 = RequestScope(
            batch_id=second.id,
            security_id=security.id,
            symbol=security.symbol,
            start_date=scope.start_date,
            end_date=scope.end_date,
        )
        s.add(scope2)
        s.flush()
        result = plan_earnings_discovery(s, scope2, second, [0])
        assert result["discovery_pending"]
        assert s.scalar(select(func.count()).select_from(Job)) == 2
        assert s.scalar(select(func.count()).select_from(BatchJob)) == 4


def test_failed_page_is_finite_partial_and_is_not_automatically_recreated():
    with session() as s, s.begin():
        _, batch, scope = setup(s)
        plan_earnings_discovery(s, scope, batch, [2])
        for job in s.scalars(select(Job)):
            job.status, job.error = "FAILED", "source_unavailable"
        for _ in range(3):
            result = plan_earnings_discovery(s, scope, batch, [20])
            assert result["partial"] and not result["discovery_pending"]
            assert "source_unavailable" in result["reason"]
        assert s.scalar(select(func.count()).select_from(Job)) == 2


def test_sec_identity_is_requested_before_issuer_submissions():
    with session() as s, s.begin():
        security, batch, scope = setup(s, issuer=False, fiscal_end=None)
        plan_earnings_discovery(s, scope, batch, [3])
        assert set(s.scalars(select(Job.kind))) == {"earnings_candidates", "sec_identity"}
        for job in s.scalars(select(Job)):
            if job.kind == "earnings_candidates":
                finish_candidates(s, job)
            else:
                job.status = "SUCCEEDED"
        s.add(Issuer(id="0000000123", name="Synthetic SEC match"))
        s.flush()
        security.issuer_id = "0000000123"
        result = plan_earnings_discovery(s, scope, batch, [1])
        assert result["discovery_pending"]
        assert (
            s.scalar(select(Job).where(Job.kind == "earnings_evidence")).target["cik"]
            == "0000000123"
        )


@pytest.mark.parametrize(
    ("fiscal_end", "current", "historical"), [("09-30", 2026, 2025), ("12-31", 2025, 2024)]
)
def test_complete_history_is_fiscal_years_and_price_scope_comes_from_actual_events(
    fiscal_end, current, historical
):
    with session() as s, s.begin():
        security, batch, scope = setup(s, fiscal_end=fiscal_end)
        examples = []
        for quarter, month in enumerate((1, 4, 7, 10), 1):
            examples.append(event(s, security, f"{historical}-{month:02d}-01", historical, quarter))
        result = plan_earnings_discovery(s, scope, batch, [2])
        assert result["coverage"]["current_fiscal_year"] == current
        assert result["coverage"]["historical_fiscal_years"] == [historical]
        assert len(result["coverage"]["matrix"]) == 4
        assert all(item["status"] == "VERIFIED" for item in result["coverage"]["matrix"])
        assert result["price_ready"]
        expected = plan_scope(
            {"kind": "earnings", "events": [event_dict(item) for item in examples]}, ASOF.date()
        )
        assert (scope.start_date, scope.end_date) == expected
        assert scope.start_date != date(historical - 1, 1, 1)


def test_duplicate_and_unknown_time_events_cannot_close_fiscal_coverage():
    with session() as s, s.begin():
        security, batch, scope = setup(s)
        event(s, security, "2024-02-01", 2024, 1)
        event(s, security, "2024-02-02", 2024, 1)
        event(s, security, "2024-05-01", 2024, 2, "date_only")
        result = plan_earnings_discovery(s, scope, batch, [0])
        statuses = {item["status"] for item in result["coverage"]["matrix"]}
        assert {
            "duplicate_primary_events",
            "precise_announcement_time_unconfirmed",
            "missing_event",
        } <= statuses
        assert result["price_ready"] and result["partial"]


@pytest.mark.parametrize(
    ("offset", "mature"),
    [
        (1, [True, False, False, False]),
        (5, [True, True, False, False]),
        (20, [True, True, True, False]),
        (60, [True, True, True, True]),
    ],
)
def test_each_earnings_window_matures_independently(offset, mature, monkeypatch):
    with session() as s, s.begin():
        security, batch, scope = setup(s)
        item = event(s, security)
        anchor = reaction_session(
            None, announced_date=item.announced_date, time_precision="after_close"
        )
        days = session_window(date.fromisoformat(anchor["baseline_date"]), 20, 60)
        clock = datetime.combine(days[20 + offset], datetime.min.time(), timezone.utc).replace(
            hour=22
        )
        monkeypatch.setattr(earnings_planner, "now", lambda: clock)
        result = plan_earnings_discovery(s, scope, batch, [0])
        windows = result["coverage"]["maturity"][0]["windows"]
        assert [windows[str(window)]["mature"] for window in (1, 5, 20, 60)] == mature
        bars = [
            {"date": str(day), "open": "110", "close": "121", "status": "VALID"}
            for day in days
            if day <= clock.date()
        ]
        bars[20]["close"] = "100"
        computed = compute_research(
            {
                "kind": "earnings",
                "historical_years": 1,
                "current_fiscal_year": 2025,
                "quarter": 4,
                "window": 60,
            },
            bars,
            [event_dict(item)],
            today=clock.date(),
        )
        assert [computed["summary"]["windows"][str(window)]["n"] for window in (1, 5, 20, 60)] == [
            int(value) for value in mature
        ]


def test_candidates_are_fresh_objects_and_repeated_page_with_changed_eps_stops():
    constructed = []

    class FakeTicker:
        def __init__(self, symbol):
            constructed.append(self)

        def get_earnings_dates(self, *, limit, offset):
            assert limit == 2
            return pd.DataFrame(
                {"EPS Estimate": [len(constructed), 1]},
                index=pd.to_datetime(["2025-05-01T16:00:00Z", "2025-02-01T16:00:00Z"]),
            )

    base = {"symbol": "TEST", "start_date": "2024-01-01", "offset": 0, "page_size": 2}
    first = fetch_candidates(base, ticker_factory=FakeTicker)
    second = fetch_candidates({**base, **first["cursor"]}, ticker_factory=FakeTicker)
    assert len(constructed) == 2 and constructed[0] is not constructed[1]
    assert second["reason"] == "candidate_repeated_page"
    assert second["cursor"] is None and not second["complete"]


@pytest.mark.parametrize(
    ("dates", "extra", "reason"),
    [
        (
            ["2025-05-01", "2025-02-01"],
            {"previous_oldest": "2025-01-01"},
            "candidate_history_not_advancing",
        ),
        (["2025-05-01"], {}, "candidate_source_exhausted_before_requested_start"),
        ([], {}, "candidate_source_empty"),
    ],
)
def test_candidate_nonprogress_and_exhaustion_are_not_complete(dates, extra, reason):
    class Fake:
        def get_earnings_dates(self, **kwargs):
            return pd.DataFrame({"EPS": [1] * len(dates)}, index=pd.to_datetime(dates))

    result = fetch_candidates(
        {"symbol": "TEST", "start_date": "2024-01-01", "page_size": 2, **extra},
        ticker_factory=lambda symbol: Fake(),
    )
    assert not result["complete"] and result["reason"] == reason


def test_candidate_source_idempotence_and_manual_facts_are_preserved():
    with session() as s, s.begin():
        security, _, _ = setup(s)
        item = event(s, security)
        item.evidence = [{"provider": "manual_review", "note": "Synthetic verified fixture"}]
        job = SimpleNamespace(
            id=None,
            target={
                "security_id": security.id,
                "start_date": "2024-01-01",
                "end_date": "2025-12-31",
            },
            checkpoint={},
        )
        response = {
            "records": [
                {"announced_date": "2025-01-30", "announced_at": "2025-01-30T08:00:00-05:00"},
                {"announced_date": "2025-05-01", "announced_at": "2025-05-01T08:00:00-04:00"},
            ],
            "complete": True,
        }
        digest = source(s, response)
        persist_candidates(s, job, response, digest)
        persist_candidates(s, job, response, digest)
        assert s.scalar(select(func.count()).select_from(EarningsEvent)) == 2
        assert (item.fiscal_year, item.fiscal_quarter, item.time_precision, item.verified) == (
            2024,
            4,
            "after_close",
            True,
        )
        assert item.announced_at is None
        candidate = s.scalar(
            select(EarningsEvent).where(EarningsEvent.announced_date == date(2025, 5, 1))
        )
        assert candidate.revision == 1 and not candidate.verified
        assert candidate.announced_at is None and candidate.time_precision == "date_only"


def release(**overrides):
    return {
        "announced_date": "2025-01-30",
        "announced_at": None,
        "time_precision": "date_only",
        "fiscal_year": 2024,
        "fiscal_quarter": 4,
        "source_url": "https://www.sec.gov/Archives/edgar/data/123/release.htm",
        **overrides,
    }


def save_release(s, security, record, marker):
    response = {"releases": [record], "complete": True, "kind": "document"}
    digest = source(s, {**response, "fixture": marker})
    job = SimpleNamespace(
        id=None,
        target={"security_id": security.id, "start_date": "2024-01-01", "end_date": "2025-12-31"},
        checkpoint={},
    )
    persist_evidence(s, job, response, {}, digest)
    s.flush()
    return s.scalar(select(EarningsEvent).where(EarningsEvent.security_id == security.id))


def test_conflicting_release_stays_conflict_until_manual_review():
    with session() as s, s.begin():
        security, _, _ = setup(s)
        item = save_release(s, security, release(), "first")
        assert item.revision == 1 and item.status == "DATE_VERIFIED"
        save_release(s, security, release(fiscal_quarter=3), "conflict")
        assert item.status == "CONFLICT" and not item.verified
        save_release(s, security, release(), "later matching source")
        assert item.status == "CONFLICT" and not item.verified
        result = correct_event(
            s,
            item.id,
            {
                "revision": item.revision,
                "source_url": "https://example.com/synthetic-review",
                "note": "Synthetic explicit fiscal reconciliation",
                    "time_evidence": "Synthetic issuer statement: results actually released after market close",
                "announced_date": "2025-01-30",
                "announced_at": None,
                "time_precision": "after_close",
                "fiscal_year": 2024,
                "fiscal_quarter": 4,
            },
        )
        assert result["items"][0]["verified"]
        save_release(s, security, release(fiscal_quarter=3), "automatic after manual")
        assert item.fiscal_quarter == 4 and item.time_precision == "after_close" and item.verified
        assert len(item.evidence) == 5


@pytest.mark.parametrize(
    "extra",
    [
        "The company will hold a conference call at 5:00 p.m. EDT.",
        "5:00 p.m. EDT conference call details follow.",
    ],
)
def test_conference_clock_is_not_announcement_timestamp(extra):
    html = f"<h1>Example reports fiscal 2025 third quarter results</h1><p>July 30, 2025 {extra}</p>".encode()
    parsed = extract_release(
        html, filed_date="2025-07-31", source_url="https://example.com/release"
    )
    assert parsed["fiscal_year"] == 2025 and parsed["fiscal_quarter"] == 3
    assert parsed["announced_date"] == "2025-07-30"
    assert parsed["announced_at"] is None and parsed["time_precision"] == "date_only"


def test_real_publication_metadata_time_is_preserved_and_sec_acceptance_is_ignored():
    html = b'<meta property="article:published_time" content="2025-07-30T16:05:00-04:00"><h1>Example reports fiscal 2025 third quarter results</h1><p>July 30, 2025 The conference call is at 5:00 p.m. EDT.</p>'
    result = extract_release(
        html, filed_date="2025-07-31", source_url="https://example.com/release"
    )
    assert result["announced_at"] == "2025-07-30T16:05:00-04:00"
    assert result["time_precision"] == "exact"


def test_future_conference_notice_does_not_become_an_actual_release():
    html = b"<h1>Example schedules earnings conference call to announce fiscal 2025 third quarter results</h1><p>July 30, 2025</p>"
    result = extract_release(
        html, filed_date="2025-07-30", source_url="https://example.com/release"
    )
    assert result["announced_date"] is None
    assert result["reason"] == "not_an_actual_earnings_release"


def test_verified_fiscal_end_requires_matching_cik_and_symbol():
    with session() as s, s.begin():
        security, _, _ = setup(s, fiscal_end=None)
        target = {
            "security_id": security.id,
            "symbol": "TEST",
            "cik": "0000000123",
            "start_date": "2024-01-01",
            "end_date": "2025-12-31",
        }
        data = {
            "cik": 123,
            "tickers": ["TEST"],
            "fiscalYearEnd": "0930",
            "filings": {
                "recent": {"form": [], "filingDate": [], "accessionNumber": []},
                "files": [],
            },
        }
        response = fetch_evidence(target, lambda url: json.dumps(data).encode())
        digest = source(s, data)
        persist_evidence(
            s, SimpleNamespace(id=None, target=target, checkpoint={}), response, {}, digest
        )
        assert security.metadata_json["verified_fiscal_year_end"] == "09-30"
        assert security.metadata_json["fiscal_year_end_evidence"]["source_hash"] == digest
        security.metadata_json = {}
        data["tickers"] = ["OTHER"]
        response = fetch_evidence(target, lambda url: json.dumps(data).encode())
        persist_evidence(
            s, SimpleNamespace(id=None, target=target, checkpoint={}), response, {}, digest
        )
        assert "verified_fiscal_year_end" not in security.metadata_json


@pytest.mark.parametrize("mutation", ["cik", "length", "filename", "url"])
def test_malformed_or_cross_issuer_sec_evidence_is_rejected(mutation):
    target = {
        "security_id": "synthetic",
        "symbol": "TEST",
        "cik": "0000000123",
        "start_date": "2024-01-01",
        "end_date": "2025-12-31",
    }
    data = {
        "cik": 123,
        "tickers": ["TEST"],
        "filings": {"recent": {"form": [], "filingDate": [], "accessionNumber": []}, "files": []},
    }
    if mutation == "cik":
        data["cik"] = 456
    elif mutation == "length":
        data["filings"]["recent"]["form"] = ["8-K"]
    elif mutation == "filename":
        target["filename"] = "../private.json"
    else:
        target["submission_url"] = "http://127.0.0.1/private"
    with pytest.raises(ValueError):
        fetch_evidence(target, lambda url: json.dumps(data).encode())


def test_partial_listing_still_expands_followup_and_retains_gap():
    with session() as s, s.begin():
        _, batch, scope = setup(s)
        plan_earnings_discovery(s, scope, batch, [2])
        job = s.scalar(select(Job).where(Job.kind == "earnings_evidence"))
        followup = {**job.target, "filename": "CIK0000000123-submissions-001.json"}
        finish_evidence(s, job, [followup], complete=False, reason="one_source_gap")
        job.status, job.error = "PARTIAL", "one_source_gap"
        result = plan_earnings_discovery(s, scope, batch, [1])
        assert result["discovery_pending"] and "one_source_gap" in result["reason"]
        assert s.scalar(select(func.count()).select_from(Job)) == 3


def test_secondary_event_review_excludes_duplicate_and_preserves_history():
    with session() as s, s.begin():
        security, batch, scope = setup(s)
        first = event(s, security, "2024-02-01", 2024, 1)
        second = event(s, security, "2024-02-02", 2024, 1)
        result = correct_event(
            s,
            second.id,
            {
                "revision": second.revision,
                "source_url": "https://example.com/synthetic",
                "note": "Supplementary announcement, primary is the previous day",
                    "time_evidence": "Synthetic issuer statement: results actually released after market close",
                "announced_date": "2024-02-02",
                "time_precision": "after_close",
                "fiscal_year": 2024,
                "fiscal_quarter": 1,
                "is_primary": False,
            },
        )
        assert not result["items"][0]["is_primary"]
        assert second.status == "SECONDARY" and second.evidence[-1]["previous"]["is_primary"]
        coverage = plan_earnings_discovery(s, scope, batch, [0])["coverage"]
        quarter = coverage["matrix"][0]
        assert quarter["status"] == "VERIFIED" and quarter["event_ids"] == [first.id]


def test_date_and_publication_metadata_conflict_is_kept_out_of_precise_statistics():
    html = b'<meta property="article:published_time" content="2025-07-31T16:05:00-04:00"><h1>Example reports Third Quarter 2025 Results</h1><p>July 30, 2025 Results are provided below.</p>'
    parsed = extract_release(
        html, filed_date="2025-07-31", source_url="https://example.com/release"
    )
    assert parsed["fiscal_year"] == 2025 and parsed["fiscal_quarter"] == 3
    assert parsed["announced_date"] == "2025-07-30" and parsed["announced_at"] is None
    assert parsed["time_precision"] == "conflict"
    with session() as s, s.begin():
        security, _, _ = setup(s)
        item = save_release(s, security, parsed, "conflicting publication metadata")
        assert item.status == "CONFLICT" and not item.verified
        assert not event_dict(item)["precise_time_supported"]


def submission(main, release):
    return (
        "<DOCUMENT>\n<TYPE>8-K\n<FILENAME>main.htm\n<TEXT>"
        + main
        + "</TEXT>\n</DOCUMENT>"
        + "<DOCUMENT>\n<TYPE>EX-99.1\n<FILENAME>release.htm\n<TEXT>"
        + release
        + "</TEXT>\n</DOCUMENT>"
    ).encode()


def document_target():
    return {
        "security_id": "synthetic",
        "symbol": "TEST",
        "cik": "0000000123",
        "start_date": "2018-01-01",
        "end_date": "2025-12-31",
        "fiscal_year_end": "12-31",
        "filing_date": "2025-01-30",
        "submission_url": "https://www.sec.gov/Archives/edgar/data/123/000000012325000001/0000000123-25-000001.txt",
    }


def test_modern_dateless_release_uses_explicit_issuer_statement_and_ignores_proud_to_report():
    main = "<p>On January 30, 2025, Example Inc. issued a press release regarding its financial results. A copy is attached as Exhibit 99.1.</p>"
    release = "<h1>Example reports first quarter results</h1><p>Today Example announced financial results for its fiscal 2025 first quarter ended December 28, 2024. We are proud to report record revenue.</p>"
    result = fetch_evidence(document_target(), lambda url: submission(main, release))
    event = result["releases"][0]
    assert (event["announced_date"], event["fiscal_year"], event["fiscal_quarter"]) == (
        "2025-01-30",
        2025,
        1,
    )
    assert event["time_precision"] == "date_only" and event["announced_at"] is None
    assert (
        event["announcement_date_evidence"][0]["date_basis"]
        == "issuer_statement_linking_release_and_exhibit"
    )


def test_long_dateline_abbreviation_full_year_and_dated_exhibit_fiscal_label():
    target = {**document_target(), "filing_date": "2024-02-13"}
    main = "Attached as Exhibit 99.1 is a copy of a press release of Example, dated February 13, 2024, reporting financial results for the fourth quarter and full year 2023."
    release = (
        "<h1>Example Reports Fourth Quarter and Full-Year 2023 Results</h1>"
        + "Revenue details. " * 60
        + "<p>ATLANTA, Feb. 13, 2024 – The company delivered strong financial performance.</p>"
    )
    result = fetch_evidence(target, lambda url: submission(main, release))
    item = result["releases"][0]
    assert item["announced_date"] == "2024-02-13" and item["fiscal_year"] == 2023
    assert item["fiscal_quarter"] == 4 and item["time_precision"] == "date_only"


def test_primary_statement_and_typo_dateline_remain_conflicting_observations():
    target = {**document_target(), "filing_date": "2021-02-10"}
    main = "Attached as Exhibit 99.1 is a copy of a press release of Example, dated February 10, 2021, reporting financial results for the fourth quarter and full year 2020."
    release = "<h1>Example Reports Fourth Quarter and Full Year 2020 Results</h1><p>ATLANTA, Feb. 10, 2020 – The company today reported its results.</p>"
    item = fetch_evidence(target, lambda url: submission(main, release))["releases"][0]
    assert item["announced_date"] == "2021-02-10"
    assert item["time_precision"] == "conflict"
    assert item["date_observations"] == ["2020-02-10"]


def test_financial_table_actual_quarter_end_supports_year_with_verified_fiscal_end():
    release = (
        "<h1>Example Reports Strong Results in Third Quarter</h1><p>ATLANTA, Oct. 18, 2019 – Financial results follow.</p>"
        + "Revenue detail. " * 220
        + "<h2>Operating Review – Three Months Ended September 27, 2019</h2>"
    )
    item = extract_release(
        release.encode(),
        filed_date="2019-10-18",
        source_url="https://example.com/release",
        fiscal_year_end="12-31",
    )
    assert item["fiscal_year"] == 2019 and item["fiscal_quarter"] == 3
    assert item["fiscal_rule"] == "stated_quarter_period_end_and_verified_fiscal_end"


def test_reparse_rejection_retires_old_automatic_guidance_event_but_keeps_evidence():
    with session() as s, s.begin():
        security, _, _ = setup(s)
        item = event(s, security, "2025-01-02", 2025, 1)
        digest = source(s, {"synthetic_original": "guidance letter"})
        url = "https://www.sec.gov/Archives/edgar/data/123/release.htm"
        item.evidence = [{"provider": "SEC release", "source_url": url, "source_hash": digest}]
        response = {
            "releases": [],
            "rejected_releases": [{"source_url": url, "reason": "not_an_actual_earnings_release"}],
            "complete": False,
        }
        job = SimpleNamespace(id=None, target={"security_id": security.id}, checkpoint={})
        persist_evidence(s, job, response, {url: digest}, digest)
        assert item.status == "EXCLUDED" and not item.verified
        assert item.evidence[-1]["rejected"] and len(item.evidence) == 2
        revision = item.revision
        persist_evidence(s, job, response, {url: digest}, digest)
        assert item.revision == revision


@pytest.mark.parametrize("kind", ["listing", "document"])
def test_cached_operation_replays_originals_without_network_and_rejects_tampering(
    kind, monkeypatch, tmp_path
):
    import iirp.operations as operations
    from iirp.business_worker import prepare_target
    from iirp.earnings_data import EARNINGS_PARSER_VERSION
    from iirp.storage import save_object

    monkeypatch.setattr(settings(), "runtime_dir", tmp_path)
    target = document_target()
    if kind == "document":
        payload = submission(
            "On January 30, 2025, Example issued a press release regarding its financial results, attached as Exhibit 99.1.",
            "<h1>Example reports first quarter fiscal 2025 results</h1>",
        )
        url = target["submission_url"]
    else:
        target.pop("submission_url")
        payload = json.dumps(
            {
                "cik": 123,
                "tickers": ["TEST"],
                "fiscalYearEnd": "1231",
                "filings": {
                    "recent": {"form": [], "filingDate": [], "accessionNumber": []},
                    "files": [],
                },
            }
        ).encode()
        url = "https://data.sec.gov/submissions/CIK0000000123.json"
    cached = {
        "parser_version": "earnings-evidence-v2",
        "source_documents": [{"url": url, "payload": payload.decode(), "encoding": "utf8"}],
    }
    stored = save_object(json.dumps(cached).encode())
    job = SimpleNamespace(
        kind="earnings_evidence", target=target, result={"source_hash": stored["sha256"]}
    )
    prepared = prepare_target(job)
    assert prepared["cached_source_hash"] == stored["sha256"]

    def no_network(url):
        raise AssertionError("Cached parsing must never request a provider")

    monkeypatch.setattr(operations, "fetch_sec", no_network)
    response = operations.operation("earnings_evidence", prepared)
    assert response["parser_version"] == EARNINGS_PARSER_VERSION
    assert response["kind"] == kind
    if kind == "document":
        assert response["releases"][0]["announced_date"] == "2025-01-30"
    (tmp_path / stored["relative_path"]).write_bytes(b"synthetic tampered object")
    with pytest.raises(ValueError, match="哈希"):
        operations.operation("earnings_evidence", prepared)


def test_known_non_earnings_guidance_document_is_examined_not_a_missing_source():
    main = "On January 30, 2025, Example issued a press release regarding financial guidance, attached as Exhibit 99.1."
    body = "<h1>A letter from the CEO: We will report first quarter results later this month</h1><p>January 30, 2025</p>"
    result = fetch_evidence(document_target(), lambda url: submission(main, body))
    assert result["classification"] == "NO_EARNINGS_RELEASE"
    assert result["complete"] and result["reason"] is None and not result["releases"]
    assert result["rejected_releases"][0]["reason"] == "not_an_actual_earnings_release"


def test_manual_resolution_closes_current_gap_without_erasing_past_observation():
    with session() as s, s.begin():
        security, batch, scope = setup(s)
        events = [
            event(
                s,
                security,
                f"2024-{month:02d}-01",
                2024,
                quarter,
                "date_only" if quarter == 1 else "after_close",
            )
            for quarter, month in enumerate((2, 5, 8, 11), 1)
        ]
        plan_earnings_discovery(s, scope, batch, [2])
        for job in s.scalars(select(Job)):
            if job.kind == "earnings_candidates":
                finish_candidates(s, job)
            else:
                job.checkpoint = {
                    "earnings_evidence": {
                        "complete": True,
                        "kind": "document",
                        "followups": [],
                        "gaps": ["fiscal_period_or_precise_time_needs_review"],
                    }
                }
                job.status = "SUCCEEDED"
        before = plan_earnings_discovery(s, scope, batch, [0])
        assert before["partial"] and before["coverage"]["source_reasons"] == []
        first = events[0]
        correct_event(
            s,
            first.id,
            {
                "revision": first.revision,
                "source_url": "https://example.com/synthetic",
                "note": "Synthetic authoritative time review",
                    "time_evidence": "Synthetic issuer statement: results actually released after market close",
                "announced_date": str(first.announced_date),
                "time_precision": "after_close",
                "fiscal_year": 2024,
                "fiscal_quarter": 1,
            },
        )
        after = plan_earnings_discovery(s, scope, batch, [0])
        assert not after["partial"] and not after["discovery_pending"]
        assert after["coverage"]["past_observation_gaps"] == [
            "fiscal_period_or_precise_time_needs_review"
        ]
        assert after["coverage"]["source_reasons"] == []


def test_current_day_before_open_event_keeps_pre_event_prices_and_frozen_cutoff(monkeypatch):
    clock = datetime(2025, 1, 31, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(earnings_planner, "now", lambda: clock)
    with session() as s, s.begin():
        security, batch, scope = setup(
            s,
            created=clock,
            params={
                "analysis_params": {
                    "kind": "earnings",
                    "historical_years": 1,
                    "cutoff_date": "2025-01-30",
                }
            },
        )
        event(s, security, "2025-01-31", 2024, 4, "before_open")
        result = plan_earnings_discovery(s, scope, batch, [0])
        assert result["price_ready"]
        assert scope.end_date == date(2025, 1, 30)
        assert not any(
            window["mature"] for window in result["coverage"]["maturity"][0]["windows"].values()
        )

def test_actual_release_replaces_a_previous_estimated_date():
    with session() as s, s.begin():
        security, _, _ = setup(s)
        item = event(s, security, "2025-01-30", 2024, 4)
        item.verified = False
        item.is_estimate = True
        s.flush()
        saved = save_release(s, security, release(), "actual release after candidate")
        assert saved.verified and saved.announced_at is None
        assert not saved.is_estimate


def test_call_session_is_not_results_publication_session():
    html = b"<h1>Example Reports Fiscal 2025 Third Quarter Results</h1><p>July 30, 2025 Example announced it will host an earnings call before the market open.</p>"
    item = extract_release(html, filed_date="2025-07-31", source_url="https://example.com/release")
    assert item["announced_date"] == "2025-07-30"
    assert item["time_precision"] == "date_only"
    assert item["time_evidence"] is None


def test_explicit_results_publication_session_has_source_excerpt():
    html = b"<h1>Example Reports Fiscal 2025 Third Quarter Results</h1><p>July 30, 2025 Example released its earnings results after the market close.</p>"
    item = extract_release(html, filed_date="2025-07-31", source_url="https://example.com/release")
    assert item["time_precision"] == "after_close"
    assert "released its earnings results after the market close" in item["time_evidence"]
