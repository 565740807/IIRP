"""Earnings and custom events become pasted AI-JSON (S3).

- ``event_set`` now holds its events directly in the short format of
  ``iirp.event_input`` (several tickers allowed). Each saved set's current
  version is converted, so no user data is lost; the immutable versions,
  previews and command receipts are dropped.
- The verified earnings dates found by the removed SEC 8-K pipeline
  (``earnings_event``) are kept as one earnings set per ticker; the table, the
  earnings CSV imports (``import_preview``) and the "earnings" schedule go.
- ``prompt_template`` stores prompts the user edited.
- Old earnings / event analyses are derived data that would expire within
  24 hours anyway (D14); they, their jobs and the earnings-evidence jobs are
  deleted. The evidence source files are marked expired so the worker deletes
  them and their files like any other expired source.
"""
import json
from datetime import datetime, time
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

ET = ZoneInfo("America/New_York")
SESSIONS = {"before_open": "before_open", "during_session": "during", "after_close": "after_close"}


def _session(event_date, clock, zone):
    """Session of a local wall time, or unknown."""
    if not clock or not zone:
        return "unknown"
    local = datetime.combine(datetime.fromisoformat(event_date).date(),
                             time.fromisoformat(clock), ZoneInfo(zone)).astimezone(ET)
    if local.date().isoformat() != event_date:
        return "unknown"
    if local.time() < time(9, 30):
        return "before_open"
    return "after_close" if local.time() >= time(16) else "during"


def _convert(kind, symbol, old):
    event = {"ticker": symbol, "date": old["event_date"], "name": old["event_name"]}
    if kind == "earnings":
        event["session"] = SESSIONS.get(old.get("release_session"), "unknown")
        if event["session"] == "unknown":
            event["session"] = _session(old["event_date"], old.get("event_time"), old.get("timezone"))
        if old.get("fiscal_year") is not None:
            event["fiscal_year"] = old["fiscal_year"]
        if old.get("fiscal_quarter") is not None:
            event["fiscal_quarter"] = old["fiscal_quarter"]
    else:
        event["session"] = _session(old["event_date"], old.get("event_time"), old.get("timezone"))
    if (old.get("notes") or "").strip():
        event["note"] = old["notes"].strip()
    return event


def _order(events):
    return sorted(events, key=lambda e: (e["date"], e["ticker"]))


def _evidence_hashes(bind):
    """Source objects only the removed earnings pipeline used."""
    rows = bind.execute(sa.text("""
        SELECT DISTINCT m[1] FROM earnings_event, regexp_matches(evidence::text, '([0-9a-f]{64})', 'g') m
        UNION SELECT source_hash FROM source_observation WHERE job_id IN
            (SELECT id FROM job WHERE kind IN ('earnings_candidates', 'earnings_evidence'))
        UNION SELECT result->>'source_hash' FROM job
            WHERE kind IN ('earnings_candidates', 'earnings_evidence') AND result ? 'source_hash'
        UNION SELECT source_hash FROM coverage_segment
            WHERE kind IN ('earnings_candidates', 'earnings_evidence') AND source_hash IS NOT NULL
    """)).scalars().all()
    return [value for value in rows if value]


def upgrade():
    bind = op.get_bind()
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.create_table(
        "prompt_template",
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("language", sa.String(8), primary_key=True),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("event_set", sa.Column("events", JSONB))
    op.add_column("event_set", sa.Column("request_id", sa.String(128)))
    op.create_unique_constraint("event_set_request_id_key", "event_set", ["request_id"])

    # Saved sets: their current version, in the new format.
    sets = bind.execute(sa.text("""
        SELECT s.id, s.kind, sec.symbol, v.events
        FROM event_set s JOIN security sec ON sec.id = s.security_id
        JOIN event_set_version v ON v.set_id = s.id AND v.version = s.version
    """)).all()
    for row in sets:
        events = [_convert(row.kind, row.symbol, old) for old in row.events
                  if not old.get("excluded") and old.get("event_date")]
        bind.execute(sa.text("UPDATE event_set SET events = CAST(:events AS jsonb) WHERE id = :id"),
                     {"id": row.id, "events": json.dumps(_order(events), ensure_ascii=False)})

    # Verified dates of the removed SEC pipeline, one earnings set per ticker.
    found = bind.execute(sa.text("""
        SELECT sec.symbol, e.announced_date, e.announced_at, e.fiscal_year, e.fiscal_quarter
        FROM earnings_event e JOIN security sec ON sec.id = e.security_id
        WHERE e.fiscal_year IS NOT NULL AND e.fiscal_quarter IS NOT NULL
        ORDER BY sec.symbol, e.announced_date
    """)).all()
    by_symbol = {}
    for row in found:
        session = "unknown"
        if row.announced_at is not None:
            local = row.announced_at.astimezone(ET)
            if local.date() == row.announced_date:
                session = ("before_open" if local.time() < time(9, 30)
                           else "after_close" if local.time() >= time(16) else "during")
        by_symbol.setdefault(row.symbol, []).append({
            "ticker": row.symbol, "date": row.announced_date.isoformat(), "session": session,
            "name": f"FY{row.fiscal_year} Q{row.fiscal_quarter} earnings",
            "fiscal_year": row.fiscal_year, "fiscal_quarter": row.fiscal_quarter,
            "note": "SEC 8-K 核对的公布日期" + ("；公布时段未知" if session == "unknown" else ""),
        })
    for symbol, events in by_symbol.items():
        events = _order(events)
        bind.execute(sa.text("""
            INSERT INTO event_set (id, kind, title, events, created_at, updated_at)
            VALUES (gen_random_uuid()::text, 'earnings', :title, CAST(:events AS jsonb), now(), now())
        """), {"title": f"{symbol} 财报日期（原 SEC 自动核对，{events[0]['date'][:4]}—{events[-1]['date'][:4]}）",
               "events": json.dumps(events, ensure_ascii=False)})

    # Source files of the earnings pipeline expire now; the worker deletes them.
    hashes = _evidence_hashes(bind)
    if hashes:
        bind.execute(sa.text("""
            UPDATE source_object SET expires_at = now()
            WHERE sha256 = ANY(:hashes) AND sha256 NOT IN
                (SELECT source_hash FROM filing_version WHERE source_hash IS NOT NULL)
        """), {"hashes": hashes})

    # Old earnings / event analyses and every job only they or the pipeline used.
    for statement in (
        """CREATE TEMP TABLE s3_batches ON COMMIT DROP AS
            SELECT id FROM batch WHERE kind IN ('earnings', 'event_dates')""",
        """CREATE TEMP TABLE s3_requests ON COMMIT DROP AS
            SELECT id FROM analysis_request WHERE batch_id IN (SELECT id FROM s3_batches)""",
        """CREATE TEMP TABLE s3_scopes ON COMMIT DROP AS
            SELECT id FROM request_scope WHERE batch_id IN (SELECT id FROM s3_batches)""",
        """CREATE TEMP TABLE s3_jobs ON COMMIT DROP AS
            SELECT id FROM job WHERE kind IN ('earnings_candidates', 'earnings_evidence', 'event_compute',
                                              'local_import')
               OR (kind = 'research_compute' AND target->'params'->>'kind' = 'earnings')""",
    ):
        op.execute(statement)
    for statement in (
        """DELETE FROM research_track WHERE origin_id IN (SELECT id FROM s3_requests)
            OR latest_id IN (SELECT id FROM s3_requests)""",
        "DELETE FROM analysis_result WHERE analysis_id IN (SELECT id FROM s3_requests)",
        "DELETE FROM analysis_request WHERE id IN (SELECT id FROM s3_requests)",
        """DELETE FROM batch_job WHERE scope_id IN (SELECT id FROM s3_scopes)
            OR job_id IN (SELECT id FROM s3_jobs)""",
        "DELETE FROM job_subscription WHERE job_id IN (SELECT id FROM s3_jobs)",
        """DELETE FROM job_dependency WHERE job_id IN (SELECT id FROM s3_jobs)
            OR prerequisite_id IN (SELECT id FROM s3_jobs)""",
        "DELETE FROM source_observation WHERE job_id IN (SELECT id FROM s3_jobs)",
        "DELETE FROM job WHERE id IN (SELECT id FROM s3_jobs)",
        "DELETE FROM request_scope WHERE id IN (SELECT id FROM s3_scopes)",
        "DELETE FROM request_receipt WHERE batch_id IN (SELECT id FROM s3_batches)",
        "DELETE FROM batch_plan_signal WHERE batch_id IN (SELECT id FROM s3_batches)",
        "DELETE FROM import_preview WHERE batch_id IN (SELECT id FROM s3_batches)",
        "UPDATE batch SET parent_id = NULL WHERE parent_id IN (SELECT id FROM s3_batches)",
        "DELETE FROM batch WHERE id IN (SELECT id FROM s3_batches)",
        "DELETE FROM coverage_segment WHERE kind IN ('earnings_candidates', 'earnings_evidence')",
        "DELETE FROM collection_strategy WHERE key = 'earnings'",
    ):
        op.execute(statement)

    op.drop_table("event_set_version")
    op.drop_table("event_import_preview")
    op.drop_table("event_command_receipt")
    op.drop_column("event_set", "security_id")
    op.drop_column("event_set", "version")
    op.alter_column("event_set", "events", nullable=False)
    op.drop_table("earnings_event")
    op.drop_table("import_preview")


def downgrade():
    """Structure only, for migration tests; saved sets cannot go back.

    The live instance is never downgraded: restore the pre-S3 backup instead.
    """
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT count(*) FROM event_set")).scalar():
        raise RuntimeError("0026 downgrade would drop saved event sets; restore the pre-S3 backup")
    op.drop_table("prompt_template")
    op.drop_constraint("event_set_request_id_key", "event_set", type_="unique")
    op.drop_column("event_set", "request_id")
    op.drop_column("event_set", "events")
    op.add_column("event_set", sa.Column("security_id", sa.String(36), sa.ForeignKey("security.id"),
                                         nullable=False))
    op.add_column("event_set", sa.Column("version", sa.Integer(), nullable=False))
    op.create_index("ix_event_set_security_id", "event_set", ["security_id"])
    op.create_table(
        "event_import_preview",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("document", JSONB(), nullable=False),
        sa.Column("warnings", JSONB(), nullable=False),
        sa.Column("set_id", sa.String(36), sa.ForeignKey("event_set.id")),
        sa.Column("expected_version", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_event_import_preview_content_hash", "event_import_preview", ["content_hash"])
    op.create_table(
        "event_set_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("set_id", sa.String(36), sa.ForeignKey("event_set.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("preview_id", sa.String(36), sa.ForeignKey("event_import_preview.id"),
                  nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("document", JSONB(), nullable=False),
        sa.Column("events", JSONB(), nullable=False),
        sa.Column("reviews", JSONB(), nullable=False),
        sa.Column("revision_note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("set_id", "version"),
    )
    op.create_index("ix_event_set_version_set_id", "event_set_version", ["set_id"])
    op.create_table(
        "event_command_receipt",
        sa.Column("request_id", sa.String(128), primary_key=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("response", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "earnings_event",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("security_id", sa.String(36), sa.ForeignKey("security.id"), nullable=False),
        sa.Column("fiscal_year", sa.Integer()),
        sa.Column("fiscal_quarter", sa.Integer()),
        sa.Column("announced_date", sa.Date(), nullable=False),
        sa.Column("announced_at", sa.DateTime(timezone=True)),
        sa.Column("time_precision", sa.String(24), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("is_estimate", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("evidence", JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("security_id", "announced_date"),
    )
    op.create_index("ix_earnings_event_security_id", "earnings_event", ["security_id"])
    op.create_table(
        "import_preview",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("source_hash", sa.String(64), sa.ForeignKey("source_object.sha256"),
                  nullable=False),
        sa.Column("data", JSONB(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("batch.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
