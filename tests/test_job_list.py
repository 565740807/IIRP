"""Task list: compact keyset pages; checkpoint/result/target only in the detail.

Disposable iirp_v1_test_* database; all jobs are synthetic and never executed.
"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from iirp.api import app
from iirp.db import session
from iirp.models import Job, now
from test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401

BULK = {"rows": ["x" * 200] * 50}


@pytest.fixture
def client():
    with TestClient(app, headers={"X-IIRP-Client": "web"}) as value:
        yield value


def seed(count=45):
    stamp = now()
    with session() as s, s.begin():
        for index in range(count):
            s.add(Job(kind="sec_document", title=f"Synthetic job {index}", target=BULK,
                      checkpoint={**BULK, "control_notice": "已保存暂停请求"} if index == 0 else BULK,
                      result=BULK, idempotency_key=f"synthetic-{index}", status="SUCCEEDED",
                      error="E" * 2000 if index == 1 else None,
                      # Two jobs share a timestamp: the id breaks the tie deterministically.
                      created_at=stamp - timedelta(seconds=index - (index == 2))))


def test_pages_are_compact_ordered_and_complete(client):
    seed()
    seen, cursor, sizes = [], "", []
    while True:
        response = client.get("/api/v1/jobs", params={"limit": 20, **({"cursor": cursor} if cursor else {})})
        assert response.status_code == 200
        body = response.json()
        sizes.append(len(response.content))
        for item in body["items"]:
            assert not {"checkpoint", "result", "target"} & set(item)
        seen += body["items"]
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert [len(seen[i:i + 20]) for i in range(0, 45, 20)] == [20, 20, 5]
    assert len({item["id"] for item in seen}) == 45
    stamps = [(item["created_at"], item["id"]) for item in seen]
    assert stamps == sorted(stamps, reverse=True)
    assert max(sizes) < 30 * 1024
    first = next(item for item in seen if item["title"] == "Synthetic job 0")
    assert first["control_notice"] == "已保存暂停请求"
    truncated = next(item for item in seen if item["title"] == "Synthetic job 1")
    assert len(truncated["error"]) == 301 and truncated["error"].endswith("…")


def test_detail_has_full_payloads_and_bad_cursor_is_rejected(client):
    seed(2)
    listed = client.get("/api/v1/jobs").json()["items"]
    assert len(listed) == 2
    detail = client.get(f"/api/v1/jobs/{listed[0]['id']}").json()
    assert detail["target"] == BULK and detail["result"] == BULK and detail["checkpoint"]["rows"]
    assert client.get("/api/v1/jobs/missing").status_code == 404
    assert client.get("/api/v1/jobs", params={"cursor": "not-a-cursor"}).status_code == 400
    assert client.get("/api/v1/jobs", params={"limit": 101}).status_code == 422
