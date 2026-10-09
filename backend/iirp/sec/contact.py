"""SEC contact (the name and e-mail SEC asks for in the User-Agent) and where it comes from.

``IIRP_SEC_USER_AGENT`` in ``deploy/.env`` wins when it is set; otherwise the name and
e-mail saved from the web page (``user_preferences.values["sec_contact"]``) are used.
The saved value is read through a short cache so the worker picks up a change within
seconds without a restart, and without a database read for every SEC request.
"""

import re
import threading
import time

from iirp.config import settings
from iirp.db import session
from iirp.messages import UserError
from iirp.models import CollectionStrategy, Preferences, now

# The placeholder an older deploy/.env.example shipped; it counts as "not set".
TEMPLATE_USER_AGENTS = frozenset({"IIRP contact@example.invalid"})
KEY = "sec_contact"
CACHE_SECONDS = 5

# RFC 2606 names (and subdomains) can never reach a real mailbox.
RESERVED_TLDS = frozenset({"test", "invalid", "localhost", "example"})
RESERVED_DOMAINS = frozenset({"example.com", "example.org", "example.net"})
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
# Header-safe: printable ASCII, no "@" so the e-mail stays the only address.
NAME = re.compile(r"[A-Za-z0-9 .,'&()/_-]{1,80}")

_lock = threading.Lock()
_cached = (0.0, None)


def _reserved(domain):
    labels = domain.lower().rstrip(".").split(".")
    return labels[-1] in RESERVED_TLDS or ".".join(labels[-2:]) in RESERVED_DOMAINS


def contact_ok(value):
    """True when the User-Agent carries a contact address SEC can actually reach."""
    value = (value or "").strip()
    if not value or value in TEMPLATE_USER_AGENTS:
        return False
    return any(not _reserved(match.group(1)) for match in EMAIL.finditer(value))


def _from_config():
    value = (settings().sec_user_agent or "").strip()
    return "" if value in TEMPLATE_USER_AGENTS else value


def saved_contact():
    """The name and e-mail saved from the web page, or None (cached for a few seconds)."""
    global _cached
    with _lock:
        loaded_at, value = _cached
        if time.monotonic() - loaded_at < CACHE_SECONDS:
            return value
    with session() as s:
        row = s.get(Preferences, 1)
        value = (row.values or {}).get(KEY) if row else None
    with _lock:
        _cached = (time.monotonic(), value)
    return value


def forget_cached():
    global _cached
    with _lock:
        _cached = (0.0, None)


def user_agent():
    """The User-Agent sent to SEC: deploy/.env first, then the saved contact, else ''."""
    configured = _from_config()
    if configured:
        return configured
    contact = saved_contact()
    return f"{contact['name']} {contact['email']}" if contact else ""


def configured():
    return contact_ok(user_agent())


def state():
    source = "config_file" if _from_config() else "web" if saved_contact() else "none"
    contact = saved_contact() if source == "web" else None
    return {
        "configured": configured(),
        "source": source,
        "editable": source != "config_file",
        "name": contact["name"] if contact else None,
        "email": contact["email"] if contact else None,
    }


def save(name, email):
    """Validate and save the contact; SEC updates start on the next scheduler tick."""
    if _from_config():
        raise UserError("sec.contact_from_config")
    name, email = " ".join((name or "").split()), (email or "").strip()
    if not NAME.fullmatch(name):
        raise UserError("sec.contact_name_invalid")
    match = EMAIL.fullmatch(email)
    if not match:
        raise UserError("sec.contact_email_invalid")
    if _reserved(match.group(1)):
        raise UserError("sec.contact_email_reserved", domain=match.group(1).lower())
    from iirp.jobs.batches import defaults

    with session() as s, s.begin():
        defaults(s)
        row = s.get(Preferences, 1, with_for_update=True)
        row.values = {**row.values, KEY: {"name": name, "email": email}}
        row.version += 1
        policy = s.get(CollectionStrategy, "sec", with_for_update=True)
        if policy.enabled:
            # Without a contact the scheduler idles SEC for 5 minutes; start now instead.
            policy.next_run_at = now()
    forget_cached()
    return state()
