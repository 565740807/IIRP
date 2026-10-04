"""Linux development and service entry points, isolated from Mac tooling."""

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def compose():
    """Explicit project selection also isolates images, ports, config and volumes.

    The existing installation keeps its defaults. A non-default project must
    supply a complete isolated profile; partial overrides fail before Docker.
    Never read or print credentials here; Compose owns private env parsing.
    """
    project = os.environ.get("IIRP_COMPOSE_PROJECT", "iirp2")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        raise SystemExit("Invalid IIRP_COMPOSE_PROJECT")
    env_file = Path(os.environ.get("IIRP_COMPOSE_ENV_FILE", ROOT / "deploy/.env"))
    if not env_file.is_absolute():
        env_file = ROOT / env_file
    if env_file.is_symlink() or not env_file.is_file():
        raise SystemExit("Missing private Compose configuration; see deploy/README.zh-CN.md")
    if project != "iirp2":
        if env_file.resolve() == (ROOT / "deploy/.env").resolve():
            raise SystemExit("An isolated project requires its own IIRP_COMPOSE_ENV_FILE")
        port = os.environ.get("IIRP_HTTP_PORT", "")
        database = os.environ.get("IIRP_DB_NAME", "")
        if not port.isdigit() or not 1024 <= int(port) <= 65535 or int(port) == 18081:
            raise SystemExit("An isolated project requires IIRP_HTTP_PORT != 18081 (1024..65535)")
        if not re.fullmatch(r"iirp_v1_test_[a-z0-9_]{1,40}", database):
            raise SystemExit("An isolated project requires an iirp_v1_test_* database")
        # Exported shell values outrank private env-file values in Compose.
        os.environ["IIRP_APP_IMAGE"] = project + "-app:local"
    command = [
        "docker",
        "compose",
        "-p",
        project,
        "--env-file",
        str(env_file),
        "-f",
        str(ROOT / "deploy/compose.yaml"),
    ]
    if identity := os.environ.get("IIRP_VALIDATION_ID"):
        if project == "iirp2" or not re.fullmatch(r"[a-f0-9]{32}", identity):
            raise SystemExit("Validation requires an isolated project and a random 32-hex identity")
        command += ["-f", str(ROOT / "deploy/validation.compose.yaml")]
    return command


def run(args):
    return subprocess.run([str(value) for value in args], cwd=ROOT, check=True)


def validation_labels():
    if not os.environ.get("IIRP_VALIDATION_ID"):
        return []
    return [
        "--label",
        "iirp.validation=" + os.environ["IIRP_VALIDATION_ID"],
        "--label",
        "iirp.validation.project=" + os.environ["IIRP_COMPOSE_PROJECT"],
    ]


def backend(command):
    (ROOT / ".tools").mkdir(exist_ok=True)
    # Credentials stay in Compose. Tests create iirp_v1_test_* databases and
    # disposable runtime files, without sharing the live app-runtime volume.
    port = os.environ.get("IIRP_DEV_HTTP_PORT")
    if port and (not port.isdigit() or not 1024 <= int(port) <= 65535):
        raise SystemExit("IIRP_DEV_HTTP_PORT 必须是1024—65535的本地端口")
    if port == "18081" and os.environ.get("IIRP_VALIDATION_ID"):
        raise SystemExit("Synthetic validation refuses original service port 18081")
    ports = ["-p", f"127.0.0.1:{port}:{port}"] if port else []
    return run(
        compose()
        + [
            "run",
            "--rm",
            "--no-deps",
            "-T",
            *ports,
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--entrypoint",
            "sh",
            "-v",
            f"{ROOT}:/workspace",
            "-w",
            "/workspace",
            "-e",
            "PYTHONPATH=/workspace/backend",
            "-e",
            "IIRP_RUNTIME_DIR=/tmp/iirp-check-runtime",
            "-e",
            "UV_PROJECT_ENVIRONMENT=/workspace/.tools/linux-python",
            "-e",
            "UV_CACHE_DIR=/workspace/.tools/linux-uv-cache",
            "web",
            "-c",
            'uv sync --frozen --no-editable >&2 && exec "$@"',
            "sh",
            *command,
        ]
    )


def frontend(args):
    tools = ROOT / ".tools"
    tools.mkdir(exist_ok=True)
    image = (ROOT / "deploy/Dockerfile").read_text().splitlines()[0].split()[1]
    base = [
        "docker",
        "run",
        "--rm",
        *validation_labels(),
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-e",
        "npm_config_cache=/workspace/.tools/linux-npm-cache",
        "-v",
        f"{ROOT}:/workspace",
        "-w",
        "/workspace/frontend",
        image,
    ]
    fingerprint = hashlib.sha256((ROOT / "frontend/package-lock.json").read_bytes()).hexdigest()
    marker = tools / "linux-frontend-lock"
    if (
        not (ROOT / "frontend/node_modules/.bin/vite").exists()
        or not marker.exists()
        or marker.read_text() != fingerprint
    ):
        run(base + ["npm", "ci"])
        marker.write_text(fingerprint)
    return run(base + args)


def browser(args):
    if not os.environ.get("IIRP_VALIDATION_ID") or len(args) != 2:
        raise SystemExit(
            "browser requires an isolated validation profile, fixture JSON and new output path"
        )
    manifest, output = (Path(value).resolve() for value in args)
    if not manifest.is_file() or output.exists():
        raise SystemExit("Browser fixture must exist; output must be new")
    output.parent.mkdir(parents=True, exist_ok=True)
    image = os.environ["IIRP_COMPOSE_PROJECT"] + "-browser:local"
    run(
        [
            "docker",
            "build",
            "--pull",
            "--no-cache",
            *validation_labels(),
            "-f",
            "deploy/Browser.Dockerfile",
            "-t",
            image,
            ".",
        ]
    )
    # Client has its own cgroup. Host networking only reaches a verified loopback
    # fixture; the JS gate rejects 18081 and mismatched live database identities.
    return run(
        [
            "docker",
            "run",
            "--rm",
            "--init",
            "--name",
            os.environ["IIRP_COMPOSE_PROJECT"] + "-browser-client",
            *validation_labels(),
            "--network",
            "host",
            "--memory",
            "768m",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{manifest}:/fixture.json:ro",
            "-v",
            f"{output.parent}:/evidence",
            image,
            "node",
            "browser/run.cjs",
            "/fixture.json",
            "/evidence/" + output.name,
        ]
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=[
            "init",
            "start",
            "stop",
            "status",
            "restart",
            "build",
            "check",
            "python",
            "lint",
            "frontend",
            "browser",
            "backup",
            "restore-verify",
        ],
    )
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    selected = compose()
    if args.action in ("init", "start", "restart"):
        run(selected + ["up", "-d", "--build", "--wait"])
    elif args.action == "build":
        run(selected + ["build", *args.args, "web"])
    elif args.action == "stop":
        run(selected + ["stop"])
    elif args.action == "status":
        run(selected + ["ps"])
    elif args.action in ("backup", "restore-verify"):
        command = "create" if args.action == "backup" else "verify"
        run(selected + ["exec", "-T", "web", "python", "scripts/backup.py", command, *args.args])
    elif args.action == "python":
        backend(["/workspace/.tools/linux-python/bin/python", *args.args])
    elif args.action == "lint":
        backend(
            [
                "/workspace/.tools/linux-python/bin/ruff",
                *(args.args or ["check", "backend", "tests", "scripts", "migrations"]),
            ]
        )
    elif args.action == "frontend":
        frontend(args.args or ["npm", "run", "build"])
    elif args.action == "browser":
        browser(args.args)
    else:
        backend(
            [
                "/workspace/.tools/linux-python/bin/ruff",
                "check",
                "backend",
                "tests",
                "scripts",
                "migrations",
            ]
        )
        backend(
            [
                "/workspace/.tools/linux-python/bin/python",
                "-m",
                "pytest",
                *(args.args or ["tests", "-q"]),
            ]
        )
        backend(["/workspace/.tools/linux-python/bin/python", "scripts/check_openapi.py"])
        frontend(["npm", "run", "check:api"])
        frontend(["npm", "test"])
        frontend(["npm", "run", "build"])


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
