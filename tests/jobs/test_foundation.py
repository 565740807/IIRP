"""Real PostgreSQL integration, isolated from the working development database."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from iirp.config import ROOT, settings
from iirp.db import engine, session
from iirp.jobs.queue import (
    claim,
    control,
    create_job,
    ensure_defaults,
    fenced,
    recover,
    tick,
    update_policy,
)
from iirp.models import Base, Job, Policy, SourceObject, Subscription, now
from iirp.storage.objects import save_object
from psycopg import sql
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from tests.zh import zh


@pytest.fixture(scope="module", autouse=True)
def isolated_database():
    original = settings().database_url
    url = make_url(original)
    name = "iirp_v1_test_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname="postgres",
        autocommit=True,
    )
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    old_env = os.environ.get("IIRP_DATABASE_URL")
    os.environ["IIRP_DATABASE_URL"] = url.set(database=name).render_as_string(hide_password=False)
    engine.cache_clear()
    settings.cache_clear()
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    yield
    engine().dispose()
    engine.cache_clear()
    settings.cache_clear()
    if old_env is None:
        os.environ.pop("IIRP_DATABASE_URL")
    else:
        os.environ["IIRP_DATABASE_URL"] = old_env
    admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
    admin.close()


@pytest.fixture(autouse=True)
def clean(tmp_path):
    with engine().begin() as conn:
        conn.execute(
            text(
                "TRUNCATE "
                + ",".join('"' + t.name + '"' for t in Base.metadata.sorted_tables)
                + " CASCADE"
            )
        )
    ensure_defaults()
    # Foundation restart probes exercise synthetic jobs only. Product freshness
    # defaults are verified separately, with explicit source-policy intent.
    from iirp.jobs.batches import defaults
    from iirp.models import CollectionStrategy

    with session() as s, s.begin():
        defaults(s)
        for policy in s.scalars(select(CollectionStrategy)):
            policy.enabled = False
            policy.next_run_at = None
            policy.options = {"user_controlled": True}
    previous = settings().runtime_dir
    settings().runtime_dir = tmp_path
    yield
    settings().runtime_dir = previous


@pytest.fixture
def client():
    from iirp.api.app import app

    with TestClient(app, headers={"X-IIRP-Client": "web"}) as client:
        yield client


def get(job_id):
    with session() as s:
        return s.get(Job, job_id)


def test_real_postgres_schema_matches_migration():
    with session() as s:
        assert (
            s.scalar(text("SELECT version_num FROM alembic_version"))
            == __import__("alembic.script", fromlist=["ScriptDirectory"])
            .ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
            .get_current_head()
        )
        assert "PostgreSQL 18" in s.scalar(text("SELECT version()"))
    command.check(Config(str(ROOT / "alembic.ini")))


def test_api_persists_job_before_202_and_reloads(client):
    before = time.monotonic()
    r = client.post("/api/v1/diagnostics/collections", json={"kind": "fixture_check"})
    assert r.status_code == 202
    assert time.monotonic() - before < 1
    job_id = r.json()["job_id"]
    engine().dispose()
    assert get(job_id).status == "QUEUED"
    assert client.get("/api/v1/jobs").json()["items"][0]["id"] == job_id


def test_concurrent_idempotency():
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = list(pool.map(lambda _: create_job("fixture_check", {})[0], range(8)))
    assert len({job.id for job in jobs}) == 1


@pytest.mark.parametrize("action", [None, "pause", "cancel"])
def test_creation_recovers_real_lock_conflict_without_reversing_control(monkeypatch, action):
    from iirp.jobs import queue

    target = {"controlled_contention": True}
    previous = None
    if action:
        previous = create_job("fixture_check", target)[0]
        if action == "cancel":
            claim()  # Cancellation with an active lease must stay fenced.
        control(previous.id, action)
        previous = get(previous.id)
    key = hashlib.sha256(json.dumps(["fixture_check", target], sort_keys=True).encode()).hexdigest()
    attempts, conflicts = [], []
    original = queue.enqueue
    with engine().connect() as holder:
        held = holder.begin()
        holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(key[:15], 16)})

        def observed_enqueue(s, *args):
            assert s.scalar(text("SHOW lock_timeout")) == "500ms"
            attempts.append((s, s.scalar(text("SELECT txid_current()"))))
            try:
                return original(s, *args)
            except OperationalError as error:
                conflicts.append(error.orig.sqlstate)
                held.rollback()  # Release only after a real server lock timeout.
                raise

        monkeypatch.setattr(queue, "enqueue", observed_enqueue)
        try:
            job, reused = create_job("fixture_check", target)
        finally:
            if held.is_active:
                held.rollback()
    assert conflicts == ["55P03"]
    assert len(attempts) == 2 and attempts[0][0] is not attempts[1][0]
    assert attempts[0][1] != attempts[1][1]
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 1
        assert s.scalar(select(func.count()).select_from(Subscription)) == 1
        if previous:
            assert reused and job.id == previous.id
            assert job.status == previous.status
            assert job.requested_action == previous.requested_action == action
            assert job.control_version == previous.control_version
            assert job.lease_token == previous.lease_token
            assert s.get(Subscription, (job.id, "manual")).active == (action != "cancel")
        else:
            assert not reused and job.status == "QUEUED"


def test_creation_conflict_rolls_back_job_changes_before_retry(monkeypatch):
    from iirp.jobs import queue

    job = create_job("fixture_check", {}, "automatic")[0]
    with session() as s, s.begin():
        s.add(Subscription(job_id=job.id, source="manual", active=False))
        s.get(Job, job.id).checkpoint = {"control_notice": "Existing automatic demand"}
    original = queue.enqueue
    conflicts = []
    with engine().connect() as holder:
        held = holder.begin()
        holder.execute(select(Subscription).where(
            Subscription.job_id == job.id, Subscription.source == "manual"
        ).with_for_update())

        def observed_enqueue(s, *args):
            try:
                return original(s, *args)
            except OperationalError as error:
                conflicts.append(error.orig.sqlstate)
                # The Job UPDATE precedes the blocked Subscription UPDATE;
                # a retry must not retain either uncommitted change.
                with session() as check:
                    current = check.get(Job, job.id)
                    assert current.priority == 30
                    assert current.checkpoint == {"control_notice": "Existing automatic demand"}
                    assert check.get(Subscription, (job.id, "manual")).active is False
                held.rollback()
                raise

        monkeypatch.setattr(queue, "enqueue", observed_enqueue)
        try:
            result, reused = create_job("fixture_check", {})
        finally:
            if held.is_active:
                held.rollback()
    assert conflicts == ["55P03"] and reused and result.id == job.id
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 1
        assert s.scalar(select(func.count()).select_from(Subscription)) == 2
        assert s.get(Subscription, (job.id, "automatic")).active
        assert s.get(Subscription, (job.id, "manual")).active
        assert s.get(Job, job.id).priority == 10
        assert s.get(Job, job.id).checkpoint == {}


def test_creation_retries_are_bounded_and_exhaustion_remains_503(client, monkeypatch):
    from iirp.jobs import queue

    key = hashlib.sha256(json.dumps(["fixture_check", {}], sort_keys=True).encode()).hexdigest()
    original = queue.enqueue
    conflicts = []

    def observed_enqueue(s, *args):
        assert s.scalar(text("SHOW lock_timeout")) == "500ms"
        try:
            return original(s, *args)
        except OperationalError as error:
            conflicts.append(error.orig.sqlstate)
            raise

    monkeypatch.setattr(queue, "enqueue", observed_enqueue)
    with engine().connect() as holder, holder.begin():
        holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(key[:15], 16)})
        response = client.post("/api/v1/diagnostics/collections", json={"kind": "fixture_check"})
    assert conflicts == ["55P03"] * 3
    assert response.status_code == 503 and response.headers["Retry-After"] == "1"
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 0
        assert s.scalar(select(func.count()).select_from(Subscription)) == 0


@pytest.mark.parametrize("sqlstate,retryable", [("40001", True), ("40P01", True), ("57014", False), ("08006", False)])
def test_creation_only_retries_confirmed_transaction_rejection(monkeypatch, sqlstate, retryable):
    from iirp.jobs import queue

    original, calls = queue.enqueue, []

    def server_rejection(s, *args):
        calls.append(s)
        result = original(s, *args)
        if len(calls) == 1:
            # A real PostgreSQL exception after inserts exercises full rollback;
            # 08xxx represents an uncertain connection result and must not retry.
            s.execute(text("SELECT set_config('iirp.test_sqlstate', :code, true)"), {"code": sqlstate})
            s.execute(text("DO $$ BEGIN RAISE EXCEPTION USING ERRCODE = current_setting('iirp.test_sqlstate'), MESSAGE = 'Controlled rejection'; END $$"))
        return result

    monkeypatch.setattr(queue, "enqueue", server_rejection)
    if retryable:
        job, reused = create_job("fixture_check", {})
        assert len(calls) == 2 and not reused
        assert get(job.id).status == "QUEUED"
    else:
        with pytest.raises(OperationalError) as error:
            create_job("fixture_check", {})
        assert error.value.orig.sqlstate == sqlstate and len(calls) == 1
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == int(retryable)
        assert s.scalar(select(func.count()).select_from(Subscription)) == int(retryable)


def test_skip_locked_claims_another_job():
    a = create_job("fixture_check", {"test": 1})[0]
    b = create_job("fixture_check", {"test": 2})[0]
    with session() as s, s.begin():
        s.get(Job, a.id, with_for_update=True)
        claimed = claim()
        assert claimed.id == b.id
    assert claim().id == a.id


def test_paused_job_reused_without_resuming():
    job = create_job("fixture_check", {})[0]
    control(job.id, "pause")
    again, reused = create_job("fixture_check", {})
    assert reused and again.id == job.id and again.status == "PAUSED"
    assert claim() is None
    control(job.id, "resume")
    assert claim().id == job.id


@pytest.mark.parametrize("action,expected", [("pause", "PAUSED"), ("cancel", "CANCELLED")])
def test_control_fences_progress_and_completion(action, expected):
    job = create_job("fixture_check", {})[0]
    lease = claim()
    assert fenced(lease, done=2, checkpoint={"step": 2})
    control(job.id, action)
    assert not fenced(lease, done=8, result={"must_not_commit": True}, status="SUCCEEDED")
    final = get(job.id)
    assert final.status == expected and final.progress_done == 2 and final.result is None


@pytest.mark.parametrize(
    "intent,expected", [(None, "QUEUED"), ("pause", "PAUSED"), ("cancel", "CANCELLED")]
)
def test_expired_lease_recovery_respects_intent(intent, expected):
    job = create_job("fixture_check", {})[0]
    old = claim()
    if intent:
        control(job.id, intent)
    with session() as s, s.begin():
        s.get(Job, job.id).lease_until = now() - timedelta(seconds=1)
    with session() as s, s.begin():
        recover(s)
    assert get(job.id).status == expected
    assert not fenced(old, done=8, status="SUCCEEDED")


def test_old_worker_cannot_commit_after_reclaim():
    job = create_job("fixture_check", {})[0]
    old = claim()
    with session() as s, s.begin():
        s.get(Job, job.id).lease_until = now() - timedelta(seconds=1)
    fresh = claim()
    assert fresh.lease_token != old.lease_token
    assert not fenced(old, done=8, status="SUCCEEDED")
    assert fenced(fresh, done=1)


def test_auto_pause_does_not_stop_shared_manual_demand():
    update_policy(True)
    automatic = create_job("sec_probe", {}, "automatic")[0]
    same = create_job("sec_probe", {}, "manual")[0]
    assert same.id == automatic.id
    update_policy(False)
    assert get(same.id).status == "QUEUED"
    assert claim().id == same.id
    with session() as s:
        assert not s.get(Policy, 1).sec_enabled
        assert s.get(Subscription, (same.id, "manual")).active
        assert not s.get(Subscription, (same.id, "automatic")).active


def test_auto_only_pauses_but_manual_can_proceed_without_enabling_auto():
    update_policy(True)
    job = create_job("sec_probe", {}, "automatic")[0]
    update_policy(False)
    assert get(job.id).status == "PAUSED"
    assert create_job("sec_probe", {}, "manual")[0].status == "QUEUED"
    with session() as s:
        assert not s.get(Policy, 1).sec_enabled


def test_scheduler_tick_does_not_undo_pause_or_accumulate():
    update_policy(True)
    tick()
    tick()
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 1
        job = s.scalar(select(Job))
    control(job.id, "pause")
    update_policy(False)
    update_policy(True)
    tick()
    assert get(job.id).status == "PAUSED"
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 1


def test_atomic_object_dedup_and_corruption_detection():
    a = save_object(b'{"fixture":1}')
    b = save_object(b'{"fixture":1}')
    assert (
        a == b
        and hashlib.sha256((settings().runtime_dir / a["relative_path"]).read_bytes()).hexdigest()
        == a["sha256"]
    )
    (settings().runtime_dir / a["relative_path"]).write_bytes(b"corrupted")
    with pytest.raises(OSError, match="storage.hash_mismatch"):
        save_object(b'{"fixture":1}')


def test_source_reference_and_progress_share_fence():
    job = create_job("fixture_check", {})[0]
    lease = claim()
    obj = save_object(b"synthetic-object")
    control(job.id, "cancel")
    assert not fenced(lease, done=1, source=obj)
    with session() as s:
        assert s.get(SourceObject, obj["sha256"]) is None
    # The orphan is retained; no background deletion of evidence.
    assert (settings().runtime_dir / obj["relative_path"]).exists()


def test_origin_host_validation_and_no_secret_in_api(client):
    assert (
        client.post(
            "/api/v1/diagnostics/collections",
            json={"kind": "fixture_check"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert (
        client.post("/api/v1/diagnostics/collections", json={"kind": "unknown"}).status_code == 422
    )
    assert client.get("/health/live", headers={"Host": "evil.example"}).status_code == 400
    assert (
        client.post(
            "/api/v1/diagnostics/collections", json={"kind": "market_probe", "ticker": "../etc"}
        ).status_code
        == 422
    )
    assert make_url(settings().database_url).password not in client.get("/api/v1/providers").text
    assert client.get("/api/no-such-route").status_code == 404


def test_empty_feed_and_home_before_any_collection(client):
    assert client.get("/api/v1/feed").json()["groups"] == []
    assert client.get("/api/v1/home").json()["market"][0]["value"] is None


def wait_until(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("deadline exceeded")


def test_real_worker_kill_restart_resumes_without_duplicate_objects(tmp_path):
    job = create_job("fixture_check", {})[0]
    env = {**os.environ, "IIRP_RUNTIME_DIR": str(tmp_path), "PYTHONPATH": str(ROOT / "backend")}
    args = [sys.executable, "-m", "iirp.jobs.worker"]
    proc = subprocess.Popen(
        args, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    def stage(label, predicate, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            state = get(job.id)
            assert proc.poll() is None, f"worker exited during {label}: {proc.returncode}"
            if predicate(state):
                return
            time.sleep(0.1)
        logs = tmp_path / "logs/worker-events.log"
        tail = logs.read_text()[-3000:] if logs.exists() else "no worker log"
        raise AssertionError(f"{label}: status={state.status}, progress={state.progress_done}, "
                             f"attempts={state.attempts}; {tail}")
    try:
        stage("initial claim", lambda state: state.attempts == 1)
        stage("initial checkpoints", lambda state: state.progress_done >= 2)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        saved = get(job.id).progress_done
        assert saved >= 2
        # Advance only lease expiration, not user intent or checkpoint.
        with session() as s, s.begin():
            s.get(Job, job.id).lease_until = now() - timedelta(seconds=1)
        proc = subprocess.Popen(
            args, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        # Observe lease recovery separately from the remaining six fsynced
        # checkpoints. A contended disk can make those two valid stages exceed
        # one 15-second budget; neither stage may silently stall or duplicate work.
        stage("lease recovery", lambda state: state.attempts == 2)
        stage("remaining checkpoints", lambda state: state.status == "SUCCEEDED")
        assert get(job.id).progress_done == 8
        assert get(job.id).attempts == 2
        with session() as s:
            assert s.scalar(select(func.count()).select_from(SourceObject)) == 8
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_disable_enable_before_worker_pause_acknowledgement():
    update_policy(True)
    job = create_job("sec_probe", {}, "automatic")[0]
    old = claim()
    update_policy(False)
    assert get(job.id).status == "PAUSE_REQUESTED"
    update_policy(True)
    assert get(job.id).status == "QUEUED"
    assert not fenced(old, status="SUCCEEDED")
    assert claim().id == job.id


def test_full_queue_does_not_starve_existing_work():
    for i in range(50):
        create_job("fixture_check", {"test": i})
    update_policy(True)
    tick()
    assert claim() is not None


def test_provider_control_ack_can_wait_for_child_exit():
    job = create_job("sec_probe", {})[0]
    lease = claim()
    control(job.id, "pause")
    assert not fenced(lease, acknowledge_control=False)
    assert get(job.id).status == "PAUSE_REQUESTED"
    assert not fenced(lease)
    assert get(job.id).status == "PAUSED"


def test_cancel_manual_subscription_keeps_shared_automatic_work():
    update_policy(True)
    job = create_job("sec_probe", {}, "automatic")[0]
    create_job("sec_probe", {}, "manual")
    lease = claim()
    response = control(job.id, "cancel")
    assert response.status == "RUNNING"
    assert "公共工作继续" in zh(response.checkpoint["control_notice"])
    assert fenced(lease, done=1)
    with session() as s:
        assert not s.get(Subscription, (job.id, "manual")).active
        assert s.get(Subscription, (job.id, "automatic")).active


def test_retry_auto_failure_adds_manual_demand():
    update_policy(True)
    job = create_job("sec_probe", {}, "automatic")[0]
    lease = claim()
    fenced(lease, status="FAILED", error="controlled test failure")
    control(job.id, "retry")
    update_policy(False)
    assert get(job.id).status == "QUEUED"
    with session() as s:
        assert s.get(Subscription, (job.id, "manual")).active


def test_concurrent_retry_and_new_request_reuse_one_active_job():
    job = create_job("fixture_check", {})[0]
    lease = claim()
    fenced(lease, status="FAILED", error="controlled test failure")
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(control, job.id, "retry")
        b = pool.submit(create_job, "fixture_check", {})
        assert a.result().id == b.result()[0].id


def test_database_unavailable_is_not_reported_as_success(client):
    import socket

    original = settings().database_url
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        # Bound but not listening: deterministic refusal without touching the PG server.
        bad_port = sock.getsockname()[1]
        engine().dispose()
        engine.cache_clear()
        settings().database_url = (
            make_url(original).set(port=bad_port).render_as_string(hide_password=False)
        )
        try:
            assert client.get("/health/live").status_code == 200
            assert client.get("/health/ready").status_code == 503
            response = client.post(
                "/api/v1/diagnostics/collections", json={"kind": "fixture_check"}
            )
            assert response.status_code == 503
            assert "操作尚未确认" in zh(response.json()["detail"])
        finally:
            engine().dispose()
            engine.cache_clear()
            settings().database_url = original
