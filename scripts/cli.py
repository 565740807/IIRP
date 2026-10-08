"""Implementation of ./iirp: services, tests and dev tools via Docker Compose (Linux and macOS)."""

import argparse
import hashlib
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def selected_project():
    project = os.environ.get("IIRP_COMPOSE_PROJECT", "iirp2")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        raise SystemExit("Invalid IIRP_COMPOSE_PROJECT")
    return project


def env_path():
    env_file = Path(os.environ.get("IIRP_COMPOSE_ENV_FILE", ROOT / "deploy/.env"))
    return env_file if env_file.is_absolute() else ROOT / env_file


def create_env():
    """First start: write the private config from deploy/.env.example (mode 600, random DB password).

    Returns False when the file already exists. Refuses when the project's database
    volume exists, since a new password would not match the one it was created with.
    """
    env_file = env_path()
    if env_file.exists() or env_file.is_symlink():
        return False
    volume = selected_project() + "_postgres-data"
    if subprocess.run(["docker", "volume", "inspect", volume], capture_output=True).returncode == 0:
        raise SystemExit(f"{env_file} is missing but the database volume {volume} exists; restore the "
                         "original file instead of creating a new password")
    text = (ROOT / "deploy/.env.example").read_text()
    text = text.replace("REPLACE_WITH_PRIVATE_URL_SAFE_PASSWORD", secrets.token_urlsafe(32))
    env_file.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as target:
        target.write(text)
    print(f"Created {env_file} (mode 600) with a random database password.")
    return True


def compose():
    """Explicit project selection also isolates images, ports, config and volumes.

    The existing installation keeps its defaults. A non-default project must
    supply a complete isolated profile; partial overrides fail before Docker.
    Never read or print credentials here; Compose owns private env parsing.
    """
    project = selected_project()
    env_file = env_path()
    if env_file.is_symlink() or not env_file.is_file():
        raise SystemExit(f"Missing private configuration {env_file}; run ./iirp start to create it")
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
    return command


def run(args):
    return subprocess.run([str(value) for value in args], cwd=ROOT, check=True)


def backend(command):
    (ROOT / ".tools").mkdir(exist_ok=True)
    # Credentials stay in Compose; this joins the selected project's network.
    # Tests use isolated_tests() instead, never this database server.
    port = os.environ.get("IIRP_DEV_HTTP_PORT")
    if port and (not port.isdigit() or not 1024 <= int(port) <= 65535):
        raise SystemExit("IIRP_DEV_HTTP_PORT 必须是1024—65535的本地端口")
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


def _postgres_image():
    for line in (ROOT / "deploy/compose.yaml").read_text().splitlines():
        if line.strip().startswith("image: postgres:"):
            return line.split("image:", 1)[1].strip()
    raise SystemExit("PostgreSQL image not found in deploy/compose.yaml")


def _app_image():
    # Compose resolves IIRP_APP_IMAGE from the shell or the private env file.
    images = subprocess.run(
        compose() + ["config", "--images"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.split()
    app = [image for image in images if not image.startswith("postgres:")]
    if not app:
        raise SystemExit("Application image not found; run ./iirp build first")
    return app[0]


def isolated_tests(command):
    """Run backend tests against a throwaway PostgreSQL whose data lives in tmpfs.

    Nothing is created on the running instance's database server: the test
    container joins a private network with only this temporary server, and
    both, plus the network, are removed afterwards (also on failure).
    """
    (ROOT / ".tools").mkdir(exist_ok=True)
    name = "iirp-check-" + secrets.token_hex(6)
    password = secrets.token_hex(16)
    database = "iirp_v1_test_check"
    run(["docker", "network", "create", name])
    try:
        run([
            "docker", "run", "-d", "--rm", "--name", name + "-pg", "--network", name,
            "--tmpfs", "/var/lib/postgresql:rw,size=4g", "--shm-size", "256m", "--memory", "6g",
            "-e", "POSTGRES_USER=iirp", "-e", "POSTGRES_PASSWORD=" + password,
            "-e", "POSTGRES_DB=" + database,
            _postgres_image(), "postgres", "-c", "fsync=off", "-c", "synchronous_commit=off",
            "-c", "full_page_writes=off", "-c", "shared_buffers=256MB",
        ])
        for _ in range(120):
            ready = subprocess.run(
                ["docker", "exec", name + "-pg", "pg_isready", "-U", "iirp", "-d", database, "-h", "127.0.0.1"],
                capture_output=True,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise SystemExit("Temporary test PostgreSQL did not become ready")
        return run([
            "docker", "run", "--rm", "--network", name,
            "--user", f"{os.getuid()}:{os.getgid()}", "--entrypoint", "sh",
            "-v", f"{ROOT}:/workspace", "-w", "/workspace",
            "-e", "PYTHONPATH=/workspace/backend",
            "-e", "IIRP_RUNTIME_DIR=/tmp/iirp-check-runtime",
            "-e", "IIRP_MODE=development",
            "-e", "IIRP_PG_BIN=/usr/lib/postgresql/18/bin",
            "-e", f"IIRP_DATABASE_URL=postgresql+psycopg://iirp:{password}@{name}-pg:5432/{database}",
            "-e", "UV_PROJECT_ENVIRONMENT=/workspace/.tools/linux-python",
            "-e", "UV_CACHE_DIR=/workspace/.tools/linux-uv-cache",
            _app_image(),
            "-c", 'uv sync --frozen --no-editable >&2 && exec "$@"', "sh", *command,
        ])
    finally:
        subprocess.run(["docker", "rm", "-f", name + "-pg"], capture_output=True)
        subprocess.run(["docker", "network", "rm", name], capture_output=True)


def frontend(args):
    tools = ROOT / ".tools"
    tools.mkdir(exist_ok=True)
    image = (ROOT / "deploy/Dockerfile").read_text().splitlines()[0].split()[1]
    base = [
        "docker",
        "run",
        "--rm",
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
            "test",
            "python",
            "lint",
            "frontend",
            "backup",
            "restore-verify",
        ],
    )
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    created = args.action in ("init", "start") and create_env()
    selected = compose()
    if args.action in ("init", "start", "restart"):
        run(selected + ["up", "-d", "--build", "--wait"])
        address = subprocess.run(selected + ["port", "web", "18081"], cwd=ROOT, capture_output=True,
                                 text=True).stdout.strip()
        print(f"IIRP is running at http://{address or '127.0.0.1:18081'}")
        if created:
            print("Next: open it and add your SEC contact (name and e-mail) from the notice on the home "
                  "page. SEC requires it; Insider filings are not fetched until then.")
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
    elif args.action == "test":
        isolated_tests(["/workspace/.tools/linux-python/bin/python", "-m", "pytest",
                        *(args.args or ["tests", "-q"])])
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
        isolated_tests(
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
