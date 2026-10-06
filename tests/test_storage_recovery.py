"""Recovery keeps the new isolation boundary and refuses corrupt private copies."""

import pytest
from iirp.db import session
from iirp.models import SourceObject
from iirp.storage import save_object
from test_maintenance_lifecycle import isolated as isolated


def test_corrupt_copy_never_publishes_a_pool_object_or_backup(isolated, monkeypatch):
    backup = isolated.backup
    source = save_object(b"the original must survive a corrupt copy", "text/plain")
    with session() as s, s.begin():
        s.add(SourceObject(**source))
    copy = backup.shutil.copy2

    def corrupt_copy(src, dest):
        copy(src, dest)
        dest.write_bytes(b"corrupt private copy")

    monkeypatch.setattr(backup.shutil, "copy2", corrupt_copy)
    with pytest.raises(ValueError, match="复制校验失败"):
        backup.backup()
    assert (isolated.runtime / source["relative_path"]).read_bytes() == b"the original must survive a corrupt copy"
    assert not (isolated.runtime / "backups" / "object-pool" / source["relative_path"]).exists()
    assert backup.completed_backups() == []


def test_verified_restore_uses_only_test_namespace(isolated):
    source = save_object(b"verified namespace evidence", "text/plain")
    with session() as s, s.begin():
        s.add(SourceObject(**source))
    directory = isolated.backup.backup()
    report = isolated.backup.restore_verify(directory, discard=True)
    assert report["database"].startswith("iirp_v1_test_restore_")
    assert report["source_objects_verified"] == 1 and report["fact_hashes_verified"] > 20
    assert report["verification_copy_discarded"] is True


def test_each_backup_keeps_only_the_two_newest(isolated):
    backup = isolated.backup
    first = backup.backup()
    second = backup.backup()
    third = backup.backup()
    assert not first.exists()
    assert second.is_dir() and third.is_dir()
    assert [entry[1] for entry in backup.completed_backups()] == [third, second]
