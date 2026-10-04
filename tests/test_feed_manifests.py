"""Shared manifests preserve old sessions, immutable order, and foreign-key protection."""

from datetime import timedelta

import pytest
from iirp.business_models import FeedManifest, FeedSession
from iirp.db import session
from iirp.feed_snapshots import freeze_feed_session, session_revision_ids
from iirp.models import now
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from test_sec_facts import clean, isolated_database  # noqa: F401


def test_many_sessions_share_one_manifest_without_copying_ids():
    ids = [f"revision-{index}" for index in range(5000)]
    with session() as s, s.begin():
        saved = [freeze_feed_session(s, ids, {"purpose": "feed", "kind": "all", "as_of": str(index)})
                 for index in range(20)]
        assert len({row.id for row in saved}) == 20
        assert s.scalar(select(func.count()).select_from(FeedManifest)) == 1
        assert all(row.revision_ids == [] and session_revision_ids(s, row) == ids for row in saved)
        assert [row.filters["as_of"] for row in saved] == [str(index) for index in range(20)]
        assert s.scalar(select(func.sum(func.jsonb_array_length(FeedSession.revision_ids)))) == 0


def test_new_revision_and_changed_order_cannot_change_old_pagination():
    with session() as s, s.begin():
        before = freeze_feed_session(s, ["first", "second"], {"purpose": "feed"})
        after = freeze_feed_session(s, ["new", "first", "second"], {"purpose": "feed"})
        other_order = freeze_feed_session(s, ["second", "first"], {"purpose": "feed"})
        assert len({before.manifest_hash, after.manifest_hash, other_order.manifest_hash}) == 3
        assert session_revision_ids(s, before)[1:] == ["second"]
        assert session_revision_ids(s, after)[1:] == ["first", "second"]


def test_legacy_session_keeps_original_inline_revisions():
    with session() as s, s.begin():
        saved = FeedSession(revision_ids=["legacy-first", "legacy-second"], filters={"purpose": "feed"},
                            expires_at=now() + timedelta(hours=12))
        s.add(saved)
        s.flush()
        assert saved.manifest_hash is None
        assert session_revision_ids(s, saved) == ["legacy-first", "legacy-second"]
        assert saved.revision_ids == ["legacy-first", "legacy-second"]


def test_referenced_manifest_cannot_be_deleted():
    with session() as s, s.begin():
        saved = freeze_feed_session(s, ["protected"], {"purpose": "feed"})
        with pytest.raises(IntegrityError), s.begin_nested():
            s.execute(delete(FeedManifest).where(FeedManifest.sha256 == saved.manifest_hash))
        assert session_revision_ids(s, saved) == ["protected"]


def test_schema_downgrade_hydrates_new_sessions_without_changing_their_identity():
    from alembic import command
    from alembic.config import Config
    from iirp.config import ROOT
    from iirp.db import engine
    from sqlalchemy import text

    with session() as s, s.begin():
        compact = freeze_feed_session(s, ["older", "newer"], {"purpose": "feed", "as_of": "frozen"})
        identifier = compact.id
    configuration = Config(str(ROOT / "alembic.ini"))
    try:
        command.downgrade(configuration, "0011")
        with engine().connect() as c:
            row = c.execute(text("SELECT revision_ids,filters FROM feed_session WHERE id=:id"), {"id": identifier}).one()
            assert row.revision_ids == ["older", "newer"] and row.filters["as_of"] == "frozen"
    finally:
        command.upgrade(configuration, "head")
    with session() as s:
        saved = s.get(FeedSession, identifier)
        assert session_revision_ids(s, saved) == ["older", "newer"]



def test_corrupt_manifest_is_explicit_and_never_becomes_latest_state():
    with session() as s, s.begin():
        saved = freeze_feed_session(s, ["original"], {"purpose": "feed"})
        manifest = s.get(FeedManifest, saved.manifest_hash)
        manifest.revision_ids = ["unexpected-other-revision"]
        s.flush()
        with pytest.raises(ValueError, match="校验失败"):
            session_revision_ids(s, saved)
