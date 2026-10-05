from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from anki.collection import Collection

from ankiweb.adapters.anki.sharing import (
    ImmutableReleaseError, ReleaseInstaller, ReleasePublisher, WorkspaceProvisioner,
)
from ankiweb.identity import IdentityDatabase, IdentityRepository
from ankiweb.identity.repository import AuthorizationError
from ankiweb.sharing import ShareRole, SharingRepository, SharingService
from ankiweb.tenancy import StorageLayout

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _add_note(col: Collection, deck: str, front: str, back: str) -> tuple[int, int]:
    deck_id = col.decks.id(deck)
    note = col.new_note(col.models.by_name("Basic"))
    note["Front"], note["Back"] = front, back
    col.add_note(note, deck_id)
    return int(note.id), int(deck_id)


def _stack(tmp_path):
    storage = StorageLayout(tmp_path / "data")
    identities = IdentityRepository(IdentityDatabase(storage.app_db))
    identities.initialize()
    owner = identities.create_user(username="owner", now=NOW)
    recipient = identities.create_user(username="recipient", now=NOW)
    stranger = identities.create_user(username="stranger", now=NOW)
    owner_paths = storage.prepare_user(owner.id)
    recipient_paths = storage.prepare_user(recipient.id)
    col = Collection(str(owner_paths.collection), server=False)
    try:
        media = Path(col.media.dir())
        (media / "shared.mp3").write_bytes(b"ID3-shared")
        (media / "private.mp3").write_bytes(b"ID3-private")
        _, shared_deck = _add_note(
            col, "Shared Greek", "γειά [sound:shared.mp3]", "hello",
        )
        _add_note(col, "Shared Greek::Verbs", "μιλάω", "I speak")
        _add_note(col, "Private Notes", "secret [sound:private.mp3]", "private")
    finally:
        col.close()
    col = Collection(str(recipient_paths.collection), server=False)
    try:
        note_id, _ = _add_note(col, "Personal", "my card", "mine")
        card_id = int(col.get_note(note_id).card_ids()[0])
        col.db.execute(
            "UPDATE cards SET due=123,ivl=17,factor=2450,reps=9,lapses=2 WHERE id=?",
            card_id,
        )
        col.db.execute(
            """INSERT INTO revlog(id,cid,usn,ease,ivl,lastIvl,factor,time,type)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            1900000000000, card_id, -1, 3, 17, 10, 2450, 1200, 1,
        )
    finally:
        col.close()
    sharing_repo = SharingRepository(identities.database)
    sharing = SharingService(sharing_repo, clock=lambda: NOW)
    share = sharing.create_share(actor_user_id=owner.id, name="Greek course")
    invite = sharing.create_invite(
        actor_user_id=owner.id, share_id=share.id, role=ShareRole.VIEWER,
        intended_user_id=recipient.id,
    )
    sharing.accept_invite(actor_user_id=recipient.id, token=invite.token)
    return storage, sharing_repo, owner, recipient, stranger, share, shared_deck, card_id


def _schedule_snapshot(path: Path, card_id: int):
    with sqlite3.connect(path) as conn:
        card = conn.execute(
            """SELECT id,due,ivl,factor,reps,lapses,queue,type,odue,odid,left
               FROM cards WHERE id=?""", (card_id,),
        ).fetchone()
        reviews = conn.execute("SELECT * FROM revlog WHERE cid=? ORDER BY id", (card_id,)).fetchall()
    return card, reviews


def test_sh4_workspace_is_selected_deck_only_and_release_is_immutable(tmp_path):
    storage, repository, owner, _, stranger, share, deck_id, _ = _stack(tmp_path)
    owner_collection = storage.user_paths(owner.id).collection
    source_digest = owner_collection.read_bytes()
    provisioner = WorkspaceProvisioner(storage)
    workspace = provisioner.create_from_owner_deck(
        actor_user_id=owner.id, owner_collection=owner_collection,
        share_id=share.id, deck_id=deck_id, authorize=repository.require_owner,
    )
    col = Collection(str(workspace.collection), server=False)
    try:
        fronts = {col.get_note(note_id)["Front"] for note_id in col.find_notes("")}
        assert fronts == {"γειά [sound:shared.mp3]", "μιλάω"}
        assert (workspace.media / "shared.mp3").exists()
        assert not (workspace.media / "private.mp3").exists()
    finally:
        col.close()
    assert provisioner.review_enabled is False
    assert owner_collection.read_bytes() == source_digest
    with pytest.raises(AuthorizationError):
        provisioner.create_from_owner_deck(
            actor_user_id=stranger.id,
            owner_collection=storage.user_paths(owner.id).collection,
            share_id=share.id, deck_id=deck_id, authorize=repository.require_owner,
        )

    publisher = ReleasePublisher(storage, repository, clock=lambda: NOW)
    release = publisher.publish(actor_user_id=owner.id, share_id=share.id)
    manifest = json.loads(release.manifest_path.read_text())
    assert release.version == 1
    assert manifest["share_id"] == share.id
    assert manifest["bundle_sha256"] == release.bundle_sha256
    assert publisher.validate_release(
        actor_user_id=owner.id, share_id=share.id, version=1,
    ).version == 1
    with pytest.raises(ImmutableReleaseError):
        publisher.publish(actor_user_id=owner.id, share_id=share.id, version=1)
    original_manifest = release.manifest_path.read_bytes()
    manifest["created_at"] = "tampered"
    release.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ImmutableReleaseError, match="manifest checksum"):
        publisher.validate_release(actor_user_id=owner.id, share_id=share.id, version=1)
    release.manifest_path.write_bytes(original_manifest)
    release.bundle_path.write_bytes(release.bundle_path.read_bytes() + b"corrupt")
    with pytest.raises(ImmutableReleaseError, match="checksum"):
        publisher.validate_release(actor_user_id=owner.id, share_id=share.id, version=1)
    with pytest.raises(AuthorizationError):
        publisher.validate_release(actor_user_id=stranger.id, share_id=share.id, version=1)


def test_sh5_copy_install_reproduces_shared_content_without_relationship(tmp_path):
    storage, repository, owner, recipient, _, share, deck_id, card_id = _stack(tmp_path)
    WorkspaceProvisioner(storage).create_from_owner_deck(
        actor_user_id=owner.id, owner_collection=storage.user_paths(owner.id).collection,
        share_id=share.id, deck_id=deck_id, authorize=repository.require_owner,
    )
    release = ReleasePublisher(storage, repository, clock=lambda: NOW).publish(
        actor_user_id=owner.id, share_id=share.id,
    )
    before = _schedule_snapshot(storage.user_paths(recipient.id).collection, card_id)
    installer = ReleaseInstaller(storage, repository, clock=lambda: NOW)
    result = installer.install(
        actor_user_id=recipient.id, share_id=share.id, version=release.version,
        mode="copy",
    )
    assert result.subscription_id is None
    with repository.database.read() as conn:
        assert conn.execute(
            "SELECT count(*) FROM share_subscriptions WHERE user_id=?", (recipient.id,),
        ).fetchone()[0] == 0
    assert _schedule_snapshot(storage.user_paths(recipient.id).collection, card_id) == before
    col = Collection(str(storage.user_paths(recipient.id).collection), server=False)
    try:
        assert col.find_notes('"γειά"')
        assert not col.find_notes('"secret"')
        assert (Path(col.media.dir()) / "shared.mp3").exists()
        assert not (Path(col.media.dir()) / "private.mp3").exists()
    finally:
        col.close()


def test_sh6_follow_install_maps_entities_and_preserves_history(tmp_path):
    storage, repository, owner, recipient, stranger, share, deck_id, card_id = _stack(tmp_path)
    WorkspaceProvisioner(storage).create_from_owner_deck(
        actor_user_id=owner.id, owner_collection=storage.user_paths(owner.id).collection,
        share_id=share.id, deck_id=deck_id, authorize=repository.require_owner,
    )
    release = ReleasePublisher(storage, repository, clock=lambda: NOW).publish(
        actor_user_id=owner.id, share_id=share.id,
    )
    before = _schedule_snapshot(storage.user_paths(recipient.id).collection, card_id)
    installer = ReleaseInstaller(storage, repository, clock=lambda: NOW)
    result = installer.install(
        actor_user_id=recipient.id, share_id=share.id, version=release.version,
        mode="follow",
    )
    assert result.subscription_id
    assert result.installed_release == 1
    mappings = repository.list_subscription_entities(
        actor_user_id=recipient.id, subscription_id=result.subscription_id,
    )
    assert len([item for item in mappings if item.entity_type == "note"]) == 2
    assert _schedule_snapshot(storage.user_paths(recipient.id).collection, card_id) == before
    with pytest.raises(AuthorizationError):
        installer.install(
            actor_user_id=stranger.id, share_id=share.id, version=1, mode="copy",
        )


def test_install_quota_failure_leaves_recipient_root_unchanged(tmp_path):
    storage, repository, owner, recipient, _, share, deck_id, card_id = _stack(tmp_path)
    WorkspaceProvisioner(storage).create_from_owner_deck(
        actor_user_id=owner.id, owner_collection=storage.user_paths(owner.id).collection,
        share_id=share.id, deck_id=deck_id, authorize=repository.require_owner,
    )
    ReleasePublisher(storage, repository, clock=lambda: NOW).publish(
        actor_user_id=owner.id, share_id=share.id,
    )
    before_bytes = storage.user_paths(recipient.id).collection.read_bytes()
    with repository.database.transaction() as conn:
        conn.execute("UPDATE user_quotas SET storage_bytes=1 WHERE user_id=?", (recipient.id,))
    with pytest.raises(Exception, match="quota"):
        ReleaseInstaller(storage, repository, clock=lambda: NOW).install(
            actor_user_id=recipient.id, share_id=share.id, version=1, mode="copy",
        )
    assert storage.user_paths(recipient.id).collection.read_bytes() == before_bytes
    assert _schedule_snapshot(storage.user_paths(recipient.id).collection, card_id)[0]


def test_copy_install_final_membership_reauthorization_preserves_original_on_revoke(tmp_path):
    storage, repository, owner, recipient, _, share, deck_id, card_id = _stack(tmp_path)
    WorkspaceProvisioner(storage).create_from_owner_deck(
        actor_user_id=owner.id, owner_collection=storage.user_paths(owner.id).collection,
        share_id=share.id, deck_id=deck_id, authorize=repository.require_owner,
    )
    ReleasePublisher(storage, repository, clock=lambda: NOW).publish(
        actor_user_id=owner.id, share_id=share.id,
    )
    before = _schedule_snapshot(storage.user_paths(recipient.id).collection, card_id)

    def revoked(**_kwargs):
        raise AuthorizationError("membership revoked")

    with pytest.raises(AuthorizationError, match="revoked"):
        ReleaseInstaller(storage, repository, clock=lambda: NOW).install(
            actor_user_id=recipient.id, share_id=share.id, version=1, mode="copy",
            authorize_before_commit=revoked,
        )
    assert _schedule_snapshot(storage.user_paths(recipient.id).collection, card_id) == before
    col = Collection(str(storage.user_paths(recipient.id).collection), server=False)
    try:
        assert not col.find_notes('"γειά"')
        assert col.find_notes('"my card"')
    finally:
        col.close()


def test_revoked_restart_restores_uncommitted_follow_swap_from_trusted_release(tmp_path):
    storage, repository, owner, recipient, _, share, deck_id, _ = _stack(tmp_path)
    WorkspaceProvisioner(storage).create_from_owner_deck(
        actor_user_id=owner.id, owner_collection=storage.user_paths(owner.id).collection,
        share_id=share.id, deck_id=deck_id, authorize=repository.require_owner,
    )
    ReleasePublisher(storage, repository, clock=lambda: NOW).publish(
        actor_user_id=owner.id, share_id=share.id,
    )
    paths = storage.user_paths(recipient.id)
    saved_old = tmp_path / "saved-old"
    shutil.copytree(paths.root, saved_old)
    installer = ReleaseInstaller(storage, repository, clock=lambda: NOW)
    installer.install(
        actor_user_id=recipient.id, share_id=share.id, version=1, mode="copy",
    )
    operation = str(uuid.uuid4())
    quarantine = storage.users_root / f".{recipient.id}.install-old-{operation}"
    shutil.copytree(saved_old, quarantine)
    with repository.database.transaction() as conn:
        conn.execute(
            "UPDATE share_members SET state='removed' WHERE share_id=? AND user_id=?",
            (share.id, recipient.id),
        )
    with pytest.raises(AuthorizationError):
        ReleasePublisher(storage, repository).validate_release(
            actor_user_id=recipient.id, share_id=share.id, version=1,
        )
    assert installer.recover_interrupted(
        actor_user_id=recipient.id, share_id=share.id, version=1,
        mode="follow", operation_id=operation,
    ) is None
    col = Collection(str(paths.collection), server=False)
    try:
        assert not col.find_notes('"γειά"')
        assert col.find_notes('"my card"')
    finally:
        col.close()
    assert not quarantine.exists()


def test_revoked_restart_retains_committed_follow_and_cleans_residue(tmp_path):
    storage, repository, owner, recipient, _, share, deck_id, _ = _stack(tmp_path)
    WorkspaceProvisioner(storage).create_from_owner_deck(
        actor_user_id=owner.id, owner_collection=storage.user_paths(owner.id).collection,
        share_id=share.id, deck_id=deck_id, authorize=repository.require_owner,
    )
    ReleasePublisher(storage, repository, clock=lambda: NOW).publish(
        actor_user_id=owner.id, share_id=share.id,
    )
    installer = ReleaseInstaller(storage, repository, clock=lambda: NOW)
    operation = str(uuid.uuid4())
    installed = installer.install(
        actor_user_id=recipient.id, share_id=share.id, version=1,
        mode="follow", operation_id=operation,
    )
    with repository.database.transaction() as conn:
        conn.execute(
            "UPDATE share_members SET state='removed' WHERE share_id=? AND user_id=?",
            (share.id, recipient.id),
        )
    recovered = installer.recover_interrupted(
        actor_user_id=recipient.id, share_id=share.id, version=1,
        mode="follow", operation_id=operation,
    )
    assert recovered == installed
    col = Collection(str(storage.user_paths(recipient.id).collection), server=False)
    try:
        assert col.find_notes('"γειά"')
    finally:
        col.close()


def test_release_path_tamper_and_failed_validation_never_commit_artifact(tmp_path, monkeypatch):
    storage, repository, owner, _, _, share, deck_id, _ = _stack(tmp_path)
    WorkspaceProvisioner(storage).create_from_owner_deck(
        actor_user_id=owner.id, owner_collection=storage.user_paths(owner.id).collection,
        share_id=share.id, deck_id=deck_id, authorize=repository.require_owner,
    )
    publisher = ReleasePublisher(storage, repository, clock=lambda: NOW)
    monkeypatch.setattr(
        publisher, "_validate_reimport",
        lambda *_args: (_ for _ in ()).throw(ImmutableReleaseError("validation failed")),
    )
    with pytest.raises(ImmutableReleaseError, match="validation"):
        publisher.publish(actor_user_id=owner.id, share_id=share.id)
    with repository.database.read() as conn:
        assert conn.execute("SELECT count(*) FROM share_releases").fetchone()[0] == 0
        assert conn.execute(
            "SELECT current_release FROM deck_shares WHERE id=?", (share.id,),
        ).fetchone()[0] is None
    releases = storage.share_paths(share.id).releases
    assert not [path for path in releases.iterdir() if path.is_dir()]

    monkeypatch.undo()
    release = publisher.publish(actor_user_id=owner.id, share_id=share.id)
    with repository.database.transaction() as conn:
        conn.execute(
            "UPDATE share_releases SET bundle_path='../../private/collection.anki2' WHERE share_id=?",
            (share.id,),
        )
    with pytest.raises(ImmutableReleaseError, match="path"):
        publisher.validate_release(
            actor_user_id=owner.id, share_id=share.id, version=release.version,
        )
