"""Host-safe regression tests: no Docker daemon, database or third-party imports."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import validate


class ValidationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root_patch = patch.object(validate, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.run = validate.Run(self.root / "output")

    def test_timeout_stops_wrapper_descendant(self):
        marker = self.root / "survivor"
        child = f"import pathlib,time; time.sleep(0.8); pathlib.Path({str(marker)!r}).touch()"
        parent = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
        )
        with self.assertRaises(RuntimeError):
            self.run.command("timeout", [sys.executable, "-c", parent], timeout=0.3)
        time.sleep(0.9)
        self.assertFalse(marker.exists(), "a descendant survived the command timeout")
        self.assertEqual(
            json.loads((self.run.output / "steps.json").read_text())[0]["exit_code"], 124
        )

    def test_profile_does_not_inherit_credentials_or_provider_contact(self):
        with patch.dict(
            os.environ,
            {
                "IIRP_DB_PASSWORD": "synthetic-not-a-secret",
                "IIRP_SEC_USER_AGENT": "synthetic-contact",
                "IIRP_DATABASE_URL": "postgresql://synthetic/original",
                "COMPOSE_PROFILES": "unexpected",
            },
        ):
            profile = self.run.profile()
        for key in [
            "IIRP_DB_PASSWORD",
            "IIRP_SEC_USER_AGENT",
            "IIRP_DATABASE_URL",
            "COMPOSE_PROFILES",
        ]:
            self.assertNotIn(key, profile["env"])
        self.assertEqual(self.run.cleanup(), [])

    def test_missing_private_config_does_not_abort_cleanup(self):
        profile = self.run.profile()
        profile["config"].unlink()
        self.assertEqual(self.run.cleanup(), [])
        self.assertTrue(json.loads((self.run.output / "cleanup.json").read_text())["pass"])

    def test_replaced_private_config_is_preserved(self):
        profile = self.run.profile()
        saved = self.root / "saved-original"
        profile["config"].rename(saved)
        profile["config"].write_text("replacement belongs to another operation")
        self.assertTrue(self.run.cleanup())
        self.assertEqual(profile["config"].read_text(), "replacement belongs to another operation")

    def test_unlabelled_named_volume_blocks_start_without_ownership(self):
        profile = self.run.profile()

        def inventory(command, **kwargs):
            if command[:3] == ["docker", "volume", "ls"] and "--format" in command:
                return SimpleNamespace(stdout=profile["project"] + "_postgres-data\n")
            return SimpleNamespace(stdout=b"" if not kwargs.get("text") else "")

        with patch.object(validate, "run_process", side_effect=inventory):
            with self.assertRaisesRegex(RuntimeError, "refusing reuse"):
                self.run.start(profile, "collision")
        self.assertFalse(profile.get("owned_images"))
        self.assertFalse(profile["started"])
        self.assertEqual(self.run.cleanup(), [])

    def check_container_cleanup(self, auto_remove):
        profile = self.run.profile()
        profile["owned_images"] = True
        calls = []
        removed = False

        def inventory(command, **kwargs):
            calls.append(command)
            if command[:2] == ["docker", "ps"]:
                self.assertIn(
                    "label=iirp.validation=" + profile["env"]["IIRP_VALIDATION_ID"], command
                )
                self.assertIn("label=iirp.validation.project=" + profile["project"], command)
                if removed:
                    return SimpleNamespace(stdout="")
                row = "owned-id|web|True\n" if "--format" in command else "owned-id\n"
                return SimpleNamespace(stdout=row)
            return SimpleNamespace(stdout="")

        def mutate(_label, command, **kwargs):
            nonlocal removed
            if command[:2] == ["docker", "rm"] or (
                auto_remove and command[:2] == ["docker", "stop"]
            ):
                removed = True

        with (
            patch.object(validate, "run_process", side_effect=inventory),
            patch.object(self.run, "command", side_effect=mutate) as command,
        ):
            self.assertEqual(self.run.cleanup(), [])
        self.assertFalse(any("down" in command for command in calls))
        return [call.args[1] for call in command.call_args_list]

    def test_cleanup_selects_precisely_owned_containers(self):
        commands = self.check_container_cleanup(auto_remove=False)
        self.assertIn(["docker", "rm", "owned-id"], commands)

    def test_auto_removed_container_does_not_make_cleanup_fail(self):
        commands = self.check_container_cleanup(auto_remove=True)
        self.assertTrue(any(command[:2] == ["docker", "stop"] for command in commands))
        self.assertFalse(any(command[:2] == ["docker", "rm"] for command in commands))

    def test_cleanup_stops_clients_then_worker_web_and_postgres(self):
        profile = self.run.profile()
        rows = "db|postgres|False\nweb|web|False\nworker|worker|False\nclient|web|True\nbrowser||\n"
        with patch.object(validate, "run_process", return_value=SimpleNamespace(stdout=rows)):
            groups = self.run.container_stop_groups(
                profile, ["db", "web", "worker", "client", "browser"]
            )
        self.assertEqual(groups, [["client", "browser"], ["worker"], ["web"], ["db"]])
        self.assertEqual(self.run.cleanup(), [])

    def test_cleanup_error_still_removes_owned_config_and_records_failure(self):
        profile = self.run.profile()
        profile["owned_images"] = True
        with patch.object(validate, "run_process", side_effect=OSError("synthetic Docker failure")):
            self.assertTrue(self.run.cleanup())
        self.assertFalse(profile["config"].exists())
        self.assertFalse(json.loads((self.run.output / "cleanup.json").read_text())["pass"])

    def test_replaced_image_is_not_removed(self):
        profile = self.run.profile()
        profile["owned_images"] = True

        def inventory(command, **kwargs):
            if command[:3] == ["docker", "image", "ls"]:
                return SimpleNamespace(stdout="image-id\n")
            if command[:3] == ["docker", "image", "inspect"]:
                return SimpleNamespace(stdout=json.dumps({"iirp.validation": "different-owner"}))
            return SimpleNamespace(stdout="")

        with (
            patch.object(validate, "run_process", side_effect=inventory),
            patch.object(self.run, "command") as command,
        ):
            self.assertTrue(self.run.cleanup())
        command.assert_not_called()

    def test_output_parent_escape_is_rejected_before_execution(self):
        with patch.object(sys, "argv", ["validate", "--output", str(self.root / "../escaped")]):
            with self.assertRaises(SystemExit):
                validate.main()
        self.assertFalse((self.root.parent / "escaped").exists())

    def test_sigterm_records_failure_and_cleanup(self):
        # Exercise actual signal handling in a separate process, without Docker.
        repo = Path(validate.__file__).resolve().parents[1]
        out = self.root / "signal-output"
        harness = (
            "import pathlib,signal,sys,time; "
            f"sys.path.insert(0, {str(repo)!r}); "
            "from scripts import validate; "
            f"validate.ROOT=pathlib.Path({str(self.root)!r}); "
            "validate.Run.command=lambda *args, **kwargs: time.sleep(30); "
            f"sys.argv=['validate','--output',{str(out)!r}]; "
            "validate.main()"
        )
        process = subprocess.Popen([sys.executable, "-c", harness])
        try:
            deadline = time.monotonic() + 5
            while not out.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(out.exists())
            time.sleep(0.1)
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=5), 1)
            result = json.loads((out / "result.json").read_text())
            self.assertFalse(result["pass"])
            self.assertEqual(result["failure"]["error"], "ValidationInterrupted")
            self.assertTrue(json.loads((out / "cleanup.json").read_text())["pass"])
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


if __name__ == "__main__":
    unittest.main()
