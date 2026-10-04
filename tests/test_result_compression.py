"""Result storage policy upgrades preserve frozen content and existing TOAST."""

import copy

from alembic import command
from alembic.config import Config
from iirp.business_models import AnalysisResult
from iirp.config import ROOT
from iirp.db import session
from sqlalchemy import text
from test_event_overlap_reads import published
from test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401


def snapshot(identifier):
    with session() as s:
        return s.execute(
            text(
                "SELECT data, pg_column_compression(data), pg_column_size(data), "
                "ctid::text, xmin::text, pg_relation_filenode('analysis_result') "
                "FROM analysis_result WHERE id=:id"
            ),
            {"id": identifier},
        ).one()


def test_compression_migration_preserves_old_rows_and_applies_to_new_writes():
    config = Config(str(ROOT / "alembic.ini"))
    _, _, view, _ = published(13)
    old_id = view["result_id"]
    command.downgrade(config, "0016")
    try:
        # Use raw SQL while the schema is intentionally older than the ORM.
        # Copy the legacy JSONB through text to apply the restored PGLZ policy.
        with session() as s, s.begin():
            s.execute(text("UPDATE analysis_result SET data=(data::text || ' ')::jsonb WHERE id=:id"), {"id": old_id})
        before = snapshot(old_id)
        assert before[1] == "pglz"
        command.upgrade(config, "head")
        assert snapshot(old_id) == before
        with session() as s, s.begin():
            old = s.get(AnalysisResult, old_id)
            new = AnalysisResult(
                analysis_id=old.analysis_id,
                security_id=old.security_id,
                input_key="compression-migration-copy",
                inputs=copy.deepcopy(old.inputs),
                legacy_data=copy.deepcopy(old.data),
            )
            s.add(new)
            s.flush()
            new_id = new.id
        after = snapshot(new_id)
        assert after[0] == before[0] and after[1] == "lz4"
        command.downgrade(config, "0016")
        assert snapshot(old_id) == before
        assert snapshot(new_id) == after
    finally:
        command.upgrade(config, "head")
