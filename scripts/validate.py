"""Serial, synthetic-only installation/check/browser/recovery/capacity entry point.

Uses only newly generated Compose projects, private configuration, named volumes
and loopback ports. A failure is recorded and cleanup targets only those projects.
Run from an exported candidate for release validation; never defaults to iirp2.
"""

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
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ValidationInterrupted(RuntimeError):
    pass


def terminate_process(process):
    """Terminate our whole session, including a wrapper's Docker descendants."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    # The session leader can exit before its children. Kill any survivors even
    # when wait() has already returned; a dead leader is not proof of cleanup.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=10)


def run_process(command, *, timeout=30, check=False, capture_output=False, **kwargs):
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process = subprocess.Popen(command, start_new_session=True, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        terminate_process(process)
        raise
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    if port == 18081:
        return free_port()
    return port


class Run:
    def __init__(self, output):
        self.output = output
        output.mkdir(parents=True, exist_ok=False)
        self.steps = []
        self.children = []
        self.profiles = []
        self.frontend_built = False

    def command(self, label, command, env=None, timeout=2400):
        log = self.output / (label + ".log")
        started = time.time()
        result = {"label": label, "command": command, "started_unix": started}
        with log.open("x") as stream:
            try:
                process = run_process(
                    command,
                    cwd=ROOT,
                    env=env,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                )
                result["exit_code"] = process.returncode
            except (
                OSError,
                subprocess.TimeoutExpired,
                KeyboardInterrupt,
                ValidationInterrupted,
            ) as exc:
                result.update(
                    exit_code=124 if isinstance(exc, subprocess.TimeoutExpired) else 126,
                    error=type(exc).__name__,
                )
        result["seconds"] = time.time() - started
        self.steps.append(result)
        (self.output / "steps.json").write_text(json.dumps(self.steps, indent=2))
        if result["exit_code"]:
            raise RuntimeError(f"{label} failed; see its log and steps.json")

    def profile(self):
        identity = secrets.token_hex(16)
        project = "iirp-h-" + identity[:12]
        config = ROOT / ".tools" / (project + ".env")
        if config.parent.is_symlink():
            raise RuntimeError("Validation refuses a symlinked private configuration directory")
        config.parent.mkdir(exist_ok=True)
        env = {
            **os.environ,
            "IIRP_COMPOSE_PROJECT": project,
            "IIRP_COMPOSE_ENV_FILE": str(config),
            "IIRP_HTTP_PORT": str(free_port()),
            "IIRP_DB_NAME": "iirp_v1_test_h_" + identity[:12],
            "IIRP_VALIDATION_ID": identity,
            "IIRP_APP_IMAGE": project + "-app:local",
        }
        # Shell interpolation outranks --env-file. Never inherit credentials,
        # provider contact information or external developer database overrides.
        for key in [
            "IIRP_DEV_HTTP_PORT",
            "IIRP_DATABASE_URL",
            "IIRP_DB_PASSWORD",
            "IIRP_SEC_USER_AGENT",
            "COMPOSE_FILE",
            "COMPOSE_PROJECT_NAME",
            "COMPOSE_PROFILES",
            "COMPOSE_ENV_FILES",
        ]:
            env.pop(key, None)
        with open(config, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            stream.write("IIRP_DB_PASSWORD=" + secrets.token_hex(24) + "\nIIRP_SEC_USER_AGENT=\n")
        stat = config.stat()
        profile = {
            "env": env,
            "config": config,
            "project": project,
            "started": False,
            "config_identity": [stat.st_dev, stat.st_ino],
        }
        self.profiles.append(profile)
        (self.output / (project + "-identity.json")).write_text(
            json.dumps(
                {
                    key: env[key]
                    for key in [
                        "IIRP_COMPOSE_PROJECT",
                        "IIRP_HTTP_PORT",
                        "IIRP_DB_NAME",
                        "IIRP_VALIDATION_ID",
                    ]
                },
                indent=2,
            )
        )
        return profile

    @staticmethod
    def compose(p):
        return [
            "docker",
            "compose",
            "-p",
            p["project"],
            "--env-file",
            str(p["config"]),
            "-f",
            "deploy/compose.yaml",
            "-f",
            "deploy/validation.compose.yaml",
        ]

    def start(self, p, label):
        # Reject an existing namespace before acquiring cleanup ownership.
        for surface, selector in [
            (
                "container",
                ["ps", "-aq", "--filter", "label=com.docker.compose.project=" + p["project"]],
            ),
            (
                "volume",
                [
                    "volume",
                    "ls",
                    "-q",
                    "--filter",
                    "label=com.docker.compose.project=" + p["project"],
                ],
            ),
            (
                "network",
                [
                    "network",
                    "ls",
                    "-q",
                    "--filter",
                    "label=com.docker.compose.project=" + p["project"],
                ],
            ),
            ("image", ["image", "ls", "-q", p["project"] + "-*:local"]),
        ]:
            existing = run_process(["docker", *selector], capture_output=True, check=True)
            if existing.stdout.strip():
                raise RuntimeError(
                    "Generated namespace already owns " + surface + "; refusing reuse"
                )
        # Compose may reuse existing, unlabeled named volumes. Labels alone
        # cannot establish that the exact names we will create are unused.
        for surface, names in [
            ("volume", [p["project"] + "_postgres-data", p["project"] + "_app-runtime"]),
            ("network", [p["project"] + "_default"]),
        ]:
            existing = run_process(
                ["docker", surface, "ls", "--format", "{{.Name}}"],
                capture_output=True,
                text=True,
                check=True,
            )
            if set(names).intersection(existing.stdout.splitlines()):
                raise RuntimeError(
                    "Generated namespace has existing " + surface + "; refusing reuse"
                )
        p["owned_images"] = True
        # Tag belongs only to this run; build without layer reuse before first up.
        self.command(label + "-build", ["./iirp", "build", "--pull", "--no-cache"], p["env"])
        p["started"] = True  # up may partly succeed; finally still removes only this project.
        self.command(label + "-start", ["./iirp", "start"], p["env"])
        self.identity(p)
        self.command(label + "-limits", self.compose(p) + ["ps", "--format", "json"], p["env"])
        containers = run_process(
            self.compose(p) + ["ps", "-q"], env=p["env"], capture_output=True, text=True, check=True
        ).stdout.split()
        for number, container in enumerate(containers):
            self.command(
                label + "-resource-" + str(number),
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{json .HostConfig.Memory}} {{json .HostConfig.MemorySwap}} {{json .State.Pid}} {{json .Image}}",
                    container,
                ],
            )

    def identity(self, p):
        with urlopen(
            f"http://127.0.0.1:{p['env']['IIRP_HTTP_PORT']}/__iirp_test_identity__", timeout=5
        ) as r:
            live = json.load(r)
        assert live == {
            "validation_id": p["env"]["IIRP_VALIDATION_ID"],
            "database": p["env"]["IIRP_DB_NAME"],
            "synthetic": True,
            "no_external_provider_calls": True,
        }

    def path(self, path):
        return "/workspace/" + path.relative_to(ROOT).as_posix()

    def start_child(self, label, args, p, port):
        env = {**p["env"], "IIRP_DEV_HTTP_PORT": str(port)}
        stream = (self.output / (label + ".log")).open("x")
        try:
            child = subprocess.Popen(
                ["./iirp", "python", *args],
                cwd=ROOT,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except BaseException:
            stream.close()
            raise
        record = {"process": child, "stream": stream, "label": label, "started": time.time()}
        self.children.append(record)
        return record

    @staticmethod
    def await_file(record, path, seconds):
        deadline = time.monotonic() + seconds
        while not path.exists():
            if record["process"].poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Fixture did not become ready; see " + record["label"])
            time.sleep(0.25)

    def finish_child(self, record):
        process = record["process"]
        code = process.wait(timeout=90)
        record["finished"] = True
        record["stream"].close()
        self.steps.append(
            {
                "label": record["label"],
                "exit_code": code,
                "seconds": time.time() - record["started"],
            }
        )
        (self.output / "steps.json").write_text(json.dumps(self.steps, indent=2))
        assert code == 0, "Fixture execution or cleanup failed"

    def browser(self, p):
        # Fixtures serve the mounted candidate. The image's /app/frontend/dist
        # is a different directory and cannot stand in for this build.
        if not self.frontend_built:
            self.command(
                "browser-frontend-build", ["./iirp", "frontend", "npm", "run", "build"], p["env"]
            )
            self.frontend_built = True
        out = self.output / "browser-fixture"
        port = free_port()
        child = self.start_child(
            "browser-fixture",
            ["-m", "scripts.validation.fixture", "--output", self.path(out), "--port", str(port)],
            p,
            port,
        )
        try:
            self.await_file(child, out / "fixture.json", 600)
            self.command(
                "browser",
                ["./iirp", "browser", str(out / "fixture.json"), str(self.output / "browser")],
                p["env"],
                timeout=4200,
            )
        finally:
            out.mkdir(exist_ok=True)
            (out / "stop").touch()
            self.finish_child(child)

    def recovery(self, p):
        self.command(
            "migration-and-recovery-regressions",
            [
                "./iirp",
                "python",
                "-m",
                "pytest",
                "tests/test_upgrade_0014.py",
                "tests/test_backup_target_protection.py",
                "tests/test_maintenance_lifecycle.py",
                "tests/test_worker_recovery.py",
                "tests/test_shared_compute.py",
            ],
            p["env"],
        )
        source = self.output / "recovery-source"
        target = self.output / "recovery-target"
        self.command(
            "backup-source",
            ["./iirp", "python", "-m", "scripts.validation.recovery", "source", self.path(source)],
            p["env"],
        )
        other = self.profile()
        self.start(other, "recovery-target")
        # Quiesce the new target's normal test worker while verifying the restored copy.
        self.command("restore-worker-stop", self.compose(other) + ["stop", "worker"], other["env"])
        for mode in ["restore", "verify"]:
            if mode == "verify":
                self.command("restored-stop", ["./iirp", "stop"], other["env"])
                self.command("restored-start", ["./iirp", "start"], other["env"])
                self.identity(other)
            self.command(
                "recovery-" + mode,
                [
                    "./iirp",
                    "python",
                    "-m",
                    "scripts.validation.recovery",
                    mode,
                    self.path(target),
                    "--source",
                    self.path(source),
                ],
                other["env"],
            )

    def capacity(self, p):
        # Eliminate unrelated installed worker/HTTP activity from the isolated PG.
        self.command("capacity-quiesce", self.compose(p) + ["stop", "worker", "web"], p["env"])
        out_a, out_bc = self.output / "capacity-a", self.output / "capacity-bc"
        self.command(
            "capacity-a14",
            [
                "./iirp",
                "python",
                "-m",
                "scripts.validation.capacity",
                "H",
                "sequence",
                self.path(out_a),
            ],
            p["env"],
        )
        port = free_port()
        child = self.start_child(
            "capacity-b10-c100",
            ["-m", "scripts.validation.capacity", "H", "repeat", self.path(out_bc), str(port)],
            p,
            port,
        )
        try:
            self.await_file(child, out_bc / "network-fixture.json", 1800)
            self.command(
                "capacity-real-http-200",
                [
                    sys.executable,
                    "scripts/validation/http_client.py",
                    str(out_bc / "network-fixture.json"),
                    str(self.output / "http-client"),
                ],
                timeout=1500,
            )
        finally:
            out_bc.mkdir(exist_ok=True)
            (out_bc / "stop").touch()
            self.finish_child(child)

    @staticmethod
    def owned_resources(p, surface):
        command = (
            ["docker", "ps", "-aq", "--no-trunc"]
            if surface == "container"
            else ["docker", surface, "ls", "-q"]
        )
        for label in [
            "iirp.validation=" + p["env"]["IIRP_VALIDATION_ID"],
            "iirp.validation.project=" + p["project"],
        ]:
            command += ["--filter", "label=" + label]
        return run_process(command, capture_output=True, text=True, check=True).stdout.split()

    @staticmethod
    def container_stop_groups(p, resources):
        rows = run_process(
            [
                "docker",
                "ps",
                "-a",
                "--no-trunc",
                "--format",
                '{{.ID}}|{{.Label "com.docker.compose.service"}}|{{.Label "com.docker.compose.oneoff"}}',
                "--filter",
                "label=iirp.validation=" + p["env"]["IIRP_VALIDATION_ID"],
                "--filter",
                "label=iirp.validation.project=" + p["project"],
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        groups = [[], [], [], []]
        for row in rows:
            identity, service, oneoff = row.split("|", 2)
            if identity in resources:
                order = (
                    0
                    if oneoff.lower() == "true"
                    else {"worker": 1, "web": 2, "postgres": 3}.get(service, 0)
                )
                groups[order].append(identity)
        return groups

    def cleanup(self):
        errors = []
        remaining = {}
        for child in reversed(self.children):
            try:
                if not child.get("finished"):
                    terminate_process(child["process"])
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(child["label"] + ": " + type(exc).__name__)
            finally:
                child["stream"].close()
        for p in reversed(self.profiles):
            if p.get("owned_images"):
                # Docker workloads outlive their client processes. Select every
                # owned service/one-off/frontend/browser container by two exact
                # labels, then remove only those immutable container IDs.
                for surface in ["container", "network", "volume"]:
                    try:
                        resources = self.owned_resources(p, surface)
                        if resources:
                            if surface == "container":
                                for number, group in enumerate(
                                    self.container_stop_groups(p, resources)
                                ):
                                    if group:
                                        self.command(
                                            p["project"] + "-containers-stop-" + str(number),
                                            ["docker", "stop", "-t", "35", *group],
                                            timeout=90,
                                        )
                                # --rm clients disappear as soon as stop returns.
                                # Retain only original IDs that still exist.
                                resources = sorted(
                                    set(resources).intersection(self.owned_resources(p, surface))
                                )
                                command = ["docker", "rm", *resources]
                            else:
                                command = ["docker", surface, "rm", *resources]
                            if resources:
                                self.command(
                                    p["project"] + "-remove-" + surface, command, timeout=90
                                )
                        left = self.owned_resources(p, surface)
                        remaining[p["project"] + ":" + surface] = left
                        if left:
                            errors.append(p["project"] + ": owned " + surface + " remains")
                    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                        errors.append(p["project"] + ": " + surface + ": " + type(exc).__name__)
                for suffix in ["app", "browser"]:
                    tag = p["project"] + "-" + suffix + ":local"
                    try:
                        inspected = run_process(
                            ["docker", "image", "ls", "-q", tag],
                            capture_output=True,
                            text=True,
                            check=True,
                        )
                        if not inspected.stdout.strip():
                            continue
                        inspected = run_process(
                            [
                                "docker",
                                "image",
                                "inspect",
                                "--format",
                                "{{json .Config.Labels}}",
                                tag,
                            ],
                            capture_output=True,
                            text=True,
                            check=True,
                        )
                        labels = json.loads(inspected.stdout) or {}
                        if (
                            labels.get("iirp.validation") != p["env"]["IIRP_VALIDATION_ID"]
                            or labels.get("iirp.validation.project") != p["project"]
                        ):
                            errors.append("Refusing removal of replaced or unowned image " + tag)
                            continue
                        self.command(
                            p["project"] + "-remove-" + suffix, ["docker", "image", "rm", tag]
                        )
                    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
                        errors.append(tag + ": " + type(exc).__name__)
            try:
                if p["config"].exists() or p["config"].is_symlink():
                    actual = p["config"].lstat()
                    if [actual.st_dev, actual.st_ino] != p["config_identity"]:
                        errors.append("Refusing removal of replaced private configuration")
                    else:
                        p["config"].unlink()
            except OSError as exc:
                errors.append(p["project"] + ": config cleanup: " + type(exc).__name__)
        (self.output / "cleanup.json").write_text(
            json.dumps(
                {
                    "errors": errors,
                    "owned_projects": [p["project"] for p in self.profiles],
                    "remaining_resources": remaining,
                    "pass": not errors,
                },
                indent=2,
            )
        )
        return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=["check", "install", "browser", "recovery", "capacity", "full"],
        default="full",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    requested_output = args.output.absolute()
    output = requested_output.resolve()
    if not output.is_relative_to(ROOT) or any(
        p.is_symlink() for p in [requested_output, *requested_output.parents]
    ):
        parser.error("Output must be a new, non-symlink directory inside the candidate")
    run = Run(output)
    failure = None

    def interrupted(signum, _frame):
        raise ValidationInterrupted("Validation interrupted by signal " + str(signum))

    previous_handlers = {
        sig: signal.signal(sig, interrupted) for sig in [signal.SIGTERM, signal.SIGINT]
    }
    try:
        run.command("docker-preflight", ["docker", "version"], timeout=30)
        run.command("compose-preflight", ["docker", "compose", "version"], timeout=30)
        if (ROOT / "CANDIDATE_MANIFEST.json").exists():
            run.command(
                "candidate-before",
                [sys.executable, "scripts/release_candidates.py", "--verify", "."],
            )
        profile = run.profile()
        run.start(profile, "clean-install")
        if args.suite in {"check", "full"}:
            run.command("full-check", ["./iirp", "check"], profile["env"], timeout=3600)
            run.frontend_built = True
        if args.suite in {"install", "full"}:
            for action in ["seed", "verify"]:
                if action == "verify":
                    run.command("retention-stop", ["./iirp", "stop"], profile["env"])
                    run.command("retention-start", ["./iirp", "start"], profile["env"])
                    run.identity(profile)
                run.command(
                    "retention-" + action,
                    run.compose(profile)
                    + ["exec", "-T", "web", "python", "-m", "scripts.validation.retention", action],
                    profile["env"],
                )
        if args.suite in {"browser", "full"}:
            run.browser(profile)
        if args.suite in {"recovery", "full"}:
            run.recovery(profile)
        if args.suite in {"capacity", "full"}:
            run.capacity(profile)
        if (ROOT / "CANDIDATE_MANIFEST.json").exists():
            run.command(
                "candidate-after",
                [sys.executable, "scripts/release_candidates.py", "--verify", "."],
            )
    except Exception as exc:
        failure = {"error": type(exc).__name__, "message": str(exc)}
    finally:
        # A second interrupt must not skip the bounded owned-resource cleanup.
        for sig in previous_handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            cleanup_errors = run.cleanup()
        except Exception as exc:
            cleanup_errors = ["Cleanup aborted: " + type(exc).__name__]
            (output / "cleanup.json").write_text(
                json.dumps({"pass": False, "errors": cleanup_errors})
            )
        (output / "result.json").write_text(
            json.dumps(
                {
                    "suite": args.suite,
                    "failure": failure,
                    "cleanup_errors": cleanup_errors,
                    "pass": not failure and not cleanup_errors,
                    "remote_ci_executed": False,
                },
                indent=2,
            )
        )
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    if failure or cleanup_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
