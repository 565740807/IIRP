"""Compare the checked-in contract to the application, without rewriting either."""

import argparse
import json
from pathlib import Path

from iirp.api import app

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", type=Path, default=ROOT / "docs/openapi.json")
    args = parser.parse_args()
    if json.loads(args.schema.read_text()) != app.openapi():
        raise SystemExit(
            "OpenAPI drift: inspect API changes, then explicitly run "
            "./iirp python scripts/export_openapi.py and ./iirp frontend npm run generate:api"
        )
    print("OpenAPI matches application (read-only comparison)")


if __name__ == "__main__":
    main()
