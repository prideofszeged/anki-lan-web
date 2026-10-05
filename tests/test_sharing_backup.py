from __future__ import annotations

import io
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from anki.collection import Collection

from ankiweb.adapters.anki.sharing import ReleasePublisher, WorkspaceProvisioner
from ankiweb.identity import IdentityDatabase, IdentityRepository
from ankiweb.identity.repository import AuthorizationError
from ankiweb.sharing import ShareRole, SharingRepository, SharingService
from ankiweb.sharing.backup import BackupIntegrityError, ShareBackupManager
from ankiweb.tenancy import StorageLayout


NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def _stack(tmp_path: Path):
    storage = StorageLayout(tmp_path / "data")
    identities = IdentityRepository(IdentityDatabase(storage.app_db))
    identities.initialize()
    owner = identities.create_user(username="owner", now=NOW)
    member = identities.create_user(username="member", now=NOW)
    stranger = identities.create_user(username="stranger", now=NOW)
    owner_paths = storage.prepare_user(owner.id)
    col = Collection(str(owner_paths.collection), server=False)
    try:
        shared = int(col.decks.id("Shared"))
        note = col.new_note(col.models.by_name("Basic"))
        note["Front"], note["Back"] = "shared-front", "shared-back"
        col.add_note(note, shared)
        private = int(col.decks.id("Private"))
        note = col.new_note(col.models.by_name("Basic"))
        note["Front"], note["Back"] = "PRIVATE-SENTINEL", "never export"
        col.add_note(note, private)
    finally:
        col.close()

    repository = SharingRepository(identities.database)
    service = SharingService(repository, clock=lambda: NOW)
    share = service.create_share(actor_user_id=owner.id, name="Greek")
    invite = service.create_invite(
        actor_user_id=owner.id, share_id=share.id, role=ShareRole.VIEWER,
        intended_user_id=member.id,
    )
    service.accept_invite(actor_user_id=member.id, token=invite.token)
    WorkspaceProvisioner(storage).create_from_owner_deck(
        actor_user_id=owner.id, owner_collection=owner_paths.collection,
        share_id=share.id, deck_id=shared, authorize=repository.require_owner,
    )
    release = ReleasePublisher(storage, repository, clock=lambda: NOW).publish(
        actor_user_id=owner.id, share_id=share.id,
    )
    comment = repository.add_workspace_comment(
        actor_user_id=owner.id, share_id=share.id, entity_type="deck",
        entity_id=str(shared), body="Restore this comment", now=NOW,
    )
    manager = ShareBackupManager(storage, repository, clock=lambda: NOW)
    return storage, repository, owner, member, stranger, share, release, comment, manager


def test_sh11_backup_restores_workspace_release_and_comments(tmp_path: Path):
    storage, repository, owner, _, _, share, release, comment, manager = _stack(tmp_path)
    backup = manager.create(actor_user_id=owner.id, share_id=share.id)
    assert backup.archive.stat().st_mode & 0o777 == 0o600
    assert backup.checksum.stat().st_mode & 0o777 == 0o600
    assert manager.verify(backup.archive).share_id == share.id

    paths = storage.share_paths(share.id)
    col = Collection(str(paths.collection), server=False)
    try:
        note = col.get_note(col.find_notes("")[0])
        note["Front"] = "mutated"
        col.update_note(note)
    finally:
        col.close()
    release.manifest_path.unlink()
    with repository.database.transaction() as conn:
        conn.execute("DELETE FROM workspace_comments WHERE share_id=?", (share.id,))

    manager.restore(actor_user_id=owner.id, share_id=share.id, archive=backup.archive)

    col = Collection(str(paths.collection), server=False)
    try:
        assert {col.get_note(n)["Front"] for n in col.find_notes("")} == {"shared-front"}
    finally:
        col.close()
    assert release.manifest_path.is_file()
    restored = repository.list_workspace_comments(
        actor_user_id=owner.id, share_id=share.id,
        entity_type="deck", entity_id=comment.entity_id,
    )
    assert [(item.id, item.body) for item in restored] == [(comment.id, "Restore this comment")]
    ReleasePublisher(storage, repository, clock=lambda: NOW).validate_release(
        actor_user_id=owner.id, share_id=share.id, version=1,
    )


def test_backup_is_owner_only_and_contains_no_private_deck(tmp_path: Path):
    _, _, owner, member, stranger, share, _, _, manager = _stack(tmp_path)
    for actor in (member, stranger):
        with pytest.raises(AuthorizationError):
            manager.create(actor_user_id=actor.id, share_id=share.id)

    backup = manager.create(actor_user_id=owner.id, share_id=share.id)
    with tarfile.open(backup.archive, "r:gz") as archive:
        assert b"PRIVATE-SENTINEL" not in b"".join(
            archive.extractfile(item).read()
            for item in archive.getmembers() if item.isfile()
        )


def test_tampered_backup_rejected_without_changing_active_share(tmp_path: Path):
    storage, _, owner, _, _, share, _, _, manager = _stack(tmp_path)
    backup = manager.create(actor_user_id=owner.id, share_id=share.id)
    before = storage.share_paths(share.id).collection.read_bytes()
    backup.archive.write_bytes(backup.archive.read_bytes() + b"tamper")
    with pytest.raises(BackupIntegrityError, match="archive checksum"):
        manager.restore(actor_user_id=owner.id, share_id=share.id, archive=backup.archive)
    assert storage.share_paths(share.id).collection.read_bytes() == before


def test_archive_links_and_cross_share_restore_are_rejected(tmp_path: Path):
    storage, repository, owner, _, _, share, _, _, manager = _stack(tmp_path)
    other = SharingService(repository, clock=lambda: NOW).create_share(
        actor_user_id=owner.id, name="Other",
    )
    backup = manager.create(actor_user_id=owner.id, share_id=share.id)
    with pytest.raises(BackupIntegrityError, match="different share"):
        manager.restore(actor_user_id=owner.id, share_id=other.id, archive=backup.archive)

    evil = storage.share_paths(share.id).backups / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as archive:
        link = tarfile.TarInfo("payload/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
        manifest = json.dumps({"schema": 1, "share_id": share.id, "files": {}, "comments": []})
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest.encode())
        archive.addfile(info, io.BytesIO(manifest.encode()))
    evil.with_name(evil.name + ".sha256").write_text(
        manager.sha256_file(evil) + "  " + evil.name + "\n",
    )
    with pytest.raises(BackupIntegrityError, match="regular files"):
        manager.verify(evil)
