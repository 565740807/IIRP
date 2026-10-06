from datetime import datetime, timezone
from unittest.mock import patch

import psycopg
from iirp.storage.status import system_database_state


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def execute(self, statement):
        assert "worker_heartbeat" in statement and "alembic_version" in statement

    def fetchone(self):
        return datetime.now(timezone.utc), "0013"


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def cursor(self):
        return _Cursor()


def test_system_database_state_is_bounded_and_fresh():
    with patch("iirp.storage.status.psycopg.connect", return_value=_Connection()) as connect:
        worker, migration = system_database_state()
    assert worker["online"] is True
    assert migration == "0013"
    assert connect.call_args.kwargs["connect_timeout"] == 2
    assert "statement_timeout=1800" in connect.call_args.kwargs["options"]


def test_system_database_failure_is_unknown_not_offline():
    with patch("iirp.storage.status.psycopg.connect", side_effect=psycopg.OperationalError("unavailable")):
        worker, migration = system_database_state()
    assert worker == {"online": None, "last_seen": None, "error": "数据库状态读取超时或失败"}
    assert migration == "未知"
