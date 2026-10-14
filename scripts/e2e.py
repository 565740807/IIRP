"""Browser smoke orchestration in locked Docker tools or the CI uv environment."""

import argparse
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
CLOCKS = {
    "intraday": "2026-10-09T15:00:00+00:00",
    "after-close": "2026-10-09T21:00:00+00:00",
    "weekend": "2026-10-10T16:00:00+00:00",
}


def output_directory():
    path = Path(os.environ.get("IIRP_E2E_OUTPUT_DIR") or tempfile.mkdtemp(prefix="iirp-e2e-"))
    path = path.resolve()
    if path.is_relative_to(ROOT):
        raise SystemExit("E2E artifacts must be stored outside the repository")
    path.mkdir(parents=True, exist_ok=True)
    print(f"Browser logs, traces, screenshots and measurements: {path}", flush=True)
    return path


def run(command, **kwargs):
    return subprocess.run(command, check=True, **{"cwd": ROOT, **kwargs})


def interrupted(_signum, _frame):
    raise KeyboardInterrupt


def browser_command(scenario):
    # Measure before parallel smoke traffic so both bundle versions get the same
    # cold-browser protocol. Independent language projects then share read-only
    # fixtures and write different watchlist codes.
    measure = "npx --no-install playwright test e2e/performance.spec.ts --project=en --workers=1 && "
    return (measure if scenario == "intraday" else "") + (
        "IIRP_E2E_SKIP_PERFORMANCE=1 exec npx --no-install playwright test")


def docker(app_image, postgres_image, frontend):
    output = output_directory()
    frontend(["npm", "run", "build"])
    name = "iirp-e2e-" + secrets.token_hex(6)
    password = secrets.token_hex(16)
    database = "iirp_v1_test_e2e"
    image = (ROOT / "deploy/playwright-image.txt").read_text().strip()
    # Synchronize dependencies before entering the network with no external route.
    run(["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
         "--entrypoint", "sh", "-v", f"{ROOT}:/workspace", "-w", "/workspace",
         "-e", "UV_PROJECT_ENVIRONMENT=/workspace/.tools/linux-python",
         "-e", "UV_CACHE_DIR=/workspace/.tools/linux-uv-cache", app_image,
         "-c", "uv sync --frozen --no-editable"])
    signal.signal(signal.SIGTERM, interrupted)
    run(["docker", "network", "create", "--internal", name])
    try:
        run(["docker", "run", "-d", "--rm", "--name", name + "-pg", "--network", name,
             "--tmpfs", "/var/lib/postgresql:rw,size=2g", "--shm-size", "256m",
             "-e", "POSTGRES_USER=iirp", "-e", "POSTGRES_PASSWORD=" + password,
             "-e", "POSTGRES_DB=" + database, postgres_image,
             "postgres", "-c", "fsync=off", "-c", "synchronous_commit=off"])
        for _ in range(120):
            if subprocess.run(["docker", "exec", name + "-pg", "pg_isready", "-U", "iirp",
                               "-d", database], capture_output=True).returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise SystemExit("Temporary PostgreSQL did not become ready")
        for scenario, clock in CLOCKS.items():
            artifacts = output / scenario
            artifacts.mkdir(exist_ok=True)
            run(["docker", "run", "-d", "--name", name + "-web", "--network", name,
                 "--user", f"{os.getuid()}:{os.getgid()}", "--entrypoint", "sh",
                 "-v", f"{ROOT}:/workspace", "-v", f"{artifacts}:/e2e-output", "-w", "/workspace",
                 "-e", "PYTHONPATH=/workspace:/workspace/backend", "-e", "IIRP_MODE=development",
                 "-e", "IIRP_RUNTIME_DIR=/tmp/iirp-e2e-runtime", "-e", "IIRP_SEC_USER_AGENT=",
                 "-e", f"IIRP_DATABASE_URL=postgresql+psycopg://iirp:{password}@{name}-pg:5432/{database}",
                 app_image, "-c", "exec .tools/linux-python/bin/python -m scripts.e2e_server "
                 f"--clock {clock} --output /e2e-output"])
            try:
                for _ in range(180):
                    if (artifacts / "fixtures.json").exists():
                        probe = subprocess.run(["docker", "exec", name + "-web",
                            ".tools/linux-python/bin/python", "-c",
                            "from urllib.request import urlopen; urlopen('http://127.0.0.1:18081/health/ready', timeout=1)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        if probe.returncode == 0:
                            break
                    if subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name + "-web"],
                                      capture_output=True, text=True).stdout.strip() != "true":
                        raise RuntimeError("Browser web container exited; see web.log")
                    time.sleep(0.5)
                else:
                    raise RuntimeError("Browser fixtures did not become ready")
                run(["docker", "run", "--rm", "--name", name + "-browser",
                     "--network", "container:" + name + "-web", "--shm-size", "1g",
                     "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
                     "-v", f"{ROOT}:/workspace", "-v", f"{artifacts}:/e2e-output",
                     "-w", "/workspace/frontend", "-e", "IIRP_E2E_OUTPUT_DIR=/e2e-output",
                     "-e", "IIRP_E2E_BASE_URL=http://127.0.0.1:18081", image,
                     "sh", "-c", browser_command(scenario)])
            finally:
                with (artifacts / "web.log").open("w") as log:
                    subprocess.run(["docker", "logs", name + "-web"], stdout=log, stderr=log)
                subprocess.run(["docker", "rm", "-f", name + "-web"], capture_output=True)
    finally:
        for suffix in ["browser", "web", "pg"]:
            subprocess.run(["docker", "rm", "-f", name + "-" + suffix], capture_output=True)
        subprocess.run(["docker", "network", "rm", name], capture_output=True)
        print(f"E2E artifacts: {output}", flush=True)


def native():
    """CI owns its isolated PostgreSQL service; this owns every web child process."""
    output = output_directory()
    signal.signal(signal.SIGTERM, interrupted)
    port = int(os.environ.get("IIRP_E2E_PORT", "18082"))
    for scenario, clock in CLOCKS.items():
        artifacts = output / scenario
        artifacts.mkdir(exist_ok=True)
        with (artifacts / "web.log").open("w") as log:
            child = subprocess.Popen([sys.executable, "-m", "scripts.e2e_server", "--clock", clock,
                                      "--port", str(port), "--output", str(artifacts)], cwd=ROOT,
                                     stdout=log, stderr=log)
            try:
                for _ in range(180):
                    if child.poll() is not None:
                        raise RuntimeError(f"Browser web process exited; see {artifacts / 'web.log'}")
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=1) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        pass
                    time.sleep(0.5)
                else:
                    raise RuntimeError("Browser web process did not become ready")
                run(["sh", "-c", browser_command(scenario)], cwd=ROOT / "frontend",
                    env={**os.environ, "IIRP_E2E_OUTPUT_DIR": str(artifacts),
                         "IIRP_E2E_BASE_URL": f"http://127.0.0.1:{port}"})
            finally:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    native()
