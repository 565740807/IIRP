"""Bounded database probe for the interactive system panel."""

from datetime import datetime, timedelta, timezone

import psycopg
from sqlalchemy.engine import make_url

from iirp.config import settings


def system_database_state():
    """Avoid the shared SQLAlchemy pool when storage or workers saturate it."""
    url = make_url(settings().database_url)
    try:
        with psycopg.connect(
            host=url.host,
            port=url.port,
            dbname=url.database,
            user=url.username,
            password=url.password,
            connect_timeout=2,
            options="-c timezone=UTC -c statement_timeout=1800 -c lock_timeout=500",
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT (SELECT max(last_seen) FROM worker_heartbeat), "
                    "(SELECT version_num FROM alembic_version LIMIT 1)"
                )
                last_seen, migration = cursor.fetchone()
        return (
            {"online": bool(last_seen and last_seen > datetime.now(timezone.utc) - timedelta(seconds=15)),
             "last_seen": last_seen, "error": None},
            migration or "未知",
        )
    except psycopg.Error:
        return {"online": None, "last_seen": None, "error": "数据库状态读取超时或失败"}, "未知"
