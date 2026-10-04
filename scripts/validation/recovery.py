"""Transfer a real synthetic backup between independently configured PG instances.

Source and restore are separate processes. The orchestrator selects a different
Compose project/volume/PG server for restore, and never passes main credentials.
"""

import argparse
import copy
import hashlib
import json
import os
import shutil
import sys
import uuid
from contextlib import closing
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))


def snapshots(client, analysis, result):
    endpoints = {
        "json": f"/api/v1/events/analyses/{analysis}/export?result_id={result}&format=json",
        "csv": f"/api/v1/events/analyses/{analysis}/export?result_id={result}&format=csv",
    }
    output = {}
    for name, url in endpoints.items():
        response = client.get(url)
        assert response.status_code == 200
        output[name] = response.content
    view = client.get(
        f"/api/v1/events/analyses/{analysis}?result_id={result}&include_freshness=false"
    )
    assert view.status_code == 200 and view.json()["result_id"] == result
    output["frozen-data"] = json.dumps(view.json()["data"], sort_keys=True).encode()
    rows = view.json()["data"]["rows"]
    keys = [row["key"] for row in rows]
    assert len(keys) >= 3 and len(set(keys)) == len(keys)
    assert all(row["points"] for row in rows), "Recovery needs actual frozen windows"
    for row in rows:
        key = row["key"]
        # Independently enumerate this small fixture's closed intervals in its
        # frozen order; comparing two equally broken pagers would be insufficient.
        expected = [
            other["key"] for other in rows
            if other["key"] != key
            and other["points"][0]["date"] <= row["points"][-1]["date"]
            and other["points"][-1]["date"] >= row["points"][0]["date"]
        ]
        assert len(expected) >= 2, "Recovery must traverse nonempty multiple pages"
        items, cursor = [], None
        seen_cursors = set()
        while True:
            params = {"event_key": key, "limit": 1, **({"cursor": cursor} if cursor else {})}
            r = client.get(
                f"/api/v1/analyses/{analysis}/results/{result}/event-overlaps", params=params
            )
            assert r.status_code == 200
            page = r.json()
            assert page["result_id"] == result and page["event_key"] == key
            assert page["total"] == page["summary"]["total"] == len(expected)
            assert page["summary"]["preview_event_ids"] == expected[:10]
            assert page["offset"] == len(items) and len(page["items"]) == 1
            items.extend(page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                break
            assert cursor not in seen_cursors, "Pagination cursor must advance"
            seen_cursors.add(cursor)
        actual = [item["event_key"] for item in items]
        assert actual == expected and len(set(actual)) == len(actual)
        output["pages-" + key] = json.dumps(items, sort_keys=True).encode()
    return output


def source(out):
    from fastapi.testclient import TestClient
    from iirp.api import app
    from iirp.business_worker import execute_business
    from iirp.db import session
    from iirp.event_service import confirm_import, create_analysis, get_analysis
    from iirp.models import SourceObject
    from iirp.operation_pool import OperationPool
    from iirp.queue import claim, create_job
    from iirp.storage import save_object
    from test_event_contracts import document
    from test_event_service import confirmation, preview
    from test_lifecycle import clean_lifecycle, lifecycle_database, seed_prices, seed_security
    from test_shared_compute import RealRunner, plan

    from scripts import backup
    from scripts.validation.identity import disable_sources, verified_identity

    out.mkdir(parents=True, exist_ok=False)
    db = lifecycle_database.__wrapped__()
    next(db)
    clean = clean_lifecycle.__wrapped__(out / "source-runtime")
    try:
        next(clean)
        disable_sources()
        security = seed_security()
        seed_prices(security, date(2024, 5, 1), date(2024, 7, 31))
        payload = document()
        template = payload["events"][0]
        payload["events"] = []
        for number, day in enumerate(("2024-06-10", "2024-06-11", "2024-06-12")):
            event = copy.deepcopy(template)
            event.update(client_event_id=f"restore-event-{number}", event_date=day,
                         event_name=f"Synthetic recovery event {number}",
                         notes=f"Complete retained note {number}; unknown != zero")
            payload["events"].append(event)
        payload["coverage"][0]["event_ids"] = [
            event["client_event_id"] for event in payload["events"]
        ]
        confirmation_payload = confirmation(preview(payload))
        for review in confirmation_payload["reviews"]:
            review["date_verified"] = True
        collection = confirm_import(confirmation_payload)
        created = create_analysis(collection["set_id"], {
            "request_id": str(uuid.uuid4()), "version": 1, "cutoff_date": "2025-01-01",
        })
        analysis = created["analysis_id"]
        plan(analysis)
        with closing(OperationPool()) as pool:
            while job := claim({"event_compute", "research_compute"}):
                execute_business(job, runner=RealRunner(pool))
        view = get_analysis(analysis)
        assert view["result_id"]
        obj = save_object(b"Synthetic retained raw source; unknown != zero", "text/plain")
        with session() as s, s.begin():
            s.add(SourceObject(**obj))
        create_job("fixture_check", {"synthetic": True})
        stale = claim({"fixture_check"})
        assert stale is not None
        sibling, _reused = create_job("fixture_check", {"synthetic": True, "independent_control": True})
        with TestClient(app, headers={"X-IIRP-Client": "web"}) as client:
            before = snapshots(client, analysis, view["result_id"])
        (out / "exports").mkdir()
        for name, payload in before.items():
            (out / "exports" / name).write_bytes(payload)
        directory = backup.backup()
        shutil.copytree(directory, out / "transfer")
        (out / "source.json").write_text(
            json.dumps(
                {
                    **verified_identity(),
                    "analysis": analysis,
                    "result": view["result_id"],
                    "stale_job": {
                        "id": stale.id,
                        "kind": stale.kind,
                        "lease_token": str(stale.lease_token),
                        "control_version": stale.control_version,
                    },
                    "independent_job": sibling.id,
                    "exports": {
                        key: hashlib.sha256(value).hexdigest() for key, value in before.items()
                    },
                    "source_sha256": obj["sha256"],
                    "pass": True,
                },
                indent=2,
            )
        )
    finally:
        clean.close()
        db.close()


def restore(source_dir, out, verify_only=False):
    from fastapi.testclient import TestClient
    from iirp.api import app
    from iirp.config import settings
    from iirp.db import engine, session
    from iirp.models import Job, SourceObject
    from sqlalchemy.engine import make_url

    from scripts import backup
    from scripts.validation.identity import verified_identity

    target = verified_identity()
    before = json.loads((source_dir / "source.json").read_text())
    assert target["validation_id"] != before["validation_id"], "Separate restore project required"
    if not verify_only:
        out.mkdir(parents=True, exist_ok=False)
        settings().runtime_dir = out / "target-runtime"
        directory = settings().runtime_dir / "backups" / "imported-synthetic"
        shutil.copytree(source_dir / "transfer", directory)
        report = backup.restore_verify(directory)
        (out / "restore.json").write_text(json.dumps(report, indent=2))
    else:
        report = json.loads((out / "restore.json").read_text())
    url = make_url(settings().database_url).set(database=report["database"])
    engine().dispose()
    engine.cache_clear()
    os.environ["IIRP_DATABASE_URL"] = url.render_as_string(hide_password=False)
    os.environ["IIRP_RUNTIME_DIR"] = report["runtime_directory"]
    settings.cache_clear()
    verified_identity()
    with TestClient(app, headers={"X-IIRP-Client": "web"}) as client:
        after = snapshots(client, before["analysis"], before["result"])
    assert {key: hashlib.sha256(value).hexdigest() for key, value in after.items()} == before[
        "exports"
    ]
    with session() as s:
        job = s.get(Job, before["stale_job"]["id"])
        assert job.status == "PAUSED" and job.lease_token is None and job.lease_until is None
        assert job.requested_action == "pause"
        sibling = s.get(Job, before["independent_job"])
        assert sibling.status == "PAUSED" and sibling.requested_action == "pause"
        assert sibling.lease_token is None and sibling.lease_until is None
        obj = s.get(SourceObject, before["source_sha256"])
        assert (
            hashlib.sha256((settings().runtime_dir / obj.relative_path).read_bytes()).hexdigest()
            == obj.sha256
        )
    # Resume only the synthetic fixture job, prove that the sibling stays paused,
    # then acknowledge its new pause before returning to the safe restored state.
    from types import SimpleNamespace

    from iirp.queue import claim, control, fenced

    stale = SimpleNamespace(**before["stale_job"])
    assert not fenced(stale, status="SUCCEEDED")
    control(stale.id, "resume")
    replacement = claim({"fixture_check"})
    assert replacement is not None and replacement.id == stale.id
    assert str(replacement.lease_token) != stale.lease_token
    assert replacement.control_version > stale.control_version
    assert not fenced(stale, status="SUCCEEDED")
    with session() as s:
        sibling = s.get(Job, before["independent_job"])
        assert sibling.status == "PAUSED" and sibling.lease_token is None
    control(replacement.id, "pause")
    assert not fenced(replacement)
    with session() as s:
        job = s.get(Job, replacement.id)
        assert job.status == "PAUSED" and job.requested_action == "pause"
        assert job.lease_token is None and job.lease_until is None
    (out / ("restart-verification.json" if verify_only else "verification.json")).write_text(
        json.dumps(
            {
                "pass": True,
                "database": report["database"],
                "exports": before["exports"],
                "stale_fence_rejected": True,
                "restored_jobs_paused": True,
                "independent_resume_pause_verified": True,
                "nonempty_frozen_pagination_verified": True,
            },
            indent=2,
        )
    )


def main():
    from scripts.validation.identity import configured_identity

    configured_identity()
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["source", "restore", "verify"])
    parser.add_argument("output", type=Path)
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    if args.mode == "source":
        source(args.output)
    else:
        assert args.source
        restore(args.source, args.output, args.mode == "verify")


if __name__ == "__main__":
    main()
