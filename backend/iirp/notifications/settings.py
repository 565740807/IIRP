"""Notification settings, kept in ``user_preferences.values.notifications`` (hidden from
``/api/v1/preferences``).

Notifications are off until turned on. Nothing that happened before they were turned on,
or before a person was added to the followed list, is notified. Rule matches look at SEC
open-market purchases only. Phone pushes go to an ntfy server (https://ntfy.sh unless
changed) under a long random topic made the first time pushes are turned on.
"""

import secrets
from datetime import datetime
from urllib.parse import urlsplit

from iirp.messages import UserError
from iirp.models import Preferences, now

PREFERENCES_KEY = "notifications"
DEFAULT_SERVER = "https://ntfy.sh"
SCOPES = ("index_watchlist", "all")
LANGUAGES = ("en", "zh")
DEFAULTS = {
    "enabled": False,
    "enabled_at": None,
    "rules": {"enabled": True, "scope": "index_watchlist", "executive_min": 100000,
              "cluster_people": 3, "cluster_days": 7, "exclude_plans": True},
    "ntfy": {"enabled": False, "server": DEFAULT_SERVER, "topic": None, "language": "en"},
}
LIMITS = {"executive_min": (1000, 100_000_000), "cluster_people": (2, 20), "cluster_days": (1, 30)}


def new_topic():
    """A topic nobody can guess: anyone who knows a topic on a public server can read it."""
    return "iirp-" + secrets.token_urlsafe(24).replace("_", "x").replace("-", "y")


def _merge(saved):
    saved = saved or {}
    return {**DEFAULTS, **{key: value for key, value in saved.items() if key not in ("rules", "ntfy")},
            "rules": {**DEFAULTS["rules"], **(saved.get("rules") or {})},
            "ntfy": {**DEFAULTS["ntfy"], **(saved.get("ntfy") or {})}}


def load(s):
    row = s.get(Preferences, 1)
    return _merge((row.values or {}).get(PREFERENCES_KEY) if row else None)


def enabled_at(settings):
    value = settings.get("enabled_at")
    return datetime.fromisoformat(value) if settings.get("enabled") and value else None


def _server(value):
    text = (value or "").strip().rstrip("/")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.query or parts.fragment or len(text) > 200:
        raise UserError("notifications.server_invalid")
    return text


def update(s, change, *, at=None):
    """Apply a partial change: enabled, rules.<name>, ntfy.enabled / server / language.
    Turning notifications on starts them from now; turning pushes on the first time makes
    the topic."""
    from iirp.jobs.batches import defaults

    at = at or now()
    defaults(s)
    row = s.get(Preferences, 1, with_for_update=True)
    current = _merge((row.values or {}).get(PREFERENCES_KEY))
    if "enabled" in change and change["enabled"] is not None:
        if change["enabled"] and not current["enabled"]:
            current["enabled_at"] = at.isoformat()
        current["enabled"] = bool(change["enabled"])
    for name, value in (change.get("rules") or {}).items():
        if value is None or name not in DEFAULTS["rules"]:
            continue
        if name == "scope" and value not in SCOPES:
            raise UserError("notifications.scope_invalid")
        if name in LIMITS:
            low, high = LIMITS[name]
            if not (isinstance(value, int | float) and low <= value <= high):
                raise UserError("notifications.limit_invalid", low=low, high=high)
            value = int(value)
        current["rules"][name] = value
    ntfy = change.get("ntfy") or {}
    if ntfy.get("server") is not None:
        current["ntfy"]["server"] = _server(ntfy["server"])
    if ntfy.get("language") is not None:
        if ntfy["language"] not in LANGUAGES:
            raise UserError("notifications.language_invalid")
        current["ntfy"]["language"] = ntfy["language"]
    if ntfy.get("enabled") is not None:
        current["ntfy"]["enabled"] = bool(ntfy["enabled"])
        if current["ntfy"]["enabled"] and not current["ntfy"]["topic"]:
            current["ntfy"]["topic"] = new_topic()
    row.values = {**row.values, PREFERENCES_KEY: current}
    row.version += 1
    return current


def regenerate_topic(s):
    from iirp.jobs.batches import defaults

    defaults(s)
    row = s.get(Preferences, 1, with_for_update=True)
    current = _merge((row.values or {}).get(PREFERENCES_KEY))
    current["ntfy"]["topic"] = new_topic()
    row.values = {**row.values, PREFERENCES_KEY: current}
    row.version += 1
    return current
