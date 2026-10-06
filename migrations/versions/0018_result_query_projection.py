"""Keep lossless compressed E2 JSON and a compact frozen query projection."""

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("analysis_result", sa.Column("payload", sa.LargeBinary(), nullable=True))
    op.add_column("analysis_result", sa.Column("overlap_projection", JSONB(), nullable=True))
    # Already compressed in the application; PostgreSQL only stores/toasts it.
    op.execute("ALTER TABLE analysis_result ALTER COLUMN payload SET STORAGE EXTERNAL")
    op.execute("ALTER TABLE analysis_result ALTER COLUMN overlap_projection SET COMPRESSION lz4")
    op.create_check_constraint(
        "ck_analysis_result_projection_pair", "analysis_result",
        "(payload IS NULL AND overlap_projection IS NULL) OR "
        "(payload IS NOT NULL AND overlap_projection IS NOT NULL AND data='{}'::jsonb)",
    )


def downgrade():
    from iirp.models.compressed import decode_payload

    # Reconstitute the original representation before removing new columns.
    # One immutable result at a time bounds conversion memory. Alembic's
    # transaction rolls back all conversions if any statement fails.
    connection = op.get_bind()
    op.drop_constraint("ck_analysis_result_projection_pair", "analysis_result", type_="check")
    last_id = ""
    while True:
        row = connection.execute(sa.text(
            "SELECT id,payload FROM analysis_result WHERE payload IS NOT NULL "
            "AND id>:last_id ORDER BY id LIMIT 1"
        ), {"last_id": last_id}).first()
        if row is None:
            break
        _restore_json(connection, row.id, decode_payload(row.payload))
        last_id = row.id
        del row
    op.drop_column("analysis_result", "overlap_projection")
    op.drop_column("analysis_result", "payload")


def _restore_json(connection, identifier, value, path=()):
    """Bound JSONB parsing as well as transfer, without cutting any input.

    Small subtrees are cast directly. Large objects are restored by key and
    arrays in ordered groups. Every intermediate row remains inside the same
    locked Alembic transaction, so failures expose no partial legacy result.
    """
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded) <= 512 * 1024 or not isinstance(value, (dict, list)):
        _write_json(connection, identifier, path, encoded)
        return
    del encoded
    if isinstance(value, dict):
        _write_json(connection, identifier, path, "{}")
        for key, item in value.items():
            _restore_json(connection, identifier, item, (*path, key))
    else:
        _write_json(connection, identifier, path, "[]")
        for start in range(0, len(value), 64):
            group = value[start:start + 64]
            encoded = json.dumps(group, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if len(encoded) <= 512 * 1024:
                _write_json(connection, identifier, path, encoded, append=True)
            else:
                # An unusually large item may itself need decomposition. Add
                # its slot first so jsonb_set can address the original index.
                del encoded
                for index, item in enumerate(group, start):
                    _write_json(connection, identifier, path, "[null]", append=True)
                    _restore_json(connection, identifier, item, (*path, str(index)))


def _write_json(connection, identifier, path, encoded, *, append=False):
    if not path:
        expression = "data || CAST(:value AS jsonb)" if append else "CAST(:value AS jsonb)"
    else:
        expression = "CAST(:value AS jsonb)"
        if append:
            expression = "(data #> CAST(:path AS text[])) || " + expression
        expression = f"jsonb_set(data, CAST(:path AS text[]), {expression}, true)"
    connection.execute(sa.text(
        f"UPDATE analysis_result SET data={expression} WHERE id=:id"
    ), {"id": identifier, "path": list(path), "value": encoded})
