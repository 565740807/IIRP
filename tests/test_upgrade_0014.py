"""Build a real 0014 schema from empty, then upgrade; never stamp a head DB as old."""

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from iirp.config import ROOT, settings
from iirp.db import engine
from psycopg import sql
from sqlalchemy import MetaData, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError


@contextmanager
def empty_database():
    url = make_url(settings().database_url)
    name = "iirp_v1_test_upgrade_" + uuid.uuid4().hex[:12]
    with psycopg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname="postgres",
        autocommit=True,
    ) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        previous = os.environ.get("IIRP_DATABASE_URL")
        os.environ["IIRP_DATABASE_URL"] = url.set(database=name).render_as_string(
            hide_password=False
        )
        engine.cache_clear()
        settings.cache_clear()
        try:
            yield Config(str(ROOT / "alembic.ini"))
        finally:
            engine().dispose()
            engine.cache_clear()
            if previous is None:
                os.environ.pop("IIRP_DATABASE_URL", None)
            else:
                os.environ["IIRP_DATABASE_URL"] = previous
            settings.cache_clear()
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def seed_0014():
    # Reflect *the then-current schema*, using explicit values for its columns.
    metadata = MetaData()
    metadata.reflect(engine(), only=["security", "batch", "analysis_request", "analysis_result"])
    assert "payload" not in metadata.tables["analysis_result"].c
    assert "planning_failures" not in metadata.tables["batch"].c
    at = datetime.now(timezone.utc)
    security, batch, request, result = [str(uuid.uuid4()) for _ in range(4)]
    frozen = {
        "metadata": {"calc_version": "synthetic-0014", "cutoff_date": "2025-01-01"},
        "rows": [],
        "sources": [{"label": "synthetic retained source", "value": None}],
        "known_zero": 0,
        "unknown": None,
    }
    with engine().begin() as conn:
        conn.execute(
            metadata.tables["security"]
            .insert()
            .values(
                id=security,
                symbol="SYNTHETIC",
                name="Synthetic migration fixture",
                instrument="EQUITY",
                currency="USD",
                exchange="NMS",
                calendar="XNYS",
                status="VERIFIED",
                metadata_json={"synthetic": True},
                maintain=False,
            )
        )
        conn.execute(
            metadata.tables["batch"]
            .insert()
            .values(
                id=batch,
                request_id=str(uuid.uuid4()),
                scope_key="a" * 64,
                kind="research",
                title="Synthetic frozen legacy research",
                params={},
                trigger="manual",
                status="PAUSED",
                requested_action="pause",
                control_version=7,
                created_at=at,
                updated_at=at,
            )
        )
        conn.execute(
            metadata.tables["analysis_request"]
            .insert()
            .values(id=request, batch_id=batch, params={"kind": "monthly"}, created_at=at)
        )
        conn.execute(
            metadata.tables["analysis_result"]
            .insert()
            .values(
                id=result,
                analysis_id=request,
                security_id=security,
                input_key="b" * 64,
                inputs={"source_version": "synthetic-0014"},
                data=frozen,
                created_at=at,
                accessed_at=at,
            )
        )
    return result, batch, frozen


@pytest.mark.parametrize("start", ["empty", "0014"])
def test_empty_and_legal_0014_upgrade_to_head(start):
    with empty_database() as config:
        legacy = None
        if start == "0014":
            command.upgrade(config, "0014")
            legacy = seed_0014()
        command.upgrade(config, "head")
        with engine().connect() as conn:
            assert (
                conn.scalar(text("SELECT version_num FROM alembic_version"))
                == ScriptDirectory.from_config(config).get_current_head()
            )
            assert conn.scalar(
                text("SELECT has_database_privilege(current_user,current_database(),'TEMP')")
            )
            if legacy:
                result, batch, frozen = legacy
                row = conn.execute(
                    text(
                        "SELECT data,payload,overlap_projection FROM analysis_result WHERE id=:id"
                    ),
                    {"id": result},
                ).one()
                assert row[0] == frozen and row[1] is None and row[2] is None
                assert conn.execute(
                    text("SELECT status,control_version,planning_failures FROM batch WHERE id=:id"),
                    {"id": batch},
                ).one() == ("PAUSED", 7, 0)
        command.check(config)


def test_0015_lock_failure_is_recoverable_without_stamping():
    with empty_database() as config:
        command.upgrade(config, "0014")
        seed_0014()
        with engine().connect() as blocker:
            transaction = blocker.begin()
            blocker.execute(text("LOCK TABLE batch IN ACCESS EXCLUSIVE MODE"))
            try:
                with pytest.raises(OperationalError):
                    command.upgrade(config, "0015")
            finally:
                transaction.rollback()
        with engine().connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0014"
        assert "planning_failures" not in {
            c["name"] for c in inspect(engine()).get_columns("batch")
        }
        command.upgrade(config, "head")
        command.check(config)
