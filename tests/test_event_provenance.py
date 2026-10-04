"""Event import chronology is server owned and immutable across reviewed revisions."""

from datetime import datetime, timezone

from iirp import event_service as service
from iirp.db import session
from iirp.event_models import EventImportPreview
from test_event_contracts import document
from test_event_service import confirmation, preview
from test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    seed_security,
)


def test_import_observation_is_distinct_from_review_and_survives_revision():
    seed_security()
    original = preview()
    observed = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)
    with session() as s, s.begin():
        s.get(EventImportPreview, original["preview_id"]).created_at = observed
    command = confirmation(original)
    command["reviews"][0]["date_verified"] = True
    saved = service.confirm_import(command)
    before = service.get_set(saved["set_id"], 1)
    row = before["events"][0]
    assert row["first_observed_at"] == observed.isoformat()
    assert row["review"]["confirmed_at"] != row["first_observed_at"]
    payload = document()
    payload["events"][0]["notes"] = "A reviewed source note revision; original event identity remains."
    revised = preview(payload, set_id=saved["set_id"], expected_version=1)
    values = confirmation(revised, expected_version=1, revision_note="Verify another source")
    values["reviews"][0]["date_verified"] = True
    second = service.confirm_import(values)
    after = service.get_set(saved["set_id"], second["version"])
    assert after["events"][0]["first_observed_at"] == observed.isoformat()
    assert service.get_set(saved["set_id"], 1)["events"] == before["events"]
    assert after["events"][0]["review"]["confirmed_at"] != row["review"]["confirmed_at"]
