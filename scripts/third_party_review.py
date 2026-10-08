"""Refresh docs/THIRD_PARTY_REVIEW.json and docs/THIRD_PARTY_LICENSE_TEXTS.txt from the locks.

Run after a dependency change (``python3 scripts/third_party_review.py``, standard
library only, needs network access). Entries for packages that are still locked at
the same version are kept as they are, including earlier manual upstream checks;
entries for packages no longer locked are dropped. Each newly locked package is
downloaded from its locked URL, checked against the locked hash, and its LICENSE /
NOTICE / COPYING files are copied out. For Python one distribution per version is
inspected (pure wheel, else Linux x86_64 wheel, else sdist); for npm every locked
tarball, including optional platform packages. Then regenerate the inventory with
``python3 scripts/dependency_inventory.py > docs/THIRD_PARTY_INVENTORY.json``.
"""

import base64
import email.parser
import hashlib
import io
import json
import re
import tarfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "docs/THIRD_PARTY_REVIEW.json"
TEXTS = ROOT / "docs/THIRD_PARTY_LICENSE_TEXTS.txt"
CACHE = ROOT / ".tools/third-party-cache"
NOTICE = re.compile(r"^(licen[cs]e|notice|copying|copyright)([._-].*)?$", re.I)
RULE = "=" * 72
TEXTS_HEADER = ("Upstream notices retained from verified locked public distributions.\n"
                "IIRP itself is released under the MIT License (see LICENSE).\n")


def python_packages():
    for package in tomllib.loads((ROOT / "uv.lock").read_text())["package"]:
        if package["name"] == "iirp-v1":
            continue
        wheels = package.get("wheels", [])
        choice = (
            next((w for w in wheels if w["url"].endswith("-none-any.whl")), None)
            or next((w for w in wheels if "manylinux" in w["url"] and "x86_64" in w["url"]), None)
            or package.get("sdist")
        )
        yield {"ecosystem": "python", "name": package["name"], "version": package["version"],
               "url": choice["url"], "integrity": choice["hash"]}


def npm_packages():
    lock = json.loads((ROOT / "frontend/package-lock.json").read_text())
    for path, package in lock["packages"].items():
        if path and "resolved" in package:
            yield {"ecosystem": "npm", "name": path.split("node_modules/")[-1],
                   "version": package["version"], "url": package["resolved"],
                   "integrity": package["integrity"]}


def key(row):
    return row["ecosystem"], row["name"], row["version"]


def download(row):
    algorithm, expected = row["integrity"].replace(":", "-", 1).split("-", 1)
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / hashlib.sha256(row["url"].encode()).hexdigest()
    if cached.exists():
        data = cached.read_bytes()
    else:
        request = urllib.request.Request(row["url"], headers={"User-Agent": "iirp-third-party-review"})
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read()
    digest = hashlib.new(algorithm, data)
    actual = digest.hexdigest() if algorithm == "sha256" else base64.b64encode(digest.digest()).decode()
    if actual != expected:
        raise SystemExit(f"{row['name']} {row['version']}: {algorithm} mismatch for {row['url']}")
    cached.write_bytes(data)
    return data


def members(data, url):
    """(archive path, bytes) of every regular file in a wheel, sdist or npm tarball."""
    if url.endswith(".whl") or url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    yield info.filename, lambda info=info, archive=archive: archive.read(info)
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            for info in archive.getmembers():
                if info.isfile():
                    yield info.name, lambda info=info, archive=archive: archive.extractfile(info).read()


def inspect(row):
    data = download(row)
    notices, texts, license_name, classifiers = [], [], None, []
    for path, read in members(data, row["url"]):
        parts = path.split("/")
        name = parts[-1]
        if row["ecosystem"] == "npm" and path.count("node_modules/"):
            continue
        if NOTICE.match(name) and len(parts) <= 4:
            body = read()
            notices.append({"archive_path": path, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)})
            texts.append((path, body))
        elif row["ecosystem"] == "npm" and path.count("/") == 1 and name == "package.json":
            declared = json.loads(read()).get("license")
            license_name = declared if isinstance(declared, str) else (declared or {}).get("type")
        elif row["ecosystem"] == "python" and re.fullmatch(r"[^/]+\.dist-info/METADATA|[^/]+/PKG-INFO", path):
            metadata = email.parser.Parser().parsestr(read().decode("utf-8", "replace"))
            license_name = metadata.get("License-Expression") or (metadata.get("License") or "").split("\n")[0] or None
            classifiers = [c for c in metadata.get_all("Classifier") or [] if c.startswith("License ::")]
    entry = {
        **row,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "integrity_verified": True,
        "status": "integrity_and_notice_material_checked" if notices else "integrity_checked_notice_missing",
        "declared_license": license_name,
        "license_classifiers": classifiers,
        "notices": notices,
    }
    return entry, texts


def text_blocks():
    """Existing notice blocks, grouped by (ecosystem, name, version)."""
    blocks = {}
    if not TEXTS.exists():
        return blocks
    text = TEXTS.read_text()
    heads = list(re.finditer(rf"^{RULE}\n(python|npm) (\S+) (\S+)\nSource: .*\n(?:.*\n){{0,3}}?{RULE}\n", text, re.M))
    for head, following in zip(heads, [*heads[1:], None]):
        end = following.start() if following else len(text)
        blocks.setdefault(head.groups(), []).append(text[head.start():end])
    return blocks


def block(entry, path, body):
    return (f"{RULE}\n{entry['ecosystem']} {entry['name']} {entry['version']}\nSource: {entry['url']}\n"
            f"Archive path: {path}\nSHA256: {hashlib.sha256(body).hexdigest()}\n{RULE}\n"
            f"{body.decode('utf-8', 'replace').rstrip()}\n\n")


def main():
    locked = [*python_packages(), *npm_packages()]
    previous = json.loads(REVIEW.read_text()) if REVIEW.exists() else {"packages": [], "remaining": []}
    reviewed = {key(entry): entry for entry in previous["packages"]}
    blocks = text_blocks()
    packages, out, added = [], [TEXTS_HEADER + "\n"], []
    for row in locked:
        if key(row) in reviewed:
            packages.append(reviewed[key(row)])
            out.extend(blocks.get(key(row), []))
            continue
        entry, texts = inspect(row)
        packages.append(entry)
        out.extend(block(entry, path, body) for path, body in texts)
        added.append(key(row))
    missing = sorted(f"{e['name']} {e['version']}" for e in packages
                     if e["status"] == "integrity_checked_notice_missing")
    REVIEW.write_text(json.dumps({
        "scope": "One locked Python distribution per version (prefer pure/Linux x86_64); all npm locked "
                 "distributions including optional platforms. Artifact and included notice inspection is "
                 "not complete binary redistribution clearance.",
        "packages": packages,
        "remaining": [
            "Locked distributions without a notice file (declared license recorded, not a substitute): "
            + ", ".join(missing) + ".",
            "Platform-specific esbuild/rollup packages omit notice bodies; matching parent-package notices retained.",
            "Only the selected Python distribution per locked version is inspected; bundled native components "
            "and container image redistribution remain a separate review.",
        ],
    }, ensure_ascii=False, indent=2) + "\n")
    TEXTS.write_text("".join(out))
    dropped = sorted(set(reviewed) - {key(row) for row in locked})
    print(f"{len(packages)} locked distributions; {len(added)} newly reviewed; {len(dropped)} no longer locked")


if __name__ == "__main__":
    main()
