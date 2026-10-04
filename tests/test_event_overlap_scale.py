"""Dense boundary must pass real frozen publication and bounded API paging."""

import pytest
from fastapi.testclient import TestClient
from iirp.api import app
from test_event_overlap_reads import all_pages, published
from test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401

pytestmark = pytest.mark.slow


def test_1500_dense_events_publish_and_page_all_relations():
    _, _, view, _ = published(1500)
    summary = view["data"]["rows"][0]["overlap"]
    assert summary["total"] == 1499
    assert summary["preview_event_ids"] == [f"event-{i:03}" for i in range(1, 11)]
    with TestClient(app) as client:
        keys = all_pages(client, view, limit=100)
    assert keys == [f"event-{i:03}" for i in range(1, 1500)]
    assert len(set(keys)) == 1499
