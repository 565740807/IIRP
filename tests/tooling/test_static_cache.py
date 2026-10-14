"""Hashed static assets can stay cached while the shell and API remain fresh."""

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient
from iirp.api.app import same_origin


def test_only_successful_assets_receive_long_term_cache_headers():
    application = FastAPI()
    application.middleware("http")(same_origin)

    @application.get("/assets/example-deadbeef.js")
    def asset():
        return Response("example", media_type="application/javascript")

    @application.get("/assets/missing.js", status_code=404)
    def missing():
        return Response(status_code=404)

    @application.get("/")
    def shell():
        return Response("shell", headers={"Cache-Control": "no-cache"})

    @application.get("/api/example")
    def api():
        return Response("data")

    client = TestClient(application)
    asset_response = client.get("/assets/example-deadbeef.js")
    assert asset_response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "cache-control" not in client.get("/assets/missing.js").headers
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/api/example").headers["cache-control"] == "no-store"
