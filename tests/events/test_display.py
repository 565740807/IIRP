"""S6c display projections: hand calculations, cache-only filtering and title repair."""

import copy
import importlib.util
from datetime import date
from decimal import Decimal

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from iirp.analysis.distributions import statistics
from iirp.analysis.event_windows import analyze_events, project_result
from iirp.api.app import app
from iirp.config import ROOT
from iirp.db import engine, session
from iirp.events import service
from iirp.messages import msg
from iirp.models import AnalysisRequest, Batch, EventSet
from sqlalchemy import select

from tests.events.test_ai_json_events import BARS, HEADERS, earnings, text
from tests.jobs.test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401


def test_boxplot_hand_quartiles_whiskers_and_outliers():
    result = statistics(["-.30", "-.20", "0", ".10", ".20", "1.00", None])
    assert result["n"] == 6 and Decimal(result["up_ratio"]) == Decimal(".5")
    for key, value in {"q25": "-.15", "median": ".05", "q75": ".175",
                       "whisker_low": "-.3", "whisker_high": ".2"}.items():
        assert Decimal(result[key]) == Decimal(value)
    assert list(map(Decimal, result["outliers"])) == [Decimal("1")]
    assert statistics(["2", "2", "2", "2", "-10", "20"])["outliers"] == ["-10", "20"]
    single = statistics([".25"])
    assert Decimal(single["whisker_low"]) == Decimal(single["whisker_high"]) == Decimal(".25")
    assert single["outliers"] == []
    empty = statistics([])
    assert empty["whisker_low"] is None and empty["outliers"] == []


def sample():
    result = analyze_events([earnings(date="2024-06-10", session="during")], BARS, n=1,
                            cutoff=date(2024, 6, 12), kind="earnings")
    template = result["rows"][0]
    rows = []
    for year, quarter, value in zip(range(2019, 2025), [2, 2, 1, 4, 2, 2],
                                    ["-.3", "-.2", "0", ".1", ".2", "1"], strict=True):
        row = copy.deepcopy(template)
        row.update(date=f"{year}-06-10", fiscal_year=year, fiscal_quarter=quarter, name=str(year),
                   session="after_close" if year % 2 else "before_open")
        for window in row["windows"].values():
            window.update(value=value, benchmark=".05", excess=str(Decimal(value) - Decimal(".05")))
        for point in row["path"]:
            point.update(value=value, benchmark=".05")
        rows.append(row)
    return {**result, "rows": rows}


def test_filtered_subsets_recompute_all_displays_and_keep_frozen_rows():
    source = sample()
    original = copy.deepcopy(source)
    result = project_result(source, quarter=2)
    stats = result["summary"]["reaction"]
    assert (stats["n"], stats["up"], result["event_count"]) == (4, 2, 4)
    assert Decimal(stats["median"]) == 0 and Decimal(stats["mean"]) == Decimal(".175")
    assert Decimal(stats["q25"]) == Decimal("-.225") and Decimal(stats["q75"]) == Decimal(".4")
    assert Decimal(stats["benchmark_box"]["median"]) == Decimal(".05")
    assert result["path"][0]["median"] == stats["median"]
    assert [q["fiscal_quarter"] for q in result["quarters"]] == [2]
    assert result["rows"][-1]["ranks"]["reaction_desc"] == 1
    assert result["rows"][0]["ranks"]["reaction_asc"] == 1
    assert all(Decimal(s["mean"]) == Decimal(".175") for s in result["summary"].values())
    assert source == original
    combined = project_result(source, quarter=2, recent_years=2, session="before_open", direction="up")
    assert combined["event_count"] == 1 and combined["rows"][0]["date"] == "2024-06-10"
    assert Decimal(combined["summary"]["reaction"]["median"]) == 1
    assert Decimal(project_result(source, direction="down")["summary"]["reaction"]["mean"]) == Decimal("-.25")
    assert project_result(source, direction="up")["event_count"] == 3
    empty = project_result(source, quarter=3)
    assert empty["event_count"] == 0 and empty["quarters"] == []
    assert empty["summary"]["reaction"]["n"] == 0 and empty["path"][0]["median"] is None


def test_saved_names_follow_language_and_only_legacy_titles_are_migrated():
    client = TestClient(app)
    for language, expected in (("en", "earnings"), ("zh", "财报")):
        response = client.post("/api/v1/events/sets", headers=HEADERS,
                               json={"kind": "earnings", "text": text(earnings()), "language": language})
        assert response.status_code == 201
        name = response.json()["set"]["title"]
        assert expected in name and "2024" in name and not name.startswith("{")
    legacy = msg("events.set_title.earnings", tickers=["TSLA"], first="2019", last="2026")
    created = service.create_set({"kind": "earnings", "text": text(earnings()), "title": legacy,
                                  "analyze": True, "benchmark": None})
    custom = service.create_set({"kind": "earnings", "text": text(earnings()), "title": "My saved title"})
    unknown = service.create_set({"kind": "earnings", "text": text(earnings()),
                                  "title": '{"code":"other.title","params":{}}'})
    migration_spec = importlib.util.spec_from_file_location("title_migration", ROOT / "migrations/versions/0002_event_titles.py")
    migration = importlib.util.module_from_spec(migration_spec)
    migration_spec.loader.exec_module(migration)
    with session() as s:
        request = s.get(AnalysisRequest, created["analysis"]["analysis_id"])
        frozen = copy.deepcopy(request.params)
        batch_id = request.batch_id
    with engine().begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
        migration.upgrade()  # Idempotent; no existing editable name is regenerated.
    with session() as s:
        saved = s.get(EventSet, created["set"]["id"])
        assert saved.title == "TSLA · earnings 2019–2026"
        assert saved.events == frozen["events"]
        assert s.get(EventSet, custom["set"]["id"]).title == "My saved title"
        assert s.get(EventSet, unknown["set"]["id"]).title == '{"code":"other.title","params":{}}'
        updated = s.get(AnalysisRequest, created["analysis"]["analysis_id"]).params
        assert updated == {**frozen, "title": saved.title}
        assert s.scalar(select(Batch.title).where(Batch.id == batch_id)) == saved.title
