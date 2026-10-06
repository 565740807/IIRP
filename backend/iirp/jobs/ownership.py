"""Fail-closed, continuously verified ownership of the coordinator session."""
import logging
import threading
import time

from sqlalchemy import exists, select, text

from iirp.db import engine, session
from iirp.models import Job, now

LOCK_KEY = 482918001


class OwnershipLost(RuntimeError):
    pass


class WorkerOwnership:
    def __init__(self, *, interval=0.25, max_silence=2.0):
        self.interval = interval
        self.max_silence = max_silence
        self.lost = threading.Event()
        self.closed = threading.Event()
        self.verified_at = 0.0

    def __enter__(self):
        self.connection = engine().connect()
        try:
            if not self.connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}):
                raise RuntimeError("This database already has an IIRP worker.")
            self.pid = self.connection.scalar(text("SELECT pg_backend_pid()"))
            self.connection.commit()
            self.verified_at = time.monotonic()
            self.thread = threading.Thread(target=self._monitor, name="worker-ownership", daemon=True)
            self.thread.start()
            return self
        except BaseException:
            self.connection.close()
            raise

    def _monitor(self):
        try:
            while not self.closed.wait(self.interval):
                # Never reacquire: a replacement connection is not our session.
                if self.connection.invalidated or not self.connection.scalar(text("""
                    SELECT pg_backend_pid() = :pid AND EXISTS (
                        SELECT 1 FROM pg_locks WHERE locktype = 'advisory'
                        AND pid = pg_backend_pid() AND classid = 0
                        AND objid = :key AND objsubid = 1 AND granted)
                """), {"pid": self.pid, "key": LOCK_KEY}):
                    raise OwnershipLost("Worker singleton session no longer owns its lock")
                self.connection.commit()
                self.verified_at = time.monotonic()
        except Exception:
            self.lost.set()
            logging.exception("worker ownership lost; stopping claims and supervised operations")

    def stopping(self):
        if time.monotonic() - self.verified_at > self.max_silence:
            self.lost.set()
        return self.lost.is_set()

    def require(self):
        if self.stopping():
            raise OwnershipLost("Worker singleton ownership lost or verification expired")

    def __exit__(self, *_):
        self.closed.set()
        self.thread.join(timeout=6)
        # The monitor uses a bounded database statement timeout. Do not return a
        # still-owned lock to the pool: pooled session locks survive close().
        if self.thread.is_alive():
            self.connection.invalidate()
        else:
            try:
                if not self.connection.invalidated:
                    self.connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
                    self.connection.commit()
            except Exception:
                self.connection.invalidate()
        self.connection.close()


def previous_leases_drained():
    """A successor must not add provider capacity during predecessor shutdown.

    Its old children observe loss within the monitor deadline, while their
    durable job leases outlive that interval. Ordinary queue recovery expires
    those leases before this gate permits new source work.
    """
    with session() as s:
        return not s.scalar(select(exists().where(Job.lease_token.is_not(None), Job.lease_until > now())))
