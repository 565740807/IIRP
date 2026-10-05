import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from iirp.config import settings


def save_object(payload: bytes, media_type="application/json"):
    if not payload or len(payload) > 128 * 1024**2:
        raise ValueError("来源对象必须在 1 字节至 128 MiB 内；较大清单应按范围分块。")
    root = settings().runtime_dir
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < settings().min_free_bytes:
        raise OSError("可用空间不足 10 GiB，已停止新增采集。")
    digest = hashlib.sha256(payload).hexdigest()
    rel = f"objects/{digest[:2]}/{digest}"
    final = root / rel
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        if hashlib.sha256(final.read_bytes()).hexdigest() != digest:
            raise OSError("来源对象哈希不一致，需要核对存储。")
    else:
        fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=final.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, final)
            directory_fd = os.open(final.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(tmp).unlink(missing_ok=True)
    return {
        "sha256": digest,
        "relative_path": rel,
        "byte_size": len(payload),
        "media_type": media_type,
    }


def response_evidence(data, sources):
    """Separate immutable content from observation metadata without losing raw evidence.

    Call after separately saving source_documents and filing XML. Existing source
    objects are never rewritten. The returned observation metadata belongs on the
    SourceObservation row, not in the content hash of an unchanged response.
    """
    import base64
    import json

    # Only these branches are edited. Prices, windows and other large result
    # trees are read-only during encoding; copying them doubled peak residency.
    content = dict(data)
    if "source_documents" in content:
        content["source_documents"] = [dict(document) for document in content["source_documents"]]
    if content.get("filing"):
        content["filing"] = dict(content["filing"])
    observation = {key: content.pop(key) for key in ("fetched_at", "timing") if key in content}

    def reference(url, payload, encoding):
        raw = base64.b64decode(payload, validate=True) if encoding == "base64" else payload.encode()
        source = sources.get(url)
        if (not source or source["sha256"] != hashlib.sha256(raw).hexdigest()
                or source["byte_size"] != len(raw)):
            raise ValueError("来源内容引用与已保存原文不一致，不能发布证据。")
        return {key: source[key] for key in ("sha256", "byte_size")}

    for document in content.get("source_documents", []):
        if "payload" in document:
            document["payload_ref"] = reference(document["url"], document["payload"], document.get("encoding"))
            del document["payload"]
    filing = content.get("filing") or {}
    if filing.get("xml_payload"):
        filing["xml_payload_ref"] = reference(filing["document_url"], filing["xml_payload"], filing.get("xml_encoding"))
        del filing["xml_payload"]
    payload = {"_iirp_evidence_format": "response-v2", "data": content}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode(), observation


def hydrate_response_evidence(payload, observation_metadata=None):
    """Replay old inline responses and new referenced responses, verifying every hash."""
    import base64
    import json

    envelope = json.loads(payload)
    if not isinstance(envelope, dict):
        raise ValueError("来源响应必须为 JSON 对象。")
    if "_iirp_evidence_format" not in envelope:
        return envelope
    if envelope["_iirp_evidence_format"] != "response-v2":
        raise ValueError("无法识别来源证据格式。")
    content = envelope["data"]

    def read(reference, encoding):
        digest = reference["sha256"]
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("来源证据引用哈希无效。")
        path = settings().runtime_dir / "objects" / digest[:2] / digest
        raw = path.read_bytes()
        if len(raw) != reference["byte_size"] or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("来源证据引用校验失败，不能重解析。")
        return base64.b64encode(raw).decode() if encoding == "base64" else raw.decode("utf-8")

    for document in content.get("source_documents", []):
        if "payload_ref" in document:
            document["payload"] = read(document.pop("payload_ref"), document.get("encoding"))
    filing = content.get("filing") or {}
    if "xml_payload_ref" in filing:
        filing["xml_payload"] = read(filing.pop("xml_payload_ref"), filing.get("xml_encoding"))
    if observation_metadata:
        content.update({key: observation_metadata[key] for key in ("fetched_at", "timing") if key in observation_metadata})
    return content


DISCOVERY_RETENTION_DAYS = 7


def discovery_expiry():
    """Refetchable SEC list pages, index files and discovery response JSON."""
    from datetime import timedelta

    from iirp.models import now

    return now() + timedelta(days=DISCOVERY_RETENTION_DAYS)


def register_object(s, source):
    """Insert a saved object row; a permanent use of the same content wins.

    ``source`` may carry ``expires_at``. An existing expiring row becomes
    permanent when registered without expiry and otherwise keeps the later
    expiry; a permanent row never gains one.
    """
    from sqlalchemy import func
    from sqlalchemy.dialects.postgresql import insert

    from iirp.models import SourceObject

    expires_at = source.get("expires_at")
    statement = insert(SourceObject).values(**{**source, "expires_at": expires_at})
    s.execute(statement.on_conflict_do_update(
        index_elements=[SourceObject.sha256],
        set_={"expires_at": None if expires_at is None else
              func.greatest(SourceObject.expires_at, statement.excluded.expires_at)},
        where=SourceObject.expires_at.is_not(None),
    ))


EXPIRY_BATCH = 500
EXPIRY_MAX_PER_TICK = 5000


def expire_sources(limit=EXPIRY_MAX_PER_TICK):
    """Delete expired objects, their observations and files in bounded batches.

    Holds the maintenance lock so a running backup never loses a file it has
    listed. Content also referenced by a permanent fact is made permanent
    instead. Files are unlinked only after commit and only when no row has
    re-registered the same content meanwhile.
    """
    from sqlalchemy import delete, select, update

    from iirp.business_models import (
        CorporateAction,
        CoverageSegment,
        FilingVersion,
        ImportPreview,
        MarketBar,
        SourceObservation,
    )
    from iirp.db import session
    from iirp.maintenance import maintenance_lock
    from iirp.models import Coverage, SourceObject, now

    removed = {"objects": 0, "bytes": 0}
    try:
        lock = maintenance_lock()
        lock.__enter__()
    except ValueError:
        return {**removed, "skipped": "maintenance_busy"}
    try:
        while removed["objects"] < limit:
            with session() as s, s.begin():
                rows = s.execute(
                    select(SourceObject.sha256, SourceObject.relative_path, SourceObject.byte_size)
                    .where(SourceObject.expires_at < now())
                    .order_by(SourceObject.expires_at)
                    .limit(EXPIRY_BATCH)
                    .with_for_update(skip_locked=True)
                ).all()
                if not rows:
                    break
                hashes = [row.sha256 for row in rows]
                permanent = set()
                for model in (FilingVersion, MarketBar, CorporateAction, ImportPreview, Coverage):
                    permanent.update(s.scalars(select(model.source_hash).where(model.source_hash.in_(hashes))))
                if permanent:
                    s.execute(update(SourceObject).where(SourceObject.sha256.in_(permanent))
                              .values(expires_at=None))
                doomed = [row for row in rows if row.sha256 not in permanent]
                gone = [row.sha256 for row in doomed]
                s.execute(delete(SourceObservation).where(SourceObservation.source_hash.in_(gone)))
                s.execute(update(CoverageSegment).where(CoverageSegment.source_hash.in_(gone))
                          .values(source_hash=None))
                s.execute(delete(SourceObject).where(SourceObject.sha256.in_(gone)))
            root = settings().runtime_dir
            with session() as s:
                again = set(s.scalars(select(SourceObject.sha256).where(SourceObject.sha256.in_(gone))))
            for row in doomed:
                if row.sha256 not in again:
                    (root / row.relative_path).unlink(missing_ok=True)
                    removed["objects"] += 1
                    removed["bytes"] += row.byte_size
            if len(rows) < EXPIRY_BATCH:
                break
    finally:
        lock.__exit__(None, None, None)
    return removed
