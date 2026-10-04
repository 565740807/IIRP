"""A short DB lock wait cannot destroy an otherwise healthy long backup."""

from unittest.mock import patch

import pytest
from iirp.maintenance import maintenance_fence_with_grace
from sqlalchemy.exc import OperationalError


def busy():
    raise OperationalError("SELECT job", {}, Exception("controlled lock wait"))


def test_one_transient_lock_timeout_retains_producer_but_long_failure_stops():
    with patch("iirp.maintenance.time.monotonic", return_value=10):
        assert maintenance_fence_with_grace(busy, 5) == (True, 5)
    with patch("iirp.maintenance.time.monotonic", return_value=21):
        with pytest.raises(OperationalError):
            maintenance_fence_with_grace(busy, 5)


def test_control_rejection_remains_immediate():
    with patch("iirp.maintenance.time.monotonic", return_value=10):
        assert maintenance_fence_with_grace(lambda: False, 5) == (False, 10)
