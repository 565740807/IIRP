"""A resumed multi-page delta must account for intermediate groups before removal."""

import copy
import json

from fastapi.testclient import TestClient
from iirp.api import app
from iirp.business_models import FeedRevision
from iirp.config import settings
from iirp.db import session
from iirp.feed_index import publish_current
from iirp.sec_facts import feed
from sqlalchemy import select, text
from test_feed_updates import _add_revision
from test_sec_facts import clean, isolated_database, save  # noqa: F401


def test_resume_frozen_delta_then_remove_intermediate_group(tmp_path, monkeypatch):
    monkeypatch.setattr(settings(), "runtime_dir", tmp_path)
    with session() as s, s.begin():
        database = s.scalar(text("SELECT current_database()"))
        assert database.startswith("iirp_v1_test_")
        save(s)
        base = feed(s, kind="buy")
        template = s.scalar(select(FeedRevision))
        for index in range(25):
            _add_revision(s, template, index)

    with TestClient(app, headers={"X-IIRP-Client": "web"}) as client:
        def read(params):
            response = client.get("/api/v1/feed/updates", params=params)
            assert response.status_code == 200, response.text
            return response.json()

        params = {"session_id": base["session_id"], "include_groups": "true"}
        first = read(params)
        assert len(first["groups"]) == 20 and first["next_cursor"] == "20"
        victim = first["groups"][0]
        with session() as s, s.begin():
            old = s.get(FeedRevision, victim["revision_id"])
            data = copy.deepcopy(old.data)
            data["transactions"] = []
            tombstone = FeedRevision(group_key=old.group_key, issuer_id=old.issuer_id,
                                     accepted_at=old.accepted_at, data=data, match_kinds=[], row_count=0,
                                     transaction_sort_dates=old.transaction_sort_dates)
            s.add(tombstone)
            s.flush()
            publish_current(s, [tombstone.id])

        # Starting again at B would omit a removal for the intermediate group G.
        restarted = read(params)
        assert victim["id"] not in restarted["removed_ids"]
        resumed_params = {**params, "target_session_id": first["target_session_id"],
                          "cursor": first["next_cursor"]}
        resumed = read(resumed_params)
        repeated = read(resumed_params)
        assert repeated == resumed
        assert resumed["target_session_id"] == first["target_session_id"]
        assert len(resumed["groups"]) == 5 and resumed["next_cursor"] is None
        assert len({g["id"] for g in first["groups"] + resumed["groups"]}) == 25
        assert resumed["removed_ids"] == []

        next_params = {"session_id": resumed["target_session_id"]}
        summary = read(next_params)
        assert summary["new_count"] == 1
        removed = read({**next_params, "include_groups": "true"})
        assert removed["removed_ids"] == [victim["id"]] and removed["groups"] == []
        final = read({"session_id": removed["target_session_id"]})
        assert final["new_count"] == 0
        detail = client.get(f'/api/v1/feed/groups/{victim["id"]}',
                            params={"session_id": first["target_session_id"]})
        assert detail.status_code == 200
        assert detail.json()["revision_id"] == victim["revision_id"] and detail.json()["items"]
        print(json.dumps({"database": database, "base": base, "first": first,
                          "resumed": resumed, "summary": summary, "removed": removed,
                          "final": final, "historical_detail": detail.json()}, ensure_ascii=False))
