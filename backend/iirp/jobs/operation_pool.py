"""Bounded warm child per coordinator thread, with the same lease/deadline fence."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from iirp.config import ROOT


class OperationInterrupted(Exception):
    pass


class OperationChild:
    def __init__(self, *, max_jobs=32):
        self.max_jobs = max_jobs
        self.proc = None
        self.directory = None
        self.errors = None
        self.completed = 0

    def close(self):
        if self.proc:
            if self.proc.poll() is None:
                os.killpg(self.proc.pid, signal.SIGTERM)
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                    self.proc.wait(timeout=2)
            self.proc.stdin.close()
            self.proc = None
        if self.errors:
            self.errors.close()
            self.errors = None
        if self.directory:
            self.directory.cleanup()
            self.directory = None

    def _start(self):
        self.close()
        self.directory = tempfile.TemporaryDirectory(prefix="iirp-operation-")
        self.errors = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "iirp.jobs.operations", "--serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=self.errors,
            start_new_session=True,
            env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
        )
        self.started = time.monotonic()
        self.completed = 0

    def run(self, kind, target, checkpoint=lambda: True, *, deadline=110):
        if (
            not self.proc
            or self.proc.poll() is not None
            or self.completed >= self.max_jobs
            or time.monotonic() - self.started >= 600
        ):
            self._start()
        directory = Path(self.directory.name)
        source, result = directory / "request.json", directory / "response.json"
        result.unlink(missing_ok=True)
        payload = json.dumps(
            {"kind": kind, "target": target}, ensure_ascii=False, default=str
        ).encode()
        if len(payload) > 16 * 1024**2:
            raise ValueError("操作输入超过16MiB预算")
        source.write_bytes(payload)
        self.proc.stdin.write(
            (json.dumps({"input": str(source), "output": str(result)}) + "\n").encode()
        )
        self.proc.stdin.flush()
        started = time.monotonic()
        try:
            while not result.exists():
                if not checkpoint():
                    raise OperationInterrupted
                if self.proc.poll() is not None:
                    raise RuntimeError(f"操作子进程退出 {self.proc.returncode}")
                if time.monotonic() - started > deadline:
                    raise TimeoutError("操作单元超过期限")
                time.sleep(0.1)
            if result.stat().st_size > 80 * 1024**2:
                raise ValueError("操作输出超过80MiB预算")
            response = json.loads(result.read_bytes())
            self.completed += 1
            return response
        except BaseException:
            # No failed/paused/cancelled operation can remain alive in the pool.
            self.close()
            raise


class OperationPool:
    """At most two warm children: one Yahoo lane and one computation lane.

    SEC keeps its disposable isolation, so idle processes cannot multiply the
    worker's steady resident memory by the executor's thread count.
    """
    def __init__(self):
        self.children = {}
        self.lock = threading.Lock()

    def run(self, kind, *args, **kwargs):
        lane = "compute" if kind == "research_compute" else "market"
        with self.lock:
            child = self.children.setdefault(lane, OperationChild())
        return child.run(kind, *args, **kwargs)

    def close(self):
        for child in self.children.values():
            child.close()
