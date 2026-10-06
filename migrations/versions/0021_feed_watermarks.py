"""Feed reading watermarks, current revision pointers and sort keys.

Revisions get a publication sequence and the writing transaction id. Existing
revisions keep NULLs (visible to every new reading session) instead of being
rewritten. ``feed_group_current`` and ``feed_group_order`` are backfilled from
each group's newest revision. feed_manifest and feed_session data stay in place
for existing sessions and the normal cleanup; nothing new is written there.
"""
from alembic import op
from sqlalchemy import text

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

MISSING = "0001-01-01 00:00:00+00"
CONCURRENT = {
    "ix_feed_group_revision_seq": "ON feed_group_revision (seq) WHERE seq IS NOT NULL",
    "ix_feed_group_revision_xid": "ON feed_group_revision (xid) WHERE xid IS NOT NULL",
}


def _concurrent_indexes():
    with op.get_context().autocommit_block():
        connection = op.get_bind()
        previous = {key: connection.scalar(text(f"SHOW {key}"))
                    for key in ("statement_timeout", "lock_timeout")}
        try:
            op.execute("SET statement_timeout = '600s'")
            op.execute("SET lock_timeout = '3s'")
            for name, definition in CONCURRENT.items():
                valid = connection.scalar(text(
                    "SELECT i.indisvalid FROM pg_index i WHERE i.indexrelid = to_regclass(:name)"),
                    {"name": name})
                if valid is False:
                    op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
                if valid is not True:
                    op.execute(f"CREATE INDEX CONCURRENTLY {name} {definition}")
        finally:
            for key, value in previous.items():
                connection.execute(text("SELECT set_config(:key, :value, false)"),
                                   {"key": key, "value": value})


def upgrade():
    # 1. Schema only, committed at once: the exclusive lock on the revision
    #    table lasts milliseconds instead of the whole backfill.
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("CREATE SEQUENCE IF NOT EXISTS feed_revision_seq")
    # Metadata-only: existing rows stay NULL, no table rewrite.
    op.execute("ALTER TABLE feed_group_revision ADD COLUMN IF NOT EXISTS seq bigint, ADD COLUMN IF NOT EXISTS xid xid8")
    op.execute("""ALTER TABLE feed_group_revision
        ALTER COLUMN seq SET DEFAULT nextval('feed_revision_seq'),
        ALTER COLUMN xid SET DEFAULT pg_current_xact_id()""")
    op.execute("ALTER SEQUENCE feed_revision_seq OWNED BY feed_group_revision.seq")

    op.execute("""CREATE TABLE IF NOT EXISTS feed_group_current (
        group_key varchar(32) PRIMARY KEY,
        revision_id varchar(36) NOT NULL REFERENCES feed_group_revision (id),
        issuer_id varchar(10) NOT NULL,
        accepted_at timestamptz NOT NULL,
        row_count integer NOT NULL,
        seq bigint,
        xid xid8)""")
    op.execute("""CREATE TABLE IF NOT EXISTS feed_group_order (
        kind varchar(16) NOT NULL,
        sort_order varchar(16) NOT NULL,
        group_key varchar(32) COLLATE "C" NOT NULL,
        sort_key timestamptz NOT NULL,
        accepted_at timestamptz NOT NULL,
        revision_id varchar(36) NOT NULL,
        seq bigint,
        xid xid8,
        PRIMARY KEY (kind, sort_order, group_key))""")
    # Transaction ids are meaningful only inside one cluster; see iirp.insider.feed_index.
    op.execute("""CREATE TABLE IF NOT EXISTS feed_watermark_cluster (
        id integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
        system_identifier text NOT NULL)""")
    op.execute("""INSERT INTO feed_watermark_cluster (system_identifier)
        SELECT system_identifier::text FROM pg_control_system() ON CONFLICT DO NOTHING""")
    # Every step below is idempotent: a failed backfill leaves this schema
    # committed and a rerun continues from it.
    with op.get_context().autocommit_block():
        pass
    # 2. Backfill reads every group once; app connections default to 5 s / 500 ms.
    #    Pointers already written by new code are newer and are kept.
    op.execute("SET LOCAL statement_timeout = '900s'")
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("""
        INSERT INTO feed_group_current (group_key, revision_id, issuer_id, accepted_at, row_count, seq, xid)
        SELECT DISTINCT ON (group_key) group_key, id, issuer_id, accepted_at, row_count, seq, xid
        FROM feed_group_revision
        ORDER BY group_key, seq DESC NULLS LAST, created_at DESC, id DESC
        ON CONFLICT (group_key) DO NOTHING
    """)
    op.execute(f"""
        INSERT INTO feed_group_order
            (kind, sort_order, group_key, sort_key, accepted_at, revision_id, seq, xid)
        SELECT k.kind, o.sort_order, c.group_key,
               CASE o.sort_order
                 WHEN 'transaction' THEN COALESCE(
                   (NULLIF(r.transaction_sort_dates ->> k.kind, '')::date)::timestamp AT TIME ZONE 'UTC',
                   '{MISSING}'::timestamptz)
                 ELSE COALESCE(
                   NULLIF(r.transaction_sort_dates ->> ('accepted:' || k.kind), '')::timestamptz,
                   '{MISSING}'::timestamptz)
               END,
               r.accepted_at, r.id, r.seq, r.xid
        FROM feed_group_current c
        JOIN feed_group_revision r ON r.id = c.revision_id
        CROSS JOIN LATERAL jsonb_array_elements_text(r.match_kinds) AS k(kind)
        CROSS JOIN (VALUES ('transaction'), ('accepted')) AS o(sort_order)
        WHERE c.row_count > 0 AND c.seq IS NULL
        ON CONFLICT DO NOTHING
    """)
    op.execute("""CREATE INDEX IF NOT EXISTS ix_feed_group_current_seq
        ON feed_group_current (seq) WHERE seq IS NOT NULL""")
    op.execute("""CREATE INDEX IF NOT EXISTS ix_feed_group_current_xid
        ON feed_group_current (xid) WHERE xid IS NOT NULL""")
    op.execute("""CREATE INDEX IF NOT EXISTS ix_feed_group_order_page ON feed_group_order
        (kind, sort_order, sort_key DESC, accepted_at DESC, group_key DESC)""")
    op.execute("ANALYZE feed_group_current")
    op.execute("ANALYZE feed_group_order")
    _concurrent_indexes()


def downgrade():
    # Inside the migration transaction: a failed multi-step downgrade rolls back whole.
    for name in CONCURRENT:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    op.drop_table("feed_watermark_cluster")
    op.drop_table("feed_group_order")
    op.drop_table("feed_group_current")
    op.drop_column("feed_group_revision", "xid")
    op.drop_column("feed_group_revision", "seq")
    op.execute("DROP SEQUENCE IF EXISTS feed_revision_seq")
