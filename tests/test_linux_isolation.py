"""Compose overrides must not accidentally select the existing installation."""

import pytest

from scripts import linux


@pytest.fixture
def isolated_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(linux, "ROOT", tmp_path)
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy/.env").write_text("synthetic-default-placeholder")
    (tmp_path / "private.env").write_text("synthetic-placeholder")
    for name in [
        "IIRP_COMPOSE_PROJECT",
        "IIRP_COMPOSE_ENV_FILE",
        "IIRP_HTTP_PORT",
        "IIRP_DB_NAME",
        "IIRP_APP_IMAGE",
        "IIRP_VALIDATION_ID",
    ]:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_existing_default_selection_kept(isolated_profile):
    command = linux.compose()
    assert command[:4] == ["docker", "compose", "-p", "iirp2"]
    assert str(isolated_profile / "deploy/.env") in command


def profile(monkeypatch):
    for key, value in {
        "IIRP_COMPOSE_PROJECT": "iirp-h-test",
        "IIRP_COMPOSE_ENV_FILE": "private.env",
        "IIRP_HTTP_PORT": "18897",
        "IIRP_DB_NAME": "iirp_v1_test_h",
    }.items():
        monkeypatch.setenv(key, value)


def test_isolated_project_owns_distinct_image_and_profile(isolated_profile, monkeypatch):
    import os

    profile(monkeypatch)
    monkeypatch.setenv("IIRP_APP_IMAGE", "original-image:must-not-touch")
    monkeypatch.setenv("IIRP_VALIDATION_ID", "a" * 32)
    command = linux.compose()
    assert command[3] == "iirp-h-test"
    assert str(isolated_profile / "private.env") in command
    assert os.environ["IIRP_APP_IMAGE"] == "iirp-h-test-app:local"
    assert str(isolated_profile / "deploy/validation.compose.yaml") in command


@pytest.mark.parametrize(
    "key,value",
    [
        ("IIRP_COMPOSE_PROJECT", "../iirp2"),
        ("IIRP_COMPOSE_ENV_FILE", "deploy/.env"),
        ("IIRP_HTTP_PORT", "18081"),
        ("IIRP_HTTP_PORT", "1"),
        ("IIRP_HTTP_PORT", ""),
        ("IIRP_DB_NAME", "iirp_v1"),
        ("IIRP_VALIDATION_ID", "invalid"),
    ],
)
def test_partial_or_unsafe_overrides_refused_before_docker(
    isolated_profile, monkeypatch, key, value
):
    profile(monkeypatch)
    monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit):
        linux.compose()


def test_validation_cannot_select_original_project(isolated_profile, monkeypatch):
    monkeypatch.setenv("IIRP_VALIDATION_ID", "a" * 32)
    with pytest.raises(SystemExit):
        linux.compose()
