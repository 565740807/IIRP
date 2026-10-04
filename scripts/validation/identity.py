"""Safety boundary for synthetic tools; never installed as a product API route."""

import os
import re

from iirp.config import settings
from iirp.db import session
from sqlalchemy import text
from sqlalchemy.engine import make_url


def configured_identity():
    identity = os.environ.get("IIRP_VALIDATION_ID", "")
    database = make_url(settings().database_url).database or ""
    if not re.fullmatch(r"[a-f0-9]{32}", identity):
        raise RuntimeError("Missing synthetic validation identity")
    if not re.fullmatch(r"iirp_v1_test_[a-z0-9_]+", database):
        raise RuntimeError("Validation refuses non-test database")
    return identity, database


def verified_identity():
    identity, database = configured_identity()
    with session() as s:
        actual = s.scalar(text("select current_database()"))
    if actual != database:
        raise RuntimeError("Live test database identity mismatch")
    return {
        "validation_id": identity,
        "database": actual,
        "synthetic": True,
        "no_external_provider_calls": True,
    }


def add_test_route(app, path, endpoint, **kwargs):
    """Place fixture-only routes before the product SPA catch-all."""
    app.add_api_route(path, endpoint, include_in_schema=False, **kwargs)
    app.router.routes.insert(0, app.router.routes.pop())


def disable_sources():
    configured_identity()
    from iirp.business_models import CollectionStrategy
    from iirp.lifecycle import defaults
    from iirp.models import Policy
    from iirp.queue import ensure_defaults

    ensure_defaults()
    with session() as s, s.begin():
        defaults(s)
        s.query(Policy).update({"sec_enabled": False, "next_run_at": None})
        for row in s.query(CollectionStrategy):
            row.enabled = False
            row.next_run_at = None
            row.options = {"user_controlled": True, "synthetic_validation": True}
