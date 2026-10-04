import asyncio
import json
from types import SimpleNamespace

from iirp.api import db_error
from sqlalchemy.exc import OperationalError


def test_statement_timeout_does_not_claim_database_connection_is_lost():
    response = asyncio.run(db_error(None, OperationalError("redacted", {}, SimpleNamespace(sqlstate="57014"))))
    assert response.status_code == 503
    assert response.headers["retry-after"] == "3"
    message = json.loads(response.body)
    assert message["code"] == "database_timeout"
    assert "响应超时" in message["detail"]
    assert "恢复连接" not in message["detail"]
