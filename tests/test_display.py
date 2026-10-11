"""Display settings (up/down colors) are saved once per instance and shared by every browser.

Uses a disposable iirp_v1_test_* database.
"""

from iirp.db import session
from iirp.models import Preferences

from tests.jobs.test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_client,
    lifecycle_database,
)


def test_default_is_us_colors(lifecycle_client):  # noqa: F811
    state = lifecycle_client.get("/api/v1/display").json()
    assert state["colors"] == {"preset": "us", "up": None, "down": None}


def test_preset_and_custom_colors_round_trip_and_stay_out_of_preferences(lifecycle_client):  # noqa: F811
    saved = lifecycle_client.put("/api/v1/display", json={"colors": {"preset": "colorblind"}})
    assert saved.status_code == 200
    assert saved.json()["colors"]["preset"] == "colorblind"
    # Another browser reads the same value.
    assert lifecycle_client.get("/api/v1/display").json()["colors"]["preset"] == "colorblind"

    custom = lifecycle_client.put("/api/v1/display", json={"colors": {"preset": "custom", "up": "#1A7F37", "down": "#cf222e"}})
    assert custom.json()["colors"] == {"preset": "custom", "up": "#1a7f37", "down": "#cf222e"}
    # Choosing a preset keeps the custom colors for later.
    back = lifecycle_client.put("/api/v1/display", json={"colors": {"preset": "asia", "up": "#1a7f37", "down": "#cf222e"}})
    assert back.json()["colors"] == {"preset": "asia", "up": "#1a7f37", "down": "#cf222e"}
    assert back.json()["version"] > custom.json()["version"]

    with session() as s:
        row = s.get(Preferences, 1)
        assert row.values["display"]["colors"]["preset"] == "asia"
        assert "history_months" in row.values
    assert "display" not in lifecycle_client.get("/api/v1/preferences").json()["values"]


def test_invalid_colors_are_refused_with_message_codes(lifecycle_client):  # noqa: F811
    cases = [
        ({"preset": "custom", "up": "#00aa00"}, "display.custom_colors_required"),
        ({"preset": "custom", "up": "#00aa00", "down": "#00AA00"}, "display.colors_identical"),
        ({"preset": "custom", "up": "green", "down": "#aa0000"}, "display.color_invalid"),
    ]
    for colors, code in cases:
        refused = lifecycle_client.put("/api/v1/display", json={"colors": colors})
        assert refused.status_code == 409, colors
        assert refused.json()["detail"]["code"] == code
    assert lifecycle_client.put("/api/v1/display", json={"colors": {"preset": "pink"}}).status_code == 422
    assert lifecycle_client.get("/api/v1/display").json()["colors"]["preset"] == "us"
