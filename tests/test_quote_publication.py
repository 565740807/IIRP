"""Out-of-order source observations cannot replace a newer visible snapshot."""
from iirp.quote_publication import merge_quote


def test_older_or_unpaired_quote_preserves_value_timestamp_and_baseline():
    old = {"value": 120, "quote_kind": "provider_snapshot", "source_time": "2026-09-16T15:00:00+00:00",
           "as_of": "2026-09-16", "baseline_date": "2026-09-15", "previous_close": 110}
    for timestamp in ["2026-09-16T14:00:00+00:00", None]:
        merged, retained = merge_quote(old, {"value": 100, "source_time": timestamp, "as_of": "2026-09-16",
                                             "fetched_at": "2026-09-16T15:01:00+00:00"})
        assert retained
        assert (merged["value"], merged["source_time"], merged["previous_close"]) == (120, old["source_time"], 110)
        assert merged["last_checked_at"] == "2026-09-16T15:01:00+00:00"
        assert merged["refresh_notice"]


def test_new_source_time_and_next_day_fallback_can_advance():
    old = {"value": 120, "quote_kind": "provider_snapshot", "source_time": "2026-09-16T15:00:00+00:00", "as_of": "2026-09-16"}
    for incoming in [dict(old, value=125, source_time="2026-09-16T15:01:00+00:00"),
                     {"value": 130, "quote_kind": "daily_fallback", "as_of": "2026-09-17", "source_time": None}]:
        value, retained = merge_quote(old, incoming)
        assert not retained and value["value"] == incoming["value"]


def test_legacy_daily_metadata_time_does_not_prevent_correct_snapshot():
    legacy = {"value": 100, "status": "DAILY", "source_time": "2026-09-16T15:00:00+00:00", "as_of": "2026-09-15"}
    current = {"value": 110, "quote_kind": "provider_snapshot", "source_time": "2026-09-16T14:59:00+00:00", "as_of": "2026-09-16"}
    assert merge_quote(legacy, current)[0]["value"] == 110


def test_fenced_quote_publication_keeps_newer_quote_and_never_writes_research_prices():
    from iirp.business_models import MarketBar, MarketQuote, PriceDataset
    from iirp.business_worker import _persist
    from iirp.db import session
    from iirp.models import Job
    from iirp.queue import fenced
    from iirp.storage import save_object
    from sqlalchemy import func, select
    from test_planning_recovery import pending_quote

    _, job = pending_quote()
    with session() as s, s.begin():
        s.add(MarketQuote(symbol="^VIX", data={"value": 120, "quote_kind": "provider_snapshot",
            "source_time": "2026-09-16T15:00:00+00:00", "as_of": "2026-09-16", "previous_close": "110"}))
    response = {"fetched_at": "2026-09-16T15:01:00+00:00", "metadata": {
        "regularMarketPrice": 100, "regularMarketTime": "2026-09-16T14:00:00+00:00"}, "records": []}
    source = save_object(b"synthetic isolated stale quote")
    assert fenced(job, source=source, business_write=lambda s, current: _persist(s, current, response, {}, source))
    with session() as s:
        assert s.get(MarketQuote, "^VIX").data["value"] == 120
        assert s.get(Job, job.id).status == "PARTIAL"
        assert s.scalar(select(func.count()).select_from(PriceDataset)) == 0
        assert s.scalar(select(func.count()).select_from(MarketBar)) == 0


from test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401, E402
