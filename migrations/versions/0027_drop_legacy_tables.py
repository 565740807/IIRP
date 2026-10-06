"""Drop structures the application no longer reads or writes (S4).

- The versioned price tables replaced by the 24-hour cache in 0025
  (``price_dataset_version``, ``market_bar_revision``, ``dataset_bar``,
  ``corporate_action``); their rows were already deleted in the S2 cleanup.
- Feed reading sessions frozen as revision manifests before the watermark
  schema (0021): such sessions expired long ago, so they, ``feed_manifest``
  and ``feed_session.manifest_hash`` go. Entity-history sessions keep
  ``revision_ids``.
- The partial index of complete SEC latest scans (0023): latest scans are no
  longer jobs since 0024.
- ``filing_version.source_hash`` gets an index, so deleting or checking a
  source object no longer scans every filing version.

The downgrade restores only the structure, for migration tests; the live
instance is never downgraded (restore the pre-upgrade backup instead).
"""
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("dataset_bar", "price_dataset_version", "market_bar_revision", "corporate_action"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DELETE FROM feed_session WHERE manifest_hash IS NOT NULL "
               "OR (filters ->> 'purpose' = 'feed' AND NOT filters ? 'watermark')")
    op.drop_index("ix_feed_session_manifest_hash", table_name="feed_session")
    op.drop_column("feed_session", "manifest_hash")
    op.drop_table("feed_manifest")
    op.execute("DROP INDEX IF EXISTS ix_job_sec_latest_complete")
    op.create_index("ix_filing_version_source_hash", "filing_version", ["source_hash"])


def downgrade():
    op.drop_index("ix_filing_version_source_hash", table_name="filing_version")
    op.execute("""
        CREATE INDEX ix_job_sec_latest_complete ON job (created_at DESC)
        WHERE kind = 'sec_discover' AND status = 'SUCCEEDED' AND (target ->> 'mode') = 'latest'
          AND (checkpoint['sec_scan'] ->> 'complete') = 'true';
        CREATE TABLE feed_manifest (sha256 varchar(64) PRIMARY KEY, revision_ids jsonb NOT NULL,
                                    created_at timestamptz NOT NULL);
        CREATE INDEX ix_feed_manifest_created ON feed_manifest (created_at, sha256);
        ALTER TABLE feed_session ADD COLUMN manifest_hash varchar(64)
            CONSTRAINT fk_feed_session_manifest_hash REFERENCES feed_manifest (sha256);
        CREATE INDEX ix_feed_session_manifest_hash ON feed_session (manifest_hash);
        CREATE TABLE corporate_action (
            id varchar(36) PRIMARY KEY, security_id varchar(36) NOT NULL REFERENCES security (id),
            session_date date NOT NULL, kind varchar(16) NOT NULL, value numeric(30, 12) NOT NULL,
            source_hash varchar(64) NOT NULL REFERENCES source_object (sha256),
            UNIQUE (security_id, session_date, kind, value));
        CREATE INDEX ix_corporate_action_security_id ON corporate_action (security_id);
        CREATE TABLE market_bar_revision (
            id varchar(36) PRIMARY KEY, security_id varchar(36) NOT NULL REFERENCES security (id),
            session_date date NOT NULL, provider varchar(32) NOT NULL,
            source_hash varchar(64) NOT NULL REFERENCES source_object (sha256),
            record_hash varchar(64) NOT NULL, open numeric(30, 12), high numeric(30, 12),
            low numeric(30, 12), close numeric(30, 12), adj_close numeric(30, 12),
            volume numeric(32, 4), status varchar(32) NOT NULL, reason text,
            observed_at timestamptz NOT NULL,
            UNIQUE (security_id, session_date, provider, record_hash));
        CREATE INDEX ix_market_bar_revision_security_id ON market_bar_revision (security_id);
        CREATE TABLE price_dataset_version (
            id varchar(36) PRIMARY KEY, security_id varchar(36) NOT NULL REFERENCES security (id),
            basis varchar(48) NOT NULL, basis_key varchar(64) NOT NULL, status varchar(24) NOT NULL,
            manifest jsonb NOT NULL, created_at timestamptz NOT NULL, published_at timestamptz);
        CREATE INDEX ix_price_dataset_version_security_id ON price_dataset_version (security_id);
        CREATE TABLE dataset_bar (
            dataset_id varchar(36) NOT NULL REFERENCES price_dataset_version (id),
            session_date date NOT NULL, bar_id varchar(36) NOT NULL REFERENCES market_bar_revision (id),
            PRIMARY KEY (dataset_id, session_date));
    """)
