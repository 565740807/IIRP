"""Behavioral checks for the synthetic-only orchestration boundary."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts.validation import identity


def test_test_identity_route_precedes_spa_without_changing_product_schema():
    app = FastAPI()

    @app.get("/{path:path}")
    def spa(path):
        return {"spa": path}

    expected = {"database": "iirp_v1_test_synthetic", "synthetic": True}
    identity.add_test_route(app, "/__iirp_test_identity__", lambda: expected)
    with TestClient(app) as client:
        assert client.get("/__iirp_test_identity__").json() == expected
        assert client.get("/analysis").json() == {"spa": "analysis"}
        assert "/__iirp_test_identity__" not in client.get("/openapi.json").json()["paths"]


@pytest.mark.parametrize(
    "database,token",
    [("iirp_v1", "a" * 32), ("iirp_v1_test_safe", ""), ("iirp_v1_test_safe", "bad")],
)
def test_bad_identity_refused_before_database(monkeypatch, database, token):
    monkeypatch.setenv("IIRP_VALIDATION_ID", token)
    monkeypatch.setattr(
        identity,
        "settings",
        lambda: SimpleNamespace(database_url="postgresql://test:test@localhost/" + database),
    )

    def forbidden():
        raise AssertionError("No connection permitted")

    monkeypatch.setattr(identity, "session", forbidden)
    with pytest.raises(RuntimeError):
        identity.verified_identity()


def test_namespace_collision_does_not_acquire_cleanup_ownership(tmp_path, monkeypatch):
    from scripts import validate

    run = validate.Run(tmp_path / "output")
    profile = {"project": "iirp-h-synthetic", "env": {}, "started": False}
    monkeypatch.setattr(
        validate, "run_process", lambda *a, **kw: SimpleNamespace(stdout=b"existing")
    )
    with pytest.raises(RuntimeError, match="refusing reuse"):
        run.start(profile, "install")
    assert not profile["started"] and not profile.get("owned_images")


def test_preflight_failure_cleanup_has_no_external_side_effects(tmp_path, monkeypatch):
    from scripts import validate

    run = validate.Run(tmp_path / "output")

    def forbidden(*a, **kw):
        raise AssertionError("No resources created, so no Docker cleanup allowed")

    monkeypatch.setattr(validate, "run_process", forbidden)
    assert run.cleanup() == []


def test_profile_creates_private_configuration_and_no_default_resources(tmp_path, monkeypatch):
    import stat

    from scripts import validate

    monkeypatch.setattr(validate, "ROOT", tmp_path)
    monkeypatch.setattr(validate, "free_port", lambda: 18897)
    run = validate.Run(tmp_path / "output")
    profile = run.profile()
    assert stat.S_IMODE(profile["config"].stat().st_mode) == 0o600
    assert profile["env"]["IIRP_DB_NAME"].startswith("iirp_v1_test_")
    assert profile["project"] != "iirp2" and not profile["started"]
    assert len(profile["env"]["IIRP_VALIDATION_ID"]) == 32
    assert run.cleanup() == [] and not profile["config"].exists()
