"""Exercise fixture readiness and launch ordering without a server or database."""

import asyncio
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scripts.validation import network_server


class TestNetworkFixtureSetup(unittest.TestCase):
    def run_fixture(self, *, identifiers=None, published=(), queue_error=None, probe=None):
        if identifiers is None:
            identifiers = [f"analysis-{i}" for i in range(10)]
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            out = Path(directory)
            routes = {}
            session = MagicMock()
            session.__enter__.return_value = session
            session.scalars.return_value = published
            session.get.return_value = SimpleNamespace(data={"synthetic": True})
            server = SimpleNamespace(started=False, should_exit=False, run=lambda: None)
            thread = MagicMock()
            thread.is_alive.return_value = False
            worker = MagicMock(pid=424242)
            worker.poll.return_value = 0
            popen = stack.enter_context(
                patch.object(network_server.subprocess, "Popen", return_value=worker)
            )

            def prepare(count):
                self.assertEqual(count, 10)
                self.assertFalse((out / "network-fixture.json").exists())
                popen.assert_not_called()
                thread.start.assert_not_called()
                if queue_error:
                    raise queue_error
                return identifiers

            queue = MagicMock(side_effect=prepare)

            def start():
                queue.assert_called_once_with(10)
                self.assertFalse((out / "network-fixture.json").exists())
                popen.assert_not_called()
                server.started = True

            thread.start.side_effect = start

            def client_ready(_timeout):
                fixture = json.loads((out / "network-fixture.json").read_text())
                self.assertEqual(fixture["background_prepared"]["queued"], identifiers)
                self.assertEqual(fixture["background_prepared"]["published"], [])
                self.assertFalse(fixture["background_prepared"]["worker_started"])
                popen.assert_not_called()
                if probe:
                    probe(routes, popen, queue, out)
                return True

            finished = MagicMock()
            finished.wait.side_effect = client_ready
            stack.enter_context(
                patch.object(
                    network_server, "verified_identity", return_value={"validation_id": "test-id"}
                )
            )
            stack.enter_context(patch.object(network_server, "disable_sources"))
            stack.enter_context(patch.object(network_server, "session", return_value=session))
            stack.enter_context(
                patch.object(
                    network_server, "settings", return_value=SimpleNamespace(runtime_dir=out)
                )
            )
            stack.enter_context(
                patch.object(
                    network_server,
                    "add_test_route",
                    side_effect=lambda app, path, fn, **kw: routes.update({path: fn}),
                )
            )
            stack.enter_context(patch.object(network_server.uvicorn, "Config"))
            stack.enter_context(patch.object(network_server.uvicorn, "Server", return_value=server))
            stack.enter_context(
                patch.object(network_server.threading, "Thread", return_value=thread)
            )
            stack.enter_context(
                patch.object(network_server.threading, "Event", return_value=finished)
            )
            stack.enter_context(patch.object(network_server.gc, "callbacks", []))
            read_text = Path.read_text

            def read_system(path, *args, **kwargs):
                return {
                    "/proc/self/cgroup": "0::/synthetic-test",
                    "/sys/fs/cgroup/memory.max": "2147483648",
                }.get(str(path), "") or read_text(path, *args, **kwargs)

            stack.enter_context(patch.object(Path, "read_text", read_system))
            try:
                network_server.serve_fixed(
                    object(), out, 19001, ("fixed-analysis", "fixed-result"), queue, {}
                )
            except Exception:
                self.assertFalse((out / "network-fixture.json").exists())
                setup = json.loads((out / "network-workload-setup.json").read_text())
                self.assertFalse(setup["complete"])
                self.assertEqual(
                    setup["error"], type(queue_error).__name__ if queue_error else "RuntimeError"
                )
                popen.assert_not_called()
                thread.start.assert_not_called()
                raise
            return {
                "setup": json.loads((out / "network-workload-setup.json").read_text()),
                "publication": json.loads((out / "network-publication.json").read_text()),
                "records": [
                    json.loads(line) for line in (out / "network-server.jsonl").read_text().splitlines()
                ],
            }

    def test_prepares_once_before_readiness_without_starting_worker(self):
        result = self.run_fixture()
        self.assertTrue(result["setup"]["complete"])
        self.assertEqual(result["setup"]["published"], [])
        self.assertFalse(result["publication"]["worker_started"])
        self.assertFalse(
            any(row["stage"] == "network_workload_started" for row in result["records"])
        )

    def test_post_only_starts_prepared_worker_once_and_checks_identity(self):
        def launch(routes, popen, queue, out):
            route = routes["/__iirp_test_workload__"]
            with self.assertRaises(Exception) as forbidden:
                asyncio.run(route(SimpleNamespace(headers={})))
            self.assertEqual(forbidden.exception.status_code, 403)
            popen.assert_not_called()
            response = asyncio.run(
                route(SimpleNamespace(headers={"X-IIRP-Test-Identity": "test-id"}))
            )
            self.assertEqual(response["queued_analyses"], [f"analysis-{i}" for i in range(10)])
            self.assertEqual(response["worker_pid"], 424242)
            queue.assert_called_once_with(10)
            popen.assert_called_once()
            with self.assertRaisesRegex(RuntimeError, "once only"):
                asyncio.run(route(SimpleNamespace(headers={"X-IIRP-Test-Identity": "test-id"})))
            popen.assert_called_once()

        result = self.run_fixture(probe=launch)
        self.assertTrue(result["publication"]["worker_started"])
        self.assertEqual(
            [row["stage"] for row in result["records"]],
            ["network_workload_setup", "network_workload_started"],
        )

    def test_preparation_exception_is_recorded_and_prevents_readiness(self):
        with self.assertRaisesRegex(ValueError, "setup failure"):
            self.run_fixture(queue_error=ValueError("setup failure"))

    def test_incomplete_or_duplicate_queue_prevents_readiness(self):
        for ids in ([f"analysis-{i}" for i in range(9)], ["same-analysis"] * 10):
            with (
                self.subTest(identifiers=ids),
                self.assertRaisesRegex(RuntimeError, "ten distinct"),
            ):
                self.run_fixture(identifiers=ids)

    def test_already_published_work_prevents_readiness(self):
        with self.assertRaisesRegex(RuntimeError, "published before"):
            self.run_fixture(published=["analysis-0"])
