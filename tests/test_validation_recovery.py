"""The restore gate must exercise real frozen row keys and nonempty pagination."""

import copy
from types import SimpleNamespace

import pytest

from scripts.validation.recovery import snapshots


class FrozenClient:
    def __init__(self, corruption=None):
        self.corruption = corruption
        self.requests = []
        self.rows = [
            {"key": key, "points": [{"date": "2024-06-03"}, {"date": "2024-06-17"}],
             "sources": [{"evidence_note": "complete frozen source"}]}
            for key in ("first", "second", "third")
        ]

    def get(self, url, params=None):
        self.requests.append((url, params))
        if "/export?" in url:
            return SimpleNamespace(status_code=200, content=b"complete frozen export")
        if url.endswith("/event-overlaps"):
            key = params["event_key"]
            expected = [row["key"] for row in self.rows if row["key"] != key]
            offset = int(params.get("cursor", 0))
            item = expected[0 if self.corruption == "duplicate" else offset]
            page = {"result_id": "result", "event_key": key, "total": 2,
                    "summary": {"total": 2, "preview_event_ids": expected},
                    "offset": offset, "items": [{"event_key": item}],
                    "next_cursor": "1" if offset == 0 else None}
            if self.corruption == "total":
                page["total"] = 99
            return SimpleNamespace(status_code=200, json=lambda: copy.deepcopy(page))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"result_id": "result", "data": {"rows": copy.deepcopy(self.rows)}},
        )


def test_recovery_snapshots_cover_nonempty_complete_frozen_pages():
    client = FrozenClient()
    captured = snapshots(client, "analysis", "result")
    assert set(captured) == {"json", "csv", "frozen-data", "pages-first", "pages-second", "pages-third"}
    assert len([url for url, _params in client.requests if url.endswith("/event-overlaps")]) == 6


@pytest.mark.parametrize("corruption", ["duplicate", "total"])
def test_recovery_snapshots_reject_bad_pages(corruption):
    with pytest.raises(AssertionError):
        snapshots(FrozenClient(corruption), "analysis", "result")
