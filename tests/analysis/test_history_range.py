"""Public API regressions for complete natural years and frozen execution bounds.

All commands operate on one disposable iirp_v1_test_r9_history_* database.
No worker, scheduler, provider, fixture service, or real market data is used.
The production exchange calendar remains real; only lifecycle's as-of clock is
fixed. Expectations below are explicit dates, not calls to the new range helper.
"""

import json
import os
import uuid
from datetime import date, datetime, timezone

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from iirp.config import ROOT, settings
from iirp.db import engine, session
from iirp.jobs import batches
from iirp.jobs.queue import ensure_defaults
from iirp.models import Base, Batch, CollectionStrategy, RequestScope
from psycopg import sql
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from tests.clock import set_clock
from tests.zh import zh


@pytest.fixture(scope="module")
def draft_database():
    original = os.environ.get("IIRP_DATABASE_URL")
    source = make_url(settings().database_url)
    name = "iirp_v1_test_r9_history_" + uuid.uuid4().hex[:12]
    assert name.startswith("iirp_v1_test_r9_history_")
    admin = psycopg.connect(
        host=source.host,
        port=source.port,
        user=source.username,
        password=source.password,
        dbname="postgres",
        autocommit=True,
    )
    created = False
    isolated_engine = None
    try:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created = True
        os.environ["IIRP_DATABASE_URL"] = source.set(database=name).render_as_string(
            hide_password=False
        )
        settings.cache_clear()
        engine.cache_clear()
        isolated_engine = engine()
        assert isolated_engine.url.database == name
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        yield name
    finally:
        if isolated_engine is not None:
            isolated_engine.dispose()
        engine.cache_clear()
        if original is None:
            os.environ.pop("IIRP_DATABASE_URL", None)
        else:
            os.environ["IIRP_DATABASE_URL"] = original
        settings.cache_clear()
        try:
            if created:
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
                assert admin.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)", (name,)
                ).fetchone() == (False,)
        finally:
            admin.close()


@pytest.fixture
def fixed_clock(monkeypatch):
    def at(iso):
        stamp = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        assert stamp.tzinfo is not None
        set_clock(monkeypatch, lambda: stamp)
        return stamp

    at("2026-09-12T16:00:00Z")
    return at


@pytest.fixture
def api(draft_database, fixed_clock, tmp_path, monkeypatch):
    assert engine().url.database == draft_database
    with engine().begin() as connection:
        connection.execute(
            text(
                "TRUNCATE "
                + ",".join('"' + table.name + '"' for table in Base.metadata.sorted_tables)
                + " CASCADE"
            )
        )
    ensure_defaults()
    with session() as s, s.begin():
        batches.defaults(s)
        for strategy in s.scalars(select(CollectionStrategy)):
            strategy.enabled = False
    monkeypatch.setattr(settings(), "runtime_dir", tmp_path)
    from iirp.api.app import app
    from iirp.jobs import providers
    from iirp.market import yahoo

    def forbidden(*args, **kwargs):
        raise AssertionError("Public planning/read/controls must not run a provider")

    monkeypatch.setattr(yahoo, "fetch_market", forbidden)
    monkeypatch.setattr(providers, "market_probe", forbidden)
    monkeypatch.setattr(providers, "sec_probe", forbidden)
    with TestClient(app, headers={"X-IIRP-Client": "web"}) as client:
        yield client


def collection_body(**changes):
    return {
        "request_id": "r9-history-" + uuid.uuid4().hex,
        "kind": "market_history",
        "tickers": ["R9SYNTH"],
        **changes,
    }


def monthly_body(**changes):
    return {
        "request_id": "r9-monthly-" + uuid.uuid4().hex,
        "kind": "monthly",
        "tickers": ["R9SYNTH"],
        **changes,
    }


def response_json(response, status):
    assert response.status_code == status, response.text
    return response.json()


def frozen_range(document):
    value = document["batch"]["params"]["price_range"]
    assert document["batch"]["price_range"] == value
    assert value["basis"]
    assert value["explanation"].strip()
    return value


def assert_dates(value, *, start, end, collection_start, collection_end, completed):
    expected = {
        "target_start_date": start,
        "target_end_date": end,
        "collection_start_date": collection_start,
        "collection_end_date": collection_end,
        "completed_through": completed,
    }
    assert {key: value[key] for key in expected} == expected


@pytest.mark.parametrize("years,start", [(3, "2023-01-01"), (8, "2018-01-01"), (12, "2014-01-01")])
def test_default_history_is_n_complete_years_plus_query_day(api, years, start):
    created = response_json(
        api.post("/api/v1/collections", json=collection_body(historical_years=years)), 202
    )
    value = frozen_range(created)
    assert_dates(
        value,
        start=start,
        end="2026-09-12",
        collection_start=start,
        collection_end="2026-09-11",
        completed="2026-09-11",
    )
    assert value["basis"] == "complete_natural_years"
    assert not value.get("buffer_explanation")
    scope = created["batch"]["items"][0]
    assert scope["coverage"]["start_date"] == start
    assert scope["coverage"]["end_date"] == "2026-09-11"
    assert scope["coverage"]["valid_sessions"] == 0
    assert scope["coverage"]["status"] != "READY"
    assert scope["jobs_total"] == 0
    # The returned value is persisted; reconnecting must not recalculate it.
    engine().dispose()
    saved = response_json(api.get(f"/api/v1/batches/{created['batch_id']}"), 200)
    assert frozen_range(saved) == value


def test_omitted_history_uses_server_preference_and_coverage_matches(api):
    response_json(api.patch("/api/v1/preferences", json={"historical_years": 12}), 200)
    created = response_json(api.post("/api/v1/collections", json=collection_body()), 202)
    value = frozen_range(created)
    assert value["historical_years"] == 12
    assert value["target_start_date"] == "2014-01-01"
    coverage = response_json(api.get("/api/v1/coverage", params={"ticker": "R9SYNTH"}), 200)
    item = next(item for item in coverage["items"] if item["symbol"] == "R9SYNTH")
    assert item["price_range"] == value
    assert item["coverage"]["start_date"] == "2014-01-01"
    assert item["coverage"]["end_date"] == "2026-09-11"


@pytest.mark.parametrize(
    "stamp,start,end",
    [
        ("2026-01-01T01:00:00Z", "2017-01-01", "2025-12-31"),
        ("2026-01-01T06:00:00Z", "2018-01-01", "2026-01-01"),
    ],
)
def test_year_rolls_at_new_york_midnight_not_utc(api, fixed_clock, stamp, start, end):
    fixed_clock(stamp)
    created = response_json(
        api.post("/api/v1/collections", json=collection_body(historical_years=8)), 202
    )
    assert_dates(
        frozen_range(created),
        start=start,
        end=end,
        collection_start=start,
        collection_end="2025-12-31",
        completed="2025-12-31",
    )


def test_explicit_dates_are_preserved(api):
    created = response_json(
        api.post(
            "/api/v1/collections",
            json=collection_body(
                historical_years=12, start_date="2020-03-15", end_date="2021-05-20"
            ),
        ),
        202,
    )
    value = frozen_range(created)
    assert_dates(
        value,
        start="2020-03-15",
        end="2021-05-20",
        collection_start="2020-03-15",
        collection_end="2021-05-20",
        completed="2026-09-11",
    )
    assert value["basis"] == "explicit_dates"


def test_monthly_retains_previous_close_as_separate_buffer(api):
    created = response_json(
        api.post("/api/v1/analyses", json=monthly_body(historical_years=8, current_year=2026)), 202
    )
    value = frozen_range(created)
    assert_dates(
        value,
        start="2018-01-01",
        end="2026-09-12",
        collection_start="2017-12-29",
        collection_end="2026-09-11",
        completed="2026-09-11",
    )
    assert value["basis"] == "research_conditions"
    assert value["buffer_explanation"].strip()
    assert created["batch"]["params"]["start_date"] == "2017-12-29"
    assert created["params"]["cutoff_date"] == "2026-09-11"


def test_selected_research_years_override_default_n_year_envelope(api):
    created = response_json(
        api.post(
            "/api/v1/analyses",
            json=monthly_body(
                historical_years=8,
                current_year=2024,
                years=[2016, 2019, 2021],
                excluded_years=[2016],
            ),
        ),
        202,
    )
    value = frozen_range(created)
    assert_dates(
        value,
        start="2019-01-01",
        end="2024-12-31",
        collection_start="2018-12-31",
        collection_end="2024-12-31",
        completed="2026-09-11",
    )


def test_preview_uses_analysis_year_count_and_matches_submitted_batch(api):
    body = monthly_body(historical_years=3, current_year=2026)
    preview = response_json(
        api.get("/api/v1/price-range", params={"parameters": json.dumps({"analysis": body})}), 200
    )
    assert preview["historical_years"] == 3
    assert_dates(
        preview,
        start="2023-01-01",
        end="2026-09-12",
        collection_start="2022-12-30",
        collection_end="2026-09-11",
        completed="2026-09-11",
    )
    # GET planning must not create any batch, including a hidden preparation.
    assert response_json(api.get("/api/v1/batches"), 200)["items"] == []
    created = response_json(api.post("/api/v1/analyses", json=body), 202)
    assert frozen_range(created) == preview


def test_interval_preview_freezes_anchor_from_query_day_after_window_end(api, fixed_clock):
    # 09:00 ET on Jan 21: Jan 20 has completed but today's date is already
    # outside a Dec 15 -> Jan 20 window. Current anchor must be 2026 in both
    # target and collection planning, even though the collection cutoff is Jan 20.
    fixed_clock("2026-01-21T14:00:00Z")
    body = monthly_body(kind="interval", historical_years=3, start_mmdd="12-15", end_mmdd="01-20")
    preview = response_json(
        api.get(
            "/api/v1/price-range",
            params={"parameters": json.dumps({"historical_years": 3, "analysis": body})},
        ),
        200,
    )
    assert_dates(
        preview,
        start="2023-12-15",
        end="2026-01-21",
        collection_start="2023-12-15",
        collection_end="2026-01-20",
        completed="2026-01-20",
    )
    assert not preview.get("buffer_explanation")
    created = response_json(api.post("/api/v1/analyses", json=body), 202)
    assert created["params"]["current_year"] == 2026
    assert frozen_range(created) == preview


@pytest.mark.parametrize("kind", ["collection", "monthly"])
def test_request_replay_and_get_keep_frozen_range_after_year_change(api, fixed_clock, kind):
    path = "/api/v1/collections" if kind == "collection" else "/api/v1/analyses"
    body = (
        collection_body(historical_years=8)
        if kind == "collection"
        else monthly_body(historical_years=8)
    )
    original = response_json(api.post(path, json=body), 202)
    value = frozen_range(original)
    fixed_clock("2027-01-02T16:00:00Z")
    # A subsequent preference change also must not reinterpret the old request.
    response_json(api.patch("/api/v1/preferences", json={"historical_years": 3}), 200)
    replay = response_json(api.post(path, json=body), 202)
    assert replay["batch_id"] == original["batch_id"]
    assert frozen_range(replay) == value
    saved = response_json(api.get(f"/api/v1/batches/{original['batch_id']}"), 200)
    assert frozen_range(saved) == value
    response_json(api.post(path, json={**body, "historical_years": 12}), 409)
    fresh = response_json(api.post(path, json={**body, "request_id": uuid.uuid4().hex}), 202)
    assert frozen_range(fresh)["target_start_date"] == "2019-01-01"
    assert frozen_range(fresh)["target_end_date"] == "2027-01-02"


@pytest.mark.parametrize("kind", ["collection", "monthly"])
def test_cancel_continue_preserves_original_target_and_buffer_across_year_change(
    api, fixed_clock, kind
):
    path = "/api/v1/collections" if kind == "collection" else "/api/v1/analyses"
    body = (
        collection_body(historical_years=8)
        if kind == "collection"
        else monthly_body(historical_years=8)
    )
    original = response_json(api.post(path, json=body), 202)
    identifier = original["batch_id"]
    value = frozen_range(original)
    cancelled = response_json(
        api.post(f"/api/v1/batches/{identifier}/actions", json={"action": "cancel"}), 200
    )
    assert cancelled["batch"]["status"] == "CANCELLED"
    fixed_clock("2027-01-02T16:00:00Z")
    child = response_json(
        api.post(f"/api/v1/batches/{identifier}/actions", json={"action": "continue_remaining"}),
        200,
    )
    assert child["batch_id"] != identifier
    assert child["batch"]["parent_id"] == identifier
    assert frozen_range(child) == value
    assert child["batch"]["items"][0]["coverage"] == original["batch"]["items"][0]["coverage"]
    again = response_json(
        api.post(f"/api/v1/batches/{identifier}/actions", json={"action": "continue_remaining"}),
        200,
    )
    assert again["batch_id"] == child["batch_id"]
    assert again["reused"] is True
    assert frozen_range(again) == value
    saved = response_json(api.get(f"/api/v1/batches/{identifier}"), 200)
    assert saved["batch"]["status"] == "CANCELLED"
    assert frozen_range(saved) == value
    if kind == "monthly":
        analysis_id = child["batch"]["analysis_id"]
        assert analysis_id and analysis_id != original["id"]
        child_analysis = response_json(api.get(f"/api/v1/analyses/{analysis_id}"), 200)
        assert child_analysis["params"] == original["params"]


def test_legacy_continue_uses_saved_scope_without_inventing_old_user_target(api, fixed_clock):
    original = response_json(
        api.post("/api/v1/collections", json=collection_body(historical_years=8)), 202
    )
    identifier = original["batch_id"]
    # Sole deliberate direct fixture edit: represent an old persisted record,
    # which lacks the new metadata and used the old December download envelope.
    with session() as s, s.begin():
        old = s.get(Batch, identifier)
        old.params = {key: value for key, value in old.params.items() if key != "price_range"}
        old.created_at = datetime(2026, 9, 12, 16, tzinfo=timezone.utc)
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == identifier))
        scope.start_date, scope.end_date = date(2017, 12, 1), date(2026, 9, 11)
    response_json(api.post(f"/api/v1/batches/{identifier}/actions", json={"action": "cancel"}), 200)
    fixed_clock("2027-01-02T16:00:00Z")
    child = response_json(
        api.post(f"/api/v1/batches/{identifier}/actions", json={"action": "continue_remaining"}),
        200,
    )
    value = frozen_range(child)
    assert value["collection_start_date"] == "2017-12-01"
    assert value["collection_end_date"] == "2026-09-11"
    assert value["basis"] == "legacy_scope"
    assert "旧" in zh(value["explanation"])
    assert child["batch"]["items"][0]["coverage"]["start_date"] == "2017-12-01"
    assert child["batch"]["items"][0]["coverage"]["end_date"] == "2026-09-11"


def test_coverage_target_does_not_shrink_to_data_or_confuse_publication_with_price_date(api):
    from tests.jobs.test_lifecycle import seed_prices, seed_security

    security = seed_security("R9SYNTH")
    seed_prices(security, date(2020, 1, 2), date(2020, 1, 6))
    value = response_json(api.get("/api/v1/coverage", params={"ticker": "R9SYNTH"}), 200)["items"][
        0
    ]
    assert value["price_range"]["target_start_date"] == "2018-01-01"
    assert value["price_range"]["target_end_date"] == "2026-09-12"
    coverage = value["coverage"]
    assert coverage["start_date"] == "2018-01-01" and coverage["end_date"] == "2026-09-11"
    assert coverage["first_valid_date"] == "2020-01-02"
    assert coverage["last_valid_date"] == "2020-01-06"
    assert coverage["valid_sessions"] == 3 and "2018-01-02" in coverage["missing_dates"]
    assert coverage["as_of"][:10] != coverage["last_valid_date"]


def test_read_only_preview_invalid_and_explicit_inputs_create_no_tasks(api):
    response_json(
        api.get(
            "/api/v1/price-range", params={"parameters": json.dumps({"start_date": "2020-01-01"})}
        ),
        422,
    )
    response_json(api.post("/api/v1/price-range", json={"historical_years": 0}), 422)
    value = response_json(
        api.post(
            "/api/v1/price-range", json={"start_date": "2020-01-01", "end_date": "2021-01-01"}
        ),
        200,
    )
    assert value["target_start_date"] == "2020-01-01" and value["target_end_date"] == "2021-01-01"
    assert response_json(api.get("/api/v1/batches"), 200)["items"] == []


def test_explicit_weekend_target_preview_matches_saved_scope_without_inventing_a_close(api):
    values = {"historical_years": 8, "start_date": "2026-09-01", "end_date": "2026-09-12"}
    preview = response_json(api.post("/api/v1/price-range", json=values), 200)
    created = response_json(api.post("/api/v1/collections", json=collection_body(**values)), 202)
    assert frozen_range(created) == preview
    assert preview["target_end_date"] == preview["collection_end_date"] == "2026-09-12"
    assert preview["completed_through"] == "2026-09-11"
    assert created["batch"]["items"][0]["coverage"]["last_valid_date"] is None
