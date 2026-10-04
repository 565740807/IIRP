import json
from pathlib import Path

from iirp.api import app

root = Path(__file__).resolve().parents[1]
(root / "docs/openapi.json").write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2))
print("docs/openapi.json exported")
