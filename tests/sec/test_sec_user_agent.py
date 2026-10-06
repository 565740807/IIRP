"""SEC runs automatically only with a real contact User-Agent, and never above the configured rate.

Uses a disposable iirp_v1_test_* database. HTTP goes to an in-process mock transport;
nothing here contacts SEC.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from iirp.config import ROOT, settings
from iirp.db import session
from iirp.jobs import operations
from iirp.jobs.auto_update import ensure_fresh, get_freshness
from iirp.jobs.profiles import profiles
from iirp.jobs.providers import SEC_TEMPLATE_USER_AGENTS, sec_contact_ok
from iirp.models import Batch, CollectionStrategy, now
from sqlalchemy import func, select

from tests.jobs.test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401

REAL = "IIRP research desk ops@iirp-research.dev"


@pytest.mark.parametrize("value", [
    REAL,
    "Jane Doe jane.doe@university.edu",
    "IIRP/0.1 (admin@mail.company.co.uk)",
])
def test_real_contact_is_accepted(value):
    assert sec_contact_ok(value)


@pytest.mark.parametrize("value", [
    "",
    "   ",
    "IIRP research tool",
    "IIRP contact@example.invalid",
    "IIRP me@example.com",
    "IIRP me@EXAMPLE.ORG",
    "IIRP me@mail.example.net",
    "IIRP me@lab.test",
    "IIRP me@host.invalid",
    "IIRP me@box.localhost",
    "IIRP me@corp.example",
    "IIRP me@localhost",
])
def test_reserved_or_template_contact_is_rejected(value):
    assert not sec_contact_ok(value)


def test_env_example_template_is_rejected():
    lines = (ROOT / "deploy/.env.example").read_text().splitlines()
    value = next(line.split("=", 1)[1] for line in lines if line.startswith("IIRP_SEC_USER_AGENT="))
    assert value in SEC_TEMPLATE_USER_AGENTS
    assert not sec_contact_ok(value)


@pytest.fixture
def user_agent(monkeypatch):
    def configure(value):
        monkeypatch.setattr(settings(), "sec_user_agent", value)
    return configure


@pytest.fixture
def sec_requests(monkeypatch):
    """Record every HTTP request fetch_sec would send; respond locally."""
    sent, lock = [], threading.Lock()
    original = httpx.Client

    def handler(request):
        with lock:
            sent.append(time.monotonic())
        return httpx.Response(200, content=b"<ok/>")

    monkeypatch.setattr(
        operations.httpx, "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    return sent


def automatic_sec_batches():
    with session() as s:
        return s.scalar(select(func.count()).select_from(Batch).where(Batch.kind == "sec_latest"))


@pytest.fixture(autouse=True)
def no_storage_inventory(monkeypatch):
    """/api/v1/system would start a background inventory that holds a connection."""
    monkeypatch.setattr("iirp.storage.maintenance.storage_state", lambda: {})


def test_first_start_without_contact_keeps_policy_on_but_sends_nothing(user_agent, sec_requests):
    from iirp.jobs.schedule import schedule_tick

    user_agent("IIRP contact@example.invalid")
    ensure_fresh({"reason": "startup", "sources": ["sec", "market"]})
    schedule_tick()
    state = get_freshness()
    with session() as s:
        policy = s.get(CollectionStrategy, "sec")
        assert policy.enabled
        assert policy.next_run_at > now() + timedelta(minutes=4)
        assert s.get(CollectionStrategy, "market").enabled
    assert automatic_sec_batches() == 0
    assert state["sources"]["sec"]["status"] == "needs_config"
    assert state["sources"]["sec"]["stage"] == "需配置 SEC User-Agent"
    with pytest.raises(operations.ProviderFailure):
        operations.fetch_sec("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent")
    assert sec_requests == []

    from iirp.api.app import app
    client = TestClient(app)
    system = client.get("/api/v1/system").json()["sec_user_agent"]
    assert system["configured"] is False and system["status"] == "NEEDS_CONFIG"
    assert "需配置 SEC User-Agent" in system["message"]
    sec = next(p for p in client.get("/api/v1/collection-policy").json()["items"] if p["key"] == "sec")
    assert sec["enabled"] is True and "需配置 SEC User-Agent" in sec["blocked_reason"]
    refused = client.post("/api/v1/collections", json={"request_id": "manual-sec", "kind": "sec_latest"},
                          headers={"X-IIRP-Client": "web"})
    assert refused.status_code == 409 and "需配置 SEC User-Agent" in refused.json()["detail"]


def test_first_start_with_real_contact_schedules_sec(user_agent):
    user_agent(REAL)
    ensure_fresh({"reason": "startup", "sources": ["sec"]})
    assert automatic_sec_batches() == 1
    assert get_freshness()["sources"]["sec"]["status"] != "needs_config"
    from iirp.api.app import app
    assert TestClient(app).get("/api/v1/system").json()["sec_user_agent"]["configured"] is True


def test_concurrent_lanes_never_exceed_configured_sec_rate(user_agent, sec_requests):
    rate = profiles()["normal_usage"]["sec_requests_per_second"]
    assert rate == 2
    user_agent(REAL)
    url = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/x.xml"
    # More concurrent callers than the worker's two SEC lanes, each with its own session.
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: operations.fetch_sec(url), range(12)))
    times = sorted(sec_requests)
    assert len(times) == 12
    # Request starts, not reservations: no one-second window holds more than `rate`.
    assert max(sum(x <= y < x + 1 for y in times) for x in times) <= rate
    assert (len(times) - 1) / (times[-1] - times[0]) <= rate
