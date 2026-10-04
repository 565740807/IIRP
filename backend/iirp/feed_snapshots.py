"""Content-addressed immutable feed page indexes, shared across reading sessions."""

import hashlib
import json
from datetime import timedelta

from sqlalchemy import delete, exists, select, text
from sqlalchemy.dialects.postgresql import insert

from iirp.business_models import FeedManifest, FeedSession
from iirp.models import now

MANIFEST_GRACE = timedelta(hours=24)
MANIFEST_CLEANUP_PAGE = 25


def lock_feed_manifests(s, *, publishing=False):
    # Shared publication locks permit parallel readers/publishers. GC skips a
    # busy publication instead of waiting, and holds the exclusive transaction
    # lock until deletion commits. The FK remains the last integrity guard.
    function = "pg_advisory_xact_lock_shared" if publishing else "pg_try_advisory_xact_lock"
    acquired = s.scalar(text(f"SELECT {function}(1229541968,1179471181)"))
    return True if publishing else bool(acquired)


def cleanup_feed_manifests(s, *, cutoff=None):
    """Delete at most one page of old unreferenced derived indexes per run.

    Every session reference is protected, including expired sessions that have
    not yet been removed. Facts, revisions, and frozen research are untouched.
    """
    if not lock_feed_manifests(s):
        return {"feed_manifests_removed": 0, "feed_manifest_cleanup_busy": True}
    cutoff = cutoff if cutoff is not None else now() - MANIFEST_GRACE
    unreferenced = ~exists(select(FeedSession.id).where(
        FeedSession.manifest_hash == FeedManifest.sha256))
    candidates = list(s.scalars(
        select(FeedManifest.sha256)
        .where(FeedManifest.created_at < cutoff, unreferenced)
        .order_by(FeedManifest.created_at, FeedManifest.sha256)
        .limit(MANIFEST_CLEANUP_PAGE).with_for_update(skip_locked=True)
    ))
    removed = []
    if candidates:
        removed = list(s.scalars(delete(FeedManifest)
                                .where(FeedManifest.sha256.in_(candidates), unreferenced)
                                .returning(FeedManifest.sha256)))
    return {"feed_manifests_removed": len(removed), "feed_manifest_cleanup_busy": False}


def freeze_feed_session(s, revision_ids, filters):
    # The sequence includes order and exact revision identity. Never use only the
    # ticker/group keys: revisions arriving later must not change old pagination.
    ids = list(revision_ids)
    encoded = json.dumps(ids, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    lock_feed_manifests(s, publishing=True)
    s.execute(insert(FeedManifest).values(sha256=digest, revision_ids=ids, created_at=now())
              .on_conflict_do_nothing(index_elements=[FeedManifest.sha256]))
    manifest = s.get(FeedManifest, digest)
    if manifest.revision_ids != ids:
        raise ValueError("阅读清单哈希冲突，不能发布不一致快照。")
    saved = FeedSession(revision_ids=[], manifest_hash=digest, filters=filters,
                        expires_at=now() + timedelta(hours=12))
    s.add(saved)
    s.flush()
    return saved


def load_feed_session(s, identifier):
    """Pin session and manifest together in one MVCC statement snapshot.

    The caller still checks expiry/purpose. Once admitted, that request owns an
    in-memory copy of the immutable index even if GC subsequently removes its
    expired session and manifest. No read locks or persistent retention needed.
    LEFT JOIN deliberately preserves a dangling reference as an integrity error.
    """
    row = s.execute(
        select(FeedSession, FeedManifest.revision_ids)
        .outerjoin(FeedManifest, FeedManifest.sha256 == FeedSession.manifest_hash)
        .where(FeedSession.id == identifier)
    ).one_or_none()
    if row is None:
        return None
    saved, ids = row
    saved._pinned_manifest = (saved.manifest_hash, ids)
    return saved


def session_revision_ids(s, saved):
    # Legacy rows keep their full array unchanged. A dangling new reference is
    # an explicit integrity failure, never an empty result or an implicit refresh.
    if saved.manifest_hash is None:
        return saved.revision_ids
    pinned = getattr(saved, "_pinned_manifest", None)
    if pinned is not None and pinned[0] == saved.manifest_hash:
        ids = pinned[1]
    else:
        manifest = s.get(FeedManifest, saved.manifest_hash)
        ids = manifest.revision_ids if manifest is not None else None
    if ids is None:
        raise ValueError("阅读清单缺失，请核对恢复状态；没有切换到最新数据。")
    encoded = json.dumps(ids, separators=(",", ":")).encode()
    if hashlib.sha256(encoded).hexdigest() != saved.manifest_hash:
        raise ValueError("阅读清单校验失败；没有切换到最新数据。")
    return ids
