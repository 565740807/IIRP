"""ASGI app with live synthetic identity; production never imports this module."""

from iirp.api import app

from scripts.validation.identity import add_test_route, configured_identity, verified_identity

configured_identity()
add_test_route(app, "/__iirp_test_identity__", verified_identity)
