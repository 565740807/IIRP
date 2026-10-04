"""Project-scoped Mac process management; never invokes brew services."""

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["PYTHONPATH"] = str(ROOT / "backend")
RUNTIME = ROOT / "runtime"
PGDATA = RUNTIME / "postgres"
PGPORT = 55481
PYTHON = ROOT / ".venv/bin/python"
PGBIN = Path(os.environ.get("IIRP_PG_BIN", "/opt/homebrew/opt/postgresql@18/bin"))


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], cwd=ROOT, check=True, **kwargs)


def occupied(port):
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def init():
    RUNTIME.mkdir(exist_ok=True, mode=0o700)
    (RUNTIME / "logs").mkdir(exist_ok=True)
    if not (ROOT / ".env").exists():
        password = secrets.token_urlsafe(32)
        config = (
            f"IIRP_DATABASE_URL=postgresql+psycopg://iirp:{password}@127.0.0.1:{PGPORT}/iirp_v1\n"
            "IIRP_PORT=18081\nIIRP_MODE=development\nIIRP_SEC_USER_AGENT=\n"
        )
        fd = os.open(ROOT / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(config)
    from iirp.config import settings
    from sqlalchemy.engine import make_url

    url = make_url(settings().database_url)
    if not (PGDATA / "PG_VERSION").exists():
        password_file = RUNTIME / ".pg-password"
        fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(url.password)
            run(
                [
                    PGBIN / "initdb",
                    "-D",
                    PGDATA,
                    "-U",
                    "iirp",
                    "--auth=scram-sha-256",
                    "--pwfile",
                    password_file,
                    "--encoding=UTF8",
                    "--locale=C",
                ]
            )
            with (PGDATA / "postgresql.conf").open("a") as stream:
                stream.write(
                    f"\nlisten_addresses='127.0.0.1'\nport={PGPORT}\nunix_socket_directories=''\nmax_connections=30\nshared_buffers='128MB'\ntimezone='UTC'\n"
                )
        finally:
            password_file.unlink(missing_ok=True)
    pg_start()
    import psycopg
    from psycopg import sql

    with psycopg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname="postgres",
        autocommit=True,
    ) as conn:
        if not conn.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (url.database,)
        ).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(url.database)))
    print("独立 PostgreSQL 已就绪；本地私有配置保存在 .env。")


def pg_start():
    status = subprocess.run(
        [str(PGBIN / "pg_ctl"), "-D", str(PGDATA), "status"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if status.returncode:
        if occupied(PGPORT):
            raise RuntimeError(f"端口 {PGPORT} 已被其他服务占用。未改动该服务。")
        run([PGBIN / "pg_ctl", "-D", PGDATA, "-l", RUNTIME / "logs/postgres.log", "-w", "start"])


def pid_running(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def owns(pid, component):
    out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    marker = "iirp.api:app" if component == "web" else "iirp.worker"
    return marker in out.stdout and str(PYTHON) in out.stdout


def start():
    from iirp.config import settings

    init()
    run([PYTHON, "-m", "alembic", "upgrade", "head"])
    if not (ROOT / "frontend/dist/index.html").exists():
        raise RuntimeError("请先运行 npm ci && npm run build（frontend 目录）。")
    log_config = RUNTIME / "web-log-config.json"
    log_config.write_text(
        json.dumps(
            {
                "version": 1,
                "disable_existing_loggers": False,
                "formatters": {"default": {"format": "%(asctime)s %(levelname)s %(message)s"}},
                "handlers": {
                    "rotating": {
                        "class": "logging.handlers.RotatingFileHandler",
                        "filename": str(RUNTIME / "logs/web-events.log"),
                        "maxBytes": 5 * 1024**2,
                        "backupCount": 4,
                        "formatter": "default",
                        "encoding": "utf-8",
                    }
                },
                "loggers": {
                    "uvicorn": {"handlers": ["rotating"], "level": "INFO", "propagate": False},
                    "uvicorn.access": {
                        "handlers": ["rotating"],
                        "level": "INFO",
                        "propagate": False,
                    },
                    "uvicorn.error": {
                        "handlers": ["rotating"],
                        "level": "INFO",
                        "propagate": False,
                    },
                },
            }
        )
    )
    for name, args in [
        ("worker", ["-m", "iirp.worker"]),
        (
            "web",
            [
                "-m",
                "uvicorn",
                "iirp.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(settings().port),
                "--log-config",
                str(log_config),
            ],
        ),
    ]:
        pidfile = RUNTIME / f"{name}.pid"
        if pidfile.exists() and owns(int(pidfile.read_text()), name):
            continue
        if name == "web" and occupied(settings().port):
            raise RuntimeError("网页端口已被占用；没有停止其他服务。")
        with (RUNTIME / f"logs/{name}.log").open("a") as log:
            proc = subprocess.Popen(
                [str(PYTHON), *args],
                cwd=ROOT,
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        pidfile.write_text(str(proc.pid))
    print(f"IIRP V1: http://127.0.0.1:{settings().port}")


def stop(keep_db=False):
    for name in ("web", "worker"):
        file = RUNTIME / f"{name}.pid"
        if not file.exists():
            continue
        pid = int(file.read_text())
        if owns(pid, name):
            os.kill(pid, signal.SIGTERM)
            for _ in range(100):
                if not pid_running(pid):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError(f"{name} 尚未退出；请查看日志。")
        file.unlink(missing_ok=True)
    if not keep_db and (PGDATA / "postmaster.pid").exists():
        run([PGBIN / "pg_ctl", "-D", PGDATA, "-m", "fast", "-w", "stop"])


def status():
    for name in ("web", "worker"):
        path = RUNTIME / f"{name}.pid"
        print(name, "running" if path.exists() and owns(int(path.read_text()), name) else "stopped")
    run([PGBIN / "pg_ctl", "-D", PGDATA, "status"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "start", "stop", "status", "restart"])
    args = parser.parse_args()
    if args.command == "restart":
        stop(keep_db=True)
        start()
    else:
        globals()[args.command]()
