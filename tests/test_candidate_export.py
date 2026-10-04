"""Export actual files safely, preserving untracked migrations and executable modes."""

import json

import pytest

from scripts import release_candidates as candidate


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    (root / "config").mkdir(parents=True)
    (root / "migrations").mkdir()
    (root / "migrations/untracked.py").write_text('revision = "synthetic"\n')
    (root / "iirp").write_text("#!/bin/sh\nexit 0\n")
    (root / "iirp").chmod(0o755)
    (root / "README.md").write_text("Personal directory: /" + "home/example/IIRP2\n")
    (root / candidate.RULES).write_text(
        json.dumps(
            {
                "required": ["iirp", "README.md", candidate.RULES],
                "trees": {"migrations": [".py"]},
                "globs": [],
                "exclude": [],
            }
        )
    )
    return root


def test_untracked_rebuild_inputs_export_and_verify(source, tmp_path):
    # Private file is deliberately unreadable and outside all traversed surfaces.
    (source / ".env").write_text("not-a-real-secret")
    (source / ".env").chmod(0)
    out = tmp_path / "candidate"
    before = {p: p.read_bytes() for p in [source / "README.md", source / "migrations/untracked.py"]}
    manifest = candidate.export(source, out)
    assert candidate.verify(out) == 4
    assert (out / "migrations/untracked.py").is_file()
    assert (out / "iirp").stat().st_mode & 0o111
    assert "<project>" in (out / "README.md").read_text()
    assert not (out / ".env").exists()
    assert any(f["documentation_redacted"] for f in manifest["files"])
    assert all(p.read_bytes() == data for p, data in before.items())
    with pytest.raises(ValueError, match="must not exist"):
        candidate.export(source, out)
    (out / "migrations/untracked.py").write_text("tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        candidate.verify(out)


@pytest.mark.parametrize("kind", ["file-link", "directory-link", "traversal", "absolute"])
def test_paths_cannot_read_outside_root(source, tmp_path, kind):
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "secret.py").write_text("private source")
    rules = json.loads((source / candidate.RULES).read_text())
    if kind == "file-link":
        (source / "migrations/link.py").symlink_to(outside / "secret.py")
    elif kind == "directory-link":
        (source / "migrations/nested").symlink_to(outside, target_is_directory=True)
    else:
        rules["required"].append(
            "../external/secret.py" if kind == "traversal" else str(outside / "secret.py")
        )
        (source / candidate.RULES).write_text(json.dumps(rules))
    out = tmp_path / "out"
    with pytest.raises(ValueError):
        candidate.export(source, out)
    assert not out.exists()


def test_missing_required_and_unclassified_inputs_stop_export(source, tmp_path):
    (source / "migrations/missing-format.dat").write_text("requires classification")
    with pytest.raises(ValueError, match="Unclassified"):
        candidate.export(source, tmp_path / "out")
    (source / "migrations/missing-format.dat").unlink()
    (source / "iirp").unlink()
    with pytest.raises(ValueError, match="missing"):
        candidate.export(source, tmp_path / "out")


def test_secret_detection_never_reports_matched_value(source, tmp_path):
    secret = "gh" + "p_" + "x" * 36  # Synthetic pattern only.
    (source / "migrations/untracked.py").write_text("token = " + repr(secret))
    with pytest.raises(ValueError) as error:
        candidate.export(source, tmp_path / "out")
    assert secret not in str(error.value)
    assert "credential-token" in str(error.value)
    assert "REDACTED" in str(error.value)
    assert not (tmp_path / "out").exists()
