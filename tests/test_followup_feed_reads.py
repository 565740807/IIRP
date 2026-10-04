"""Deterministic expiry/GC interleavings through real isolated PostgreSQL and API."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from time import monotonic

import pytest
from fastapi.testclient import TestClient
from iirp import sec_facts
from iirp.api import app
from iirp.business_models import FeedManifest, FeedRevision, FeedSession
from iirp.config import settings
from iirp.db import session
from iirp.feed_snapshots import cleanup_feed_manifests
from iirp.maintenance import cleanup
from iirp.models import now
from sqlalchemy import select, text
from test_sec_facts import FIXTURE, clean, isolated_database, save  # noqa: F401


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings(), "runtime_dir", tmp_path)
    with TestClient(app, headers={"X-IIRP-Client": "web"}) as value:
        yield value


def frozen_feed():
    with session() as s, s.begin():
        assert s.scalar(text("SELECT current_database()")).startswith("iirp_v1_test_")
        save(s)
        original = sec_facts.feed(s)
        saved = s.get(FeedSession, original["session_id"])
        s.get(FeedManifest, saved.manifest_hash).created_at = now() - timedelta(days=2)
        return original


@pytest.mark.parametrize("path", ["page", "group", "updates", "merge"])
def test_valid_inflight_read_survives_expiry_and_real_cleanup(client, monkeypatch, path):
    original = frozen_feed()
    identifier = original["session_id"]
    with session() as s, s.begin():
        save(s, xml=FIXTURE.read_bytes().replace(b"100000", b"125000"), accession="0000000123-26-000002")
    entered, release = Event(), Event()
    real_guard = sec_facts._session

    def paused_guard(*args, **kwargs):
        saved = real_guard(*args, **kwargs)
        entered.set()
        assert release.wait(10), "reader scheduling barrier timed out"
        return saved

    monkeypatch.setattr(sec_facts, "_session", paused_guard)
    monkeypatch.setattr("iirp.feed_updates._session", paused_guard)
    url = "/api/v1/feed" if path == "page" else (
        f'/api/v1/feed/groups/{original["groups"][0]["id"]}' if path == "group" else "/api/v1/feed/updates")
    with ThreadPoolExecutor(max_workers=1) as pool:
        params = {"session_id": identifier}
        if path == "merge":
            params["include_groups"] = "true"
        reading = pool.submit(client.get, url, params=params)
        try:
            assert entered.wait(5), "reader never passed the real validity check"
            # Advance this isolated row's expiry after the guard; no wall-clock race.
            with session() as s, s.begin():
                s.get(FeedSession, identifier).expires_at = now() - timedelta(seconds=1)
            reclaimed = cleanup()["data"]
            assert reclaimed["reading_sessions_removed"] == 1
            assert reclaimed["feed_manifests_removed"] == 1
            with session() as s:
                assert s.get(FeedSession, identifier) is None
        finally:
            release.set()
        response = reading.result(timeout=5)
    print(json.dumps({"path": path, "http": response.status_code, "body": response.json(),
                      "cleanup": reclaimed}, ensure_ascii=False))
    assert response.status_code == 200
    body = response.json()
    if path == "page":
        assert body["groups"] == original["groups"] and body["session_id"] == identifier
    elif path == "group":
        assert body["revision_id"] == original["groups"][0]["revision_id"]
        assert body["items"]
    elif path == "updates":
        assert body["new_count"] == 1 and body["target_session_id"] == identifier
    else:
        assert body["new_count"] == 1 and body["target_session_id"] != identifier
        assert body["groups"][0]["revision_id"] != original["groups"][0]["revision_id"]
    # A later request does not inherit the in-flight request's protection.
    assert "过期" in client.get(url, params={"session_id": identifier}).json()["detail"]


@pytest.mark.parametrize("damage", ["expired", "missing", "corrupt"])
@pytest.mark.parametrize("path", ["page", "group", "updates"])
def test_expiry_and_integrity_errors_remain_distinct(client, damage, path):
    original = frozen_feed()
    with session() as s, s.begin():
        saved = s.get(FeedSession, original["session_id"])
        if damage == "expired":
            saved.expires_at = now() - timedelta(seconds=1)
        elif damage == "corrupt":
            s.get(FeedManifest, saved.manifest_hash).revision_ids = ["corrupt"]
        else:
            # Simulate restored corruption only in the disposable DB. Production
            # FK protection remains intact; normal deletes cannot create this.
            s.execute(text("SET LOCAL session_replication_role = replica"))
            s.execute(text("DELETE FROM feed_manifest WHERE sha256=:digest"), {"digest": saved.manifest_hash})
            s.execute(text("SET LOCAL session_replication_role = origin"))
        assert s.scalar(select(FeedRevision.id)) is not None
    url = "/api/v1/feed" if path == "page" else (
        f'/api/v1/feed/groups/{original["groups"][0]["id"]}' if path == "group" else "/api/v1/feed/updates")
    response = client.get(url, params={"session_id": original["session_id"]})
    print(json.dumps({"damage": damage, "path": path, "http": response.status_code, "body": response.json()}, ensure_ascii=False))
    assert response.status_code == (400 if path == "updates" else 409)
    assert {"expired": "过期", "missing": "清单缺失", "corrupt": "校验失败"}[damage] in response.json()["detail"]
    if damage != "expired":
        assert "过期" not in response.json()["detail"]


@pytest.mark.parametrize("path", ["new_session", "merge"])
def test_controlled_gc_lock_busy_then_api_recovers(client, path):
    original = frozen_feed()
    with session() as s, s.begin():
        save(s, xml=FIXTURE.read_bytes().replace(b"100000", b"125000"), accession="0000000123-26-000002")
        s.add(FeedManifest(sha256="orphan", revision_ids=["unreferenced"], created_at=now() - timedelta(days=2)))
    url = "/api/v1/feed" if path == "new_session" else "/api/v1/feed/updates"
    params = {} if path == "new_session" else {"session_id": original["session_id"], "include_groups": "true"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        with session() as s, s.begin():
            assert cleanup_feed_manifests(s)["feed_manifests_removed"] == 1
            started = monotonic()
            busy = pool.submit(client.get, url, params=params).result(timeout=5)
            elapsed = (monotonic() - started) * 1000
            assert busy.status_code == 503
            # The GC transaction is deliberately held until timeout. Existing
            # readers need no publication lock and still see their frozen data.
            old = pool.submit(client.get, "/api/v1/feed", params={"session_id": original["session_id"]}).result(timeout=5)
            assert old.status_code == 200 and old.json()["groups"] == original["groups"]
        retried = client.get(url, params=params)
    print(json.dumps({"experiment": "deliberately held GC, not normal load", "path": path,
                      "busy_http": busy.status_code, "busy_body": busy.json(), "elapsed_ms": elapsed,
                      "retry_http": retried.status_code}, ensure_ascii=False))
    assert retried.status_code == 200
    if path == "merge":
        assert retried.json()["groups"] and retried.json()["target_session_id"] != original["session_id"]
