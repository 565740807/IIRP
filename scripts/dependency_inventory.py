"""Inventory exact locked versions; unavailable license metadata stays pending."""

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def inventory():
    result = []
    review_path = ROOT / "docs/THIRD_PARTY_REVIEW.json"
    reviewed = (
        {
            (row["ecosystem"], row["name"], row["version"]): row
            for row in json.loads(review_path.read_text())["packages"]
        }
        if review_path.exists()
        else {}
    )
    for package in tomllib.loads((ROOT / "uv.lock").read_text())["package"]:
        if package["name"] == "iirp-v1":
            continue
        result.append(
            {
                "ecosystem": "python",
                "name": package["name"],
                "version": package["version"],
                "license": "Pending upstream distribution/license review",
                "lock": "uv.lock",
            }
        )
    for lock_path in ["frontend/package-lock.json", "browser/package-lock.json"]:
        lock = json.loads((ROOT / lock_path).read_text())
        for path, package in lock["packages"].items():
            if not path or "version" not in package:
                continue
            result.append(
                {
                    "ecosystem": "npm",
                    "name": path.split("node_modules/")[-1],
                    "version": package["version"],
                    "license": package.get("license", "Pending review"),
                    "lock": lock_path,
                }
            )
    for row in result:
        review = reviewed.get((row["ecosystem"], row["name"], row["version"]))
        if review:
            row["distribution_review"] = {
                key: review[key] for key in ["status", "url", "sha256", "integrity_verified"]
            }
            if review.get("declared_license"):
                row["license"] = review["declared_license"]
            elif review.get("license_classifiers"):
                row["license"] = "; ".join(review["license_classifiers"])
            row["notice_material"] = "docs/THIRD_PARTY_LICENSE_TEXTS.txt"
    return {
        "project_license": "Owner decision pending; no project license granted",
        "scope": "Locked Python and npm packages; verified artifact/notice material in "
        "docs/THIRD_PARTY_REVIEW.json is not publication or licensing approval",
        "packages": result,
    }


if __name__ == "__main__":
    print(json.dumps(inventory(), ensure_ascii=False, indent=2))
