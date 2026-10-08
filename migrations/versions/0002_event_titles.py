"""Materialize only legacy generated event titles; event content stays untouched."""

import json

from alembic import op
from iirp.messages import saved_name
from sqlalchemy import text

revision = "0002_event_titles"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def readable(value):
    try:
        message = json.loads(value)
        code, params = message["code"], message["params"]
        if code not in {f"events.set_title.{kind}{suffix}" for kind in ("earnings", "custom")
                        for suffix in ("", "_more")}:
            return value
        if (not isinstance(params["tickers"], list)
                or not all(isinstance(t, str) for t in params["tickers"])
                or not str(params["first"]).isdigit() or not str(params["last"]).isdigit()):
            return value
        return saved_name(code, **params)
    except (ValueError, TypeError, KeyError):
        return value


def upgrade():
    connection = op.get_bind()
    for table in ("event_set", "batch"):
        rows = connection.execute(text(f"SELECT id, title FROM {table} WHERE title LIKE :pattern"),
                                  {"pattern": '%"events.set_title.%'}).all()
        for identifier, title in rows:
            name = readable(title)
            if name != title:
                connection.execute(text(f"UPDATE {table} SET title=:title WHERE id=:id"),
                                   {"title": name, "id": identifier})
    rows = connection.execute(text("SELECT id, params FROM analysis_request "
                                   "WHERE params->>'kind'='event_dates' "
                                   "AND params->>'title' LIKE :pattern"),
                              {"pattern": '%"events.set_title.%'}).all()
    for identifier, params in rows:
        name = readable(params["title"])
        if name != params["title"]:
            connection.execute(text("UPDATE analysis_request SET params=jsonb_set(params, '{title}', "
                                    "CAST(:title AS jsonb)) WHERE id=:id"),
                               {"title": json.dumps(name), "id": identifier})


def downgrade():
    # Editable names cannot safely be converted back into message objects.
    pass
