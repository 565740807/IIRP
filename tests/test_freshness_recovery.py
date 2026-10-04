"""Freshness recovery uses a disposable iirp_v1_test_* database only."""
from datetime import timedelta

from iirp import lifecycle
from iirp.business_models import Batch, CollectionStrategy, RequestScope
from iirp.db import session
from iirp.freshness import ensure_fresh
from iirp.models import Job, now
from sqlalchemy import func, select
from test_lifecycle import clean_lifecycle, lifecycle_database, seed_security  # noqa: F401


def finish_market_children(batch_id):
    lifecycle.plan_tick()
    with session() as s, s.begin():
        batch = s.get(Batch, batch_id)
        batch.created_at = now() - timedelta(minutes=5)
        for scope in s.scalars(select(RequestScope).where(RequestScope.batch_id == batch_id)):
            for job in lifecycle.linked_jobs(s, scope.id):
                job.status, job.finished_at = "SUCCEEDED", now() - timedelta(minutes=2)
        policy = s.get(CollectionStrategy, "market")
        policy.options = {**policy.options, "latest_requested_at": batch.created_at.isoformat()}


def test_open_reconciles_completed_parent_and_starts_due_check():
    from iirp.market_data import MARKETS
    for symbol in MARKETS:
        seed_security(symbol)
    first = ensure_fresh({"reason": "open", "sources": ["market"]})
    old = first["batch_ids"][0]
    finish_market_children(old)
    second = ensure_fresh({"reason": "open", "sources": ["market"]})
    assert second["batch_ids"] != [old]
    assert second["sources"]["market"]["batch_id"] == second["batch_ids"][0]
    with session() as s:
        assert s.get(Batch, old).status == "SUCCEEDED"
        assert s.scalar(select(func.count()).select_from(Job)) == 5


def test_open_shares_live_children_and_pause_remains_explicit():
    from iirp.market_data import MARKETS
    for symbol in MARKETS:
        seed_security(symbol)
    first = ensure_fresh({"reason": "open", "sources": ["market"]})
    lifecycle.plan_tick()
    assert ensure_fresh({"reason": "resume", "sources": ["market"]})["batch_ids"] == first["batch_ids"]
    with session() as s, s.begin():
        policy = s.get(CollectionStrategy, "market")
        policy.enabled = False
        policy.version += 1
    assert ensure_fresh({"reason": "open", "sources": ["market"]})["batch_ids"] == []
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Batch)) == 1


def test_periodic_quotes_require_visible_demand_and_only_refresh_due_instruments():
    from iirp.business_models import MarketQuote
    from iirp.market_data import MARKETS
    assert ensure_fresh({"reason": "scheduler", "sources": ["market"]})["batch_ids"] == []
    with session() as s, s.begin():
        for symbol in MARKETS:
            s.add(MarketQuote(symbol=symbol, data={"next_refresh_at":
                (now() + timedelta(minutes=15) if symbol != "GC=F" else now() - timedelta(minutes=1)).isoformat()}))
    due = ensure_fresh({"reason": "visible", "sources": ["market"], "market_visible": True})
    with session() as s, s.begin():
        assert s.scalars(select(RequestScope.symbol).where(RequestScope.batch_id == due["batch_ids"][0])).all() == ["GC=F"]
        policy = s.get(CollectionStrategy, "market")
        policy.options = {**policy.options, "quote_visible_until": (now() - timedelta(seconds=1)).isoformat()}
    assert ensure_fresh({"reason": "scheduler", "sources": ["market"]})["batch_ids"] == []
    # Closing another tab must not revoke an active tab's lease.
    ensure_fresh({"reason": "resume", "sources": ["market"], "market_visible": True})
    ensure_fresh({"reason": "resume", "sources": ["market"], "market_visible": False})
    with session() as s:
        from datetime import datetime
        assert datetime.fromisoformat(s.get(CollectionStrategy, "market").options["quote_visible_until"]) > now()


def test_latest_completed_manual_batch_does_not_mask_returned_automatic_demand():
    first = ensure_fresh({"reason": "open", "sources": ["market"]})
    with session() as s, s.begin():
        batch, _ = lifecycle._create(s, {"kind": "market_quotes", "request_id": "later-manual", "tickers": ["^VIX"]})
        batch.status = "SUCCEEDED"
    shared = ensure_fresh({"reason": "resume", "sources": ["market"]})
    assert shared["batch_ids"] == first["batch_ids"]
    assert shared["sources"]["market"]["batch_id"] == first["batch_ids"][0]
    assert shared["sources"]["market"]["status"] == "waiting"
