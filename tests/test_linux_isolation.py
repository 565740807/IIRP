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


def test_backend_tests_use_a_throwaway_tmpfs_postgres_and_remove_it(isolated_profile, monkeypatch):
    (isolated_profile / "deploy/compose.yaml").write_text(
        "services:\n  postgres:\n    image: postgres:18.6-bookworm@sha256:" + "0" * 64 + "\n")
    monkeypatch.setattr(linux, "_app_image", lambda: "iirp-v1-app:synthetic")
    commands, cleanup = [], []
    monkeypatch.setattr(linux, "run", lambda args: commands.append([str(a) for a in args]))

    def fake_subprocess(args, **_kwargs):
        cleanup.append(args)
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr(linux.subprocess, "run", fake_subprocess)
    linux.isolated_tests(["python", "-m", "pytest"])
    network = commands[0][-1]
    assert commands[0][:3] == ["docker", "network", "create"]
    server = commands[1]
    assert "--tmpfs" in server and server[server.index("--tmpfs") + 1].startswith("/var/lib/postgresql")
    assert server[server.index("--network") + 1] == network
    tests = commands[2]
    assert tests[tests.index("--network") + 1] == network
    url = next(value for value in tests if value.startswith("IIRP_DATABASE_URL="))
    assert f"@{network}-pg:5432/iirp_v1_test_check" in url
    # Never the Compose project's database service or network.
    assert not any("compose" in part for command in commands for part in command)
    assert ["docker", "rm", "-f", network + "-pg"] in cleanup
    assert ["docker", "network", "rm", network] in cleanup
