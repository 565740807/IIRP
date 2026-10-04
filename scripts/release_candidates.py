"""Allowlisted working-tree export, including untracked inputs; no publication permission."""

import argparse
import fnmatch
import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
RULES = "config/public-candidate.json"
SKIP_DIRS = {"__pycache__", "node_modules", "dist", ".pytest_cache", ".ruff_cache"}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(root, name):
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or "\\" in name or not relative.parts:
        raise ValueError("Unsafe candidate path")
    path = root
    for part in relative.parts:
        path /= part
        if path.is_symlink():
            raise ValueError(f"Candidate symlink rejected: {name}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Candidate path escaped source root")
    return path


def selected(root):
    rules = json.loads(safe_path(root, RULES).read_text())
    names = set(rules["required"])
    for directory, suffixes in rules["trees"].items():
        base = safe_path(root, directory)
        if not base.is_dir():
            raise ValueError(f"Required candidate directory missing: {directory}")
        for current, directories, files in os.walk(base, followlinks=False):
            directories[:] = sorted(d for d in directories if d not in SKIP_DIRS)
            for item in directories + files:
                path = Path(current) / item
                if path.is_symlink():
                    raise ValueError(f"Candidate symlink rejected: {path.relative_to(root)}")
            for item in files:
                if item == ".DS_Store":
                    continue
                path = Path(current) / item
                name = path.relative_to(root).as_posix()
                if path.suffix in suffixes:
                    names.add(name)
                elif path.suffix not in {".pyc", ".tsbuildinfo"}:
                    raise ValueError(f"Unclassified candidate input (update rules): {name}")
    for pattern in rules["globs"]:
        for path in root.glob(pattern):
            name = path.relative_to(root).as_posix()
            safe_path(root, name)
            if path.is_file():
                names.add(name)
    names = {n for n in names if not any(fnmatch.fnmatchcase(n, p) for p in rules["exclude"])}
    for name in rules["required"]:
        if name not in names or not safe_path(root, name).is_file():
            raise ValueError(f"Required candidate input missing: {name}")
    return rules, sorted(names)


def public_bytes(name, data):
    """Deterministic docs-only redaction, visible in original/exported hashes.

    Never transforms runnable code or overwrites private historical documents.
    """
    if name.endswith(".md"):
        text = data.decode("utf-8")
        text = re.sub(
            r"/(?:home|Users)/[^/\s`]+/(?:IIRP2|IIRP-v2|IIRP)(?=[/\s`。，；]|$)", "<project>", text
        )
        text = re.sub(r"/(?:home|Users)/[^/\s`]+", "<user-home>", text)
        return text.encode("utf-8")
    return data


def scan(name, data):
    """Category/location only; bounded pattern scan complements human review."""
    patterns = {
        "private-key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        "credential-token": r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{32,})",
        "personal-absolute-path": r"/(?:home|Users)/[A-Za-z][^/\s\"\x27`]+/",
        "credential-url": r"(?:postgres(?:ql)?(?:\+psycopg)?|https?)://[^\s/:]+:[^\s/@]+@",
    }
    text = data.decode("utf-8", errors="replace")
    findings = []
    for category, pattern in patterns.items():
        for match in re.finditer(pattern, text):
            value = match.group()
            line = text.splitlines()[text.count("\n", 0, match.start())]
            # Existing rejection fixture, reserved example host and placeholder
            # credentials, reviewed without reporting its value. Exact digest
            # keeps this exception from exempting another URL or whole file.
            if (
                category == "credential-url"
                and name == "tests/test_event_contracts.py"
                and sha256(line.strip().rstrip(",").strip('"').encode())
                == "263fb9cca7f080dd65bbe4f33492592a4abf9091aff1c1db208aa2707ece1b5e"
            ):
                continue
            if category == "credential-url" and any(
                v in value
                for v in (
                    "${",
                    "{password}",
                    "test:test@",
                    "test:secret@",
                    "u:p@",
                )
            ):
                continue
            findings.append(
                {
                    "path": name,
                    "line": text.count("\n", 0, match.start()) + 1,
                    "category": category,
                    "value": "REDACTED",
                }
            )
    return findings


def candidates(root=ROOT):
    _rules, names = selected(root)
    return [{"path": name, "sha256": sha256(safe_path(root, name).read_bytes())} for name in names]


def export(root, destination):
    root = root.resolve()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Export destination must not exist")
    for parent in destination.absolute().parents:
        if parent.is_symlink():
            raise ValueError("Export destination has a symlink ancestor")
    rules, names = selected(root)
    prepared, findings = [], []
    for name in names:
        path = safe_path(root, name)
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError(f"Not a regular candidate file: {name}")
        original = path.read_bytes()
        data = public_bytes(name, original)
        findings.extend(scan(name, data))
        prepared.append(
            (
                name,
                data,
                {
                    "path": name,
                    "source_sha256": sha256(original),
                    "sha256": sha256(data),
                    "bytes": len(data),
                    "documentation_redacted": data != original,
                    "executable": bool(path.stat().st_mode & 0o111),
                },
            )
        )
    if findings:
        raise ValueError(json.dumps({"scan_findings": findings}, ensure_ascii=False))
    destination.mkdir(parents=True, exist_ok=False)
    for name, data, record in prepared:
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o755 if record["executable"] else 0o644)
    manifest = {
        "format": 1,
        "status": "local_candidate_not_publication_approval",
        "license": "owner_decision_pending",
        "rules": rules,
        "files": [entry[2] for entry in prepared],
        "scan_findings": findings,
    }
    (destination / "CANDIDATE_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    (destination / "SHA256SUMS").write_text(
        "".join(f"{record['sha256']}  {name}\n" for name, _data, record in prepared)
    )
    verify(destination)
    return manifest


def verify(root):
    manifest = json.loads(safe_path(root, "CANDIDATE_MANIFEST.json").read_text())
    expected = {entry["path"]: entry for entry in manifest["files"]}
    if len(expected) != len(manifest["files"]):
        raise ValueError("Duplicate manifest paths")
    for name, entry in expected.items():
        path = safe_path(root, name)
        if not path.is_file() or sha256(path.read_bytes()) != entry["sha256"]:
            raise ValueError(f"Candidate hash mismatch: {name}")
        if bool(path.stat().st_mode & 0o111) != entry["executable"]:
            raise ValueError(f"Candidate executable mode mismatch: {name}")
    if set(selected(root)[1]) != set(expected):
        raise ValueError("Candidate inventory differs from reconstruction rules")
    return len(expected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        print(json.dumps({"verified_files": verify(args.verify)}))
    elif args.output:
        result = export(ROOT, args.output)
        print(json.dumps({"exported_files": len(result["files"]), "scan_findings": []}))
    else:
        rules, _names = selected(ROOT)
        print(json.dumps({"rules": rules, "files": candidates()}, indent=2))


if __name__ == "__main__":
    main()
