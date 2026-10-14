"""Disclosure HTTP boundaries use mock bytes and a real disposable coordination database."""
import gzip
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import pytest
from iirp.db import session
from iirp.jobs.operations import ProviderFailure
from iirp.messages import decode
from iirp.models import SourceBudget, now
from iirp.political import transport
from iirp.political.config import policy
from iirp.sec import contact

from tests.political.test_service import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    no_external_requests,
)

HOUSE = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2025FD.zip"
SENATE = "https://efdsearch.senate.gov/search/"
TEST_CONTACT = "IIRP offline fixture contact@example.invalid"


@pytest.fixture(autouse=True)
def offline_contact(monkeypatch):
    monkeypatch.setattr(contact, "configured", lambda: True)
    monkeypatch.setattr(contact, "user_agent", lambda: TEST_CONTACT)


@pytest.fixture
def clients():
    opened = []
    def build(handler):
        client = transport.Transport()
        headers = client.client.headers
        client.client.close()
        client.client = httpx.Client(transport=httpx.MockTransport(handler), headers=headers)
        opened.append(client)
        return client
    yield build
    for client in opened:
        client.close()


def test_gzip_stream_is_decoded_once_and_preserves_contact_headers(clients):
    payload = b"PK synthetic government index payload"
    encoded = gzip.compress(payload)
    requests = []
    def serve(request):
        requests.append(request)
        # Use a raw stream so the normal httpx streaming decoder handles gzip.
        return httpx.Response(200, headers={"content-type": "application/zip", "content-encoding": "gzip",
                                            "content-length": str(len(encoded))},
                              stream=httpx.ByteStream(encoded))
    client = clients(serve)
    response = client.fetch("GET", HOUSE)
    assert response.content == payload and response.headers["content-type"] == "application/zip"
    assert response.headers.get("content-encoding") is None
    assert int(response.headers["content-length"]) == len(payload)
    assert len(requests) == 1
    assert requests[0].headers["user-agent"] == TEST_CONTACT
    assert requests[0].headers["referer"] == HOUSE
    with session() as s:
        budget = s.get(SourceBudget, "political_house")
        assert budget.failures == 0 and budget.next_allowed_at > now()


def test_advisory_lock_prevents_overlapping_requests_to_the_same_source(clients):
    entered, release = threading.Event(), threading.Event()
    requests = []
    def slow(request):
        requests.append(request)
        entered.set()
        assert release.wait(5), "Synthetic request was not released"
        return httpx.Response(200, content=b"house index")
    first = clients(slow)
    competing = clients(slow)
    independent = clients(lambda request: httpx.Response(200, content=b"senate index"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(first.fetch, "GET", HOUSE)
        try:
            assert entered.wait(5), "Synthetic request did not begin"
            with pytest.raises(ProviderFailure) as failure:
                competing.fetch("GET", HOUSE)
            assert decode(str(failure.value))["code"] == "political.source_busy"
            assert failure.value.source_wait and len(requests) == 1
            # A busy House source does not consume the Senate's independent budget.
            assert independent.fetch("GET", SENATE).content == b"senate index"
        finally:
            release.set()
        assert running.result(timeout=5).content == b"house index"


def test_successful_request_gap_is_shared_between_independent_clients(clients):
    starts = []
    def serve(request):
        starts.append(time.monotonic())
        return httpx.Response(200, content=b"synthetic index")
    first, second = clients(serve), clients(serve)
    first.fetch("GET", HOUSE)
    second.fetch("GET", HOUSE)
    assert len(starts) == 2
    assert starts[1] - starts[0] >= 1
    assert starts[1] - starts[0] >= policy()["collection"]["request_gap_seconds"]


def test_failure_backoff_blocks_requests_and_grows_to_configured_ceiling(clients, monkeypatch):
    requests = []
    def blocked(request):
        requests.append(request)
        return httpx.Response(403, content=b"synthetic refusal")
    first, second = clients(blocked), clients(blocked)
    with pytest.raises(ProviderFailure) as failure:
        first.fetch("GET", HOUSE)
    assert failure.value.status_code == 403 and failure.value.source_wait
    assert decode(str(failure.value))["code"] == "political.http_unavailable"
    with session() as s:
        budget = s.get(SourceBudget, "political_house")
        assert budget.failures == 1
        deadline = budget.next_allowed_at
        assert (deadline - now()).total_seconds() > 899
    with pytest.raises(ProviderFailure) as cooling:
        second.fetch("GET", HOUSE)
    assert decode(str(cooling.value))["code"] == "political.cooldown"
    assert cooling.value.retry_seconds > 899 and len(requests) == 1
    # Advance only the test clock; production budgets, gaps and retry ceilings stay intact.
    for failures in range(2, 9):
        clock = deadline + timedelta(seconds=1)
        monkeypatch.setattr(transport, "now", lambda: clock)
        with pytest.raises(ProviderFailure):
            second.fetch("GET", HOUSE)
        with session() as s:
            budget = s.get(SourceBudget, "political_house")
            assert budget.failures == failures
            deadline = budget.next_allowed_at
            delay = (deadline - clock).total_seconds()
            assert delay >= 900 and delay <= policy()["cadence"]["retry_max_seconds"]
    assert delay == policy()["cadence"]["retry_max_seconds"] and len(requests) == 8


@pytest.mark.parametrize("host", ["whitehouse.gov", "www.whitehouse.gov"])
def test_white_house_requests_use_oge_budget_without_consuming_house_budget(clients, host):
    response = clients(lambda request: httpx.Response(200, content=b"executive disclosure index")).fetch(
        "GET", f"https://{host}/financial-disclosures/")
    assert response.content == b"executive disclosure index"
    with session() as s:
        assert s.get(SourceBudget, "political_oge") is not None
        assert s.get(SourceBudget, "political_house") is None
