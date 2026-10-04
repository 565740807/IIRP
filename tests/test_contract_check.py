"""The contract gate detects real drift and leaves the evidence untouched."""

import json
import subprocess
import sys

from iirp.api import app
from iirp.config import ROOT


def test_openapi_gate_rejects_drift_without_repairing(tmp_path):
    schema = tmp_path / "openapi.json"
    schema.write_text(json.dumps(app.openapi()))
    command = [sys.executable, str(ROOT / "scripts/check_openapi.py"), "--schema", str(schema)]
    assert subprocess.run(command, capture_output=True).returncode == 0
    changed = json.loads(schema.read_text())
    changed["info"]["title"] = "deliberately stale contract"
    schema.write_text(json.dumps(changed))
    before = schema.read_bytes()
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert "OpenAPI drift" in result.stderr
    assert schema.read_bytes() == before
