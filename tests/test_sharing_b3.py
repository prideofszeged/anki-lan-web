from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from anki.collection import Collection

from ankiweb.adapters.anki.collaboration import (
    DestructiveTemplateChangeError,
    EditConflictError,
    MirrorPreviewRequired,
    SubscriptionUpdater,
    UnresolvedUpdateError,
    WorkspaceCollaboration,
    _tree_digest,
)
from ankiweb.adapters.anki.sharing import (
    ReleaseInstaller,
    ReleasePublisher,
    WorkspaceProvisioner,
)
from ankiweb.identity import IdentityDatabase, IdentityRepository
from ankiweb.identity.jobs import JobState
from ankiweb.identity.repository import AuthorizationError, NotFoundError
from ankiweb.sharing import ShareRole, SharingRepository, SharingService
from ankiweb.tenancy import StorageLayout

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _add_note(col: Collection, deck: str, front: str, back: str):
    did = int(col.decks.id(deck))
    note = col.new_note(col.models.by_name("Basic"))
    note["Front"], note["Back"] = front, back
    col.add_note(note, did)
    return note.guid, did


def _field(path: Path, guid: str, name: str) -> str:
    col = Collection(str(path), server=False)
    try:
        note_id = col.db.scalar("SELECT id FROM notes WHERE guid=?", guid)
        return col.get_note(note_id)[name]
    finally:
        col.close()


def _tags(path: Path, guid: str) -> set[str]:
    col = Collection(str(path), server=False)
    try:
        note_id = col.db.scalar("SELECT id FROM notes WHERE guid=?", guid)
        return set(col.get_note(note_id).tags) if note_id else set()
    finally:
        col.close()


def _stack(tmp_path):
    storage = StorageLayout(tmp_path / "data")
    identities = IdentityRepository(IdentityDatabase(storage.app_db))
    identities.initialize()
    owner = identities.create_user(username="owner", now=NOW)
    editor = identities.create_user(username="editor", now=NOW)
    viewer = identities.create_user(username="viewer", now=NOW)
    stranger = identities.create_user(username="stranger", now=NOW)
    for user in (owner, editor, viewer, stranger):
        storage.prepare_user(user.id)
        empty = Collection(str(storage.user_paths(user.id).collection), server=False)
        empty.close()
    col = Collection(str(storage.user_paths(owner.id).collection), server=False)
    try:
        (Path(col.media.dir()) / "shared.mp3").write_bytes(b"base-media")
        guid, did = _add_note(col, "Greek", "hello [sound:shared.mp3]", "base")
    finally:
        col.close()
    repository = SharingRepository(identities.database)
    service = SharingService(repository, clock=lambda: NOW)
    share = service.create_share(actor_user_id=owner.id, name="Greek")
    for user, role in ((editor, ShareRole.EDITOR), (viewer, ShareRole.VIEWER)):
        grant = service.create_invite(
            actor_user_id=owner.id, share_id=share.id, role=role,
            intended_user_id=user.id,
        )
        service.accept_invite(actor_user_id=user.id, token=grant.token)
    WorkspaceProvisioner(storage).create_from_owner_deck(
        actor_user_id=owner.id,
        owner_collection=storage.user_paths(owner.id).collection,
        share_id=share.id,
        deck_id=did,
        authorize=repository.require_owner,
    )
    release = ReleasePublisher(storage, repository, clock=lambda: NOW).publish(
        actor_user_id=owner.id, share_id=share.id,
    )
    installed = ReleaseInstaller(storage, repository, clock=lambda: NOW).install(
        actor_user_id=viewer.id, share_id=share.id, version=release.version,
        mode="follow",
    )
    return storage, repository, service, owner, editor, viewer, stranger, share, guid, installed


def test_sh7_three_way_conflict_stops_update_and_explicit_resolution_preserves_history(tmp_path):
    storage, repository, _, owner, _, viewer, _, share, guid, installed = _stack(tmp_path)
    recipient = storage.user_paths(viewer.id).collection
    with sqlite3.connect(recipient) as conn:
        before_cards = conn.execute("SELECT id,due,ivl,reps,lapses FROM cards").fetchall()
        before_reviews = conn.execute("SELECT * FROM revlog").fetchall()
    local = Collection(str(recipient), server=False)
    try:
        note = local.get_note(local.db.scalar("SELECT id FROM notes WHERE guid=?", guid))
        note["Back"] = "my local answer"
        local.update_note(note)
    finally:
        local.close()
    workspace = WorkspaceCollaboration(storage, repository, clock=lambda: NOW)
    workspace.edit_note(
        actor_user_id=owner.id, share_id=share.id, guid=guid,
        fields={"Back": "upstream answer"}, expected_revision=0,
    )
    ReleasePublisher(storage, repository, clock=lambda: NOW).publish(
        actor_user_id=owner.id, share_id=share.id,
    )
    updater = SubscriptionUpdater(storage, repository, clock=lambda: NOW)
    with pytest.raises(UnresolvedUpdateError) as raised:
        updater.run(
            actor_user_id=viewer.id, subscription_id=installed.subscription_id,
            target_version=2, idempotency_key="update-v2",
        )
    conflict = raised.value.conflicts[0]
    assert (conflict.field_name, conflict.local_value, conflict.upstream_value) == (
        "Back", "my local answer", "upstream answer",
    )
    assert _field(recipient, guid, "Back") == "my local answer"
    assert repository.get_subscription(
        actor_user_id=viewer.id, subscription_id=installed.subscription_id,
    ).installed_release == 1

    repository.resolve_conflict(
        actor_user_id=viewer.id, job_id=raised.value.job_id,
        conflict_id=conflict.id, resolution="upstream", now=NOW,
    )
    result = updater.run(
        actor_user_id=viewer.id, subscription_id=installed.subscription_id,
        target_version=2, idempotency_key="update-v2",
    )
    assert result.applied and result.installed_release == 2
    assert _field(recipient, guid, "Back") == "upstream answer"
    with sqlite3.connect(recipient) as conn:
        assert conn.execute("SELECT id,due,ivl,reps,lapses FROM cards").fetchall() == before_cards
        assert conn.execute("SELECT * FROM revlog").fetchall() == before_reviews
    with repository.database.read() as conn:
        audit = conn.execute(
            "SELECT metadata_json FROM audit_events WHERE action='share.conflict.resolved'"
        ).fetchone()[0]
    assert "my local answer" not in audit and "upstream answer" not in audit


def test_sh8_tombstone_retires_by_default_and_mirror_requires_exact_preview(tmp_path):
    storage, repository, _, owner, _, viewer, _, share, guid, installed = _stack(tmp_path)
    workspace_path = storage.share_paths(share.id).collection
    col = Collection(str(workspace_path), server=False)
    try:
        col.remove_notes([col.db.scalar("SELECT id FROM notes WHERE guid=?", guid)])
    finally:
        col.close()
    ReleasePublisher(storage, repository, clock=lambda: NOW).publish(
        actor_user_id=owner.id, share_id=share.id,
    )
    updater = SubscriptionUpdater(storage, repository, clock=lambda: NOW)
    result = updater.run(
        actor_user_id=viewer.id, subscription_id=installed.subscription_id,
        target_version=2, idempotency_key="retire-v2",
    )
    assert result.applied
    assert "ankiweb::retired" in _tags(storage.user_paths(viewer.id).collection, guid)

    # A second subscriber exercises mirror mode independently.
    mirror = repository.database
    identities = IdentityRepository(mirror)
    mirror_user = identities.create_user(username="mirror", now=NOW)
    storage.prepare_user(mirror_user.id)
    empty = Collection(str(storage.user_paths(mirror_user.id).collection), server=False)
    empty.close()
    service = SharingService(repository, clock=lambda: NOW)
    grant = service.create_invite(
        actor_user_id=owner.id, share_id=share.id, role=ShareRole.VIEWER,
        intended_user_id=mirror_user.id,
    )
    service.accept_invite(actor_user_id=mirror_user.id, token=grant.token)
    mirror_install = ReleaseInstaller(storage, repository, clock=lambda: NOW).install(
        actor_user_id=mirror_user.id, share_id=share.id, version=1, mode="follow",
    )
    repository.set_subscription_policy(
        actor_user_id=mirror_user.id,
        subscription_id=mirror_install.subscription_id,
        policy="mirror", now=NOW,
    )
    with pytest.raises(MirrorPreviewRequired):
        updater.run(
            actor_user_id=mirror_user.id,
            subscription_id=mirror_install.subscription_id,
            target_version=2, idempotency_key="mirror-v2",
        )
    assert _field(storage.user_paths(mirror_user.id).collection, guid, "Front").startswith("hello")
    preview = updater.preview_mirror(
        actor_user_id=mirror_user.id,
        subscription_id=mirror_install.subscription_id,
        target_version=2,
    )
    assert preview.tombstones == (guid,)
    updater.run(
        actor_user_id=mirror_user.id,
        subscription_id=mirror_install.subscription_id,
        target_version=2, idempotency_key="mirror-v2",
        mirror_preview_digest=preview.digest,
    )
    col = Collection(str(storage.user_paths(mirror_user.id).collection), server=False)
    try:
        assert col.db.scalar("SELECT id FROM notes WHERE guid=?", guid) is None
    finally:
        col.close()


def test_sh9_workspace_revision_comments_and_cross_share_idor(tmp_path):
    storage, repository, service, owner, editor, viewer, stranger, share, guid, _ = _stack(tmp_path)
    workspace = WorkspaceCollaboration(storage, repository, clock=lambda: NOW)
    winning = workspace.edit_note(
        actor_user_id=editor.id, share_id=share.id, guid=guid,
        fields={"Back": "winner"}, expected_revision=0,
    )
    assert winning.revision == 1
    with pytest.raises(EditConflictError) as raised:
        workspace.edit_note(
            actor_user_id=owner.id, share_id=share.id, guid=guid,
            fields={"Back": "stale loser"}, expected_revision=0,
        )
    assert raised.value.latest.fields["Back"] == "winner"
    assert _field(storage.share_paths(share.id).collection, guid, "Back") == "winner"
    with pytest.raises(AuthorizationError):
        workspace.edit_note(
            actor_user_id=viewer.id, share_id=share.id, guid=guid,
            fields={"Back": "forbidden"}, expected_revision=1,
        )
    comment = repository.add_workspace_comment(
        actor_user_id=viewer.id, share_id=share.id,
        entity_type="note", entity_id=guid, body="Looks good — see [Back](#back).", now=NOW,
    )
    assert comment.body.startswith("Looks good")
    edited = repository.edit_workspace_comment(
        actor_user_id=viewer.id, share_id=share.id, comment_id=comment.id,
        body="Updated locally.", now=NOW + timedelta(minutes=14),
    )
    assert edited.body == "Updated locally."
    with pytest.raises(Exception, match="edit window"):
        repository.edit_workspace_comment(
            actor_user_id=viewer.id, share_id=share.id, comment_id=comment.id,
            body="Too late", now=NOW + timedelta(minutes=16),
        )
    resolved = repository.resolve_workspace_comment(
        actor_user_id=editor.id, share_id=share.id,
        comment_id=comment.id, now=NOW + timedelta(minutes=17),
    )
    assert resolved.resolved_at is not None
    with pytest.raises(ValueError):
        repository.add_workspace_comment(
            actor_user_id=viewer.id, share_id=share.id,
            entity_type="note", entity_id=guid,
            body="[leak](https://outside.example)", now=NOW,
        )
    with pytest.raises(AuthorizationError):
        repository.add_workspace_comment(
            actor_user_id=stranger.id, share_id=share.id,
            entity_type="note", entity_id=guid, body="private?", now=NOW,
        )
    other = service.create_share(actor_user_id=owner.id, name="Other")
    with pytest.raises((AuthorizationError, NotFoundError)):
        repository.get_workspace_revision(
            actor_user_id=editor.id, share_id=other.id,
            entity_type="note", entity_id=guid,
        )


def test_sh13_destructive_template_change_rejects_without_state_change(tmp_path):
    storage, repository, _, owner, _, viewer, _, share, guid, installed = _stack(tmp_path)
    workspace_path = storage.share_paths(share.id).collection
    col = Collection(str(workspace_path), server=False)
    try:
        note_id = col.db.scalar("SELECT id FROM notes WHERE guid=?", guid)
        model = col.get_note(note_id).note_type()
        template = col.models.new_template("Second")
        template["qfmt"], template["afmt"] = "{{Back}}", "{{FrontSide}}<hr>{{Front}}"
        col.models.add_template(model, template)
        col.models.update_dict(model)
    finally:
        col.close()
    # Release 2 establishes a two-template baseline and is installed normally by a fresh stack
    # update only when it is non-destructive.
    ReleasePublisher(storage, repository, clock=lambda: NOW).publish(
        actor_user_id=owner.id, share_id=share.id,
    )
    updater = SubscriptionUpdater(storage, repository, clock=lambda: NOW)
    updater.run(
        actor_user_id=viewer.id, subscription_id=installed.subscription_id,
        target_version=2, idempotency_key="templates-v2", approve_templates=True,
    )
    col = Collection(str(workspace_path), server=False)
    try:
        note_id = col.db.scalar("SELECT id FROM notes WHERE guid=?", guid)
        model = col.get_note(note_id).note_type()
        col.models.remove_template(model, model["tmpls"][1])
        col.models.update_dict(model)
    finally:
        col.close()
    ReleasePublisher(storage, repository, clock=lambda: NOW).publish(
        actor_user_id=owner.id, share_id=share.id,
    )
    before = storage.user_paths(viewer.id).collection.read_bytes()
    with pytest.raises(DestructiveTemplateChangeError):
        updater.run(
            actor_user_id=viewer.id, subscription_id=installed.subscription_id,
            target_version=3, idempotency_key="templates-v3",
        )
    assert storage.user_paths(viewer.id).collection.read_bytes() == before
    assert repository.get_subscription(
        actor_user_id=viewer.id, subscription_id=installed.subscription_id,
    ).installed_release == 2


def test_sh10_update_reauthorizes_before_commit_and_rolls_back_member_revocation(
    tmp_path, monkeypatch,
):
    storage, repository, service, owner, _, viewer, _, share, guid, installed = _stack(tmp_path)
    WorkspaceCollaboration(storage, repository, clock=lambda: NOW).edit_note(
        actor_user_id=owner.id, share_id=share.id, guid=guid,
        fields={"Back": "safe upstream"}, expected_revision=0,
    )
    ReleasePublisher(storage, repository, clock=lambda: NOW).publish(
        actor_user_id=owner.id, share_id=share.id,
    )
    recipient = storage.user_paths(viewer.id).collection
    before = recipient.read_bytes()
    real_commit = repository.commit_subscription_update

    def revoke_then_commit(**kwargs):
        service.remove_member(
            actor_user_id=owner.id, share_id=share.id, target_user_id=viewer.id,
        )
        return real_commit(**kwargs)

    monkeypatch.setattr(repository, "commit_subscription_update", revoke_then_commit)
    with pytest.raises(AuthorizationError):
        SubscriptionUpdater(storage, repository, clock=lambda: NOW).run(
            actor_user_id=viewer.id, subscription_id=installed.subscription_id,
            target_version=2, idempotency_key="revoked-at-commit",
        )
    assert recipient.read_bytes() == before
    with repository.database.read() as conn:
        assert conn.execute(
            "SELECT installed_release FROM share_subscriptions WHERE id=?",
            (installed.subscription_id,),
        ).fetchone()[0] == 1


def test_sh7_divergent_media_is_conflict_and_never_silently_overwritten(tmp_path):
    storage, repository, _, owner, _, viewer, _, share, _, installed = _stack(tmp_path)
    local_media = storage.user_paths(viewer.id).media / "shared.mp3"
    upstream_media = storage.share_paths(share.id).media / "shared.mp3"
    local_media.write_bytes(b"my-local-media")
    upstream_media.write_bytes(b"new-upstream-media")
    ReleasePublisher(storage, repository, clock=lambda: NOW).publish(
        actor_user_id=owner.id, share_id=share.id,
    )
    updater = SubscriptionUpdater(storage, repository, clock=lambda: NOW)
    with pytest.raises(UnresolvedUpdateError) as raised:
        updater.run(
            actor_user_id=viewer.id, subscription_id=installed.subscription_id,
            target_version=2, idempotency_key="media-v2",
        )
    conflict = next(item for item in raised.value.conflicts if item.record.entity_type == "media")
    assert local_media.read_bytes() == b"my-local-media"
    with pytest.raises(ValueError, match="binary media"):
        repository.resolve_conflict(
            actor_user_id=viewer.id, job_id=raised.value.job_id,
            conflict_id=conflict.id, resolution="manual", now=NOW,
        )
    repository.resolve_conflict(
        actor_user_id=viewer.id, job_id=raised.value.job_id,
        conflict_id=conflict.id, resolution="mine", now=NOW,
    )
    updater.run(
        actor_user_id=viewer.id, subscription_id=installed.subscription_id,
        target_version=2, idempotency_key="media-v2",
    )
    assert local_media.read_bytes() == b"my-local-media"


def test_new_upstream_media_same_name_collision_requires_resolution(tmp_path):
    storage, repository, _, owner, _, viewer, _, share, guid, installed = _stack(tmp_path)
    local_collision = storage.user_paths(viewer.id).media / "collision.mp3"
    local_collision.write_bytes(b"unrelated-recipient-file")
    (storage.share_paths(share.id).media / "collision.mp3").write_bytes(b"upstream-file")
    WorkspaceCollaboration(storage, repository, clock=lambda: NOW).edit_note(
        actor_user_id=owner.id, share_id=share.id, guid=guid,
        fields={"Back": "listen [sound:collision.mp3]"}, expected_revision=0,
    )
    ReleasePublisher(storage, repository, clock=lambda: NOW).publish(
        actor_user_id=owner.id, share_id=share.id,
    )
    with pytest.raises(UnresolvedUpdateError) as raised:
        SubscriptionUpdater(storage, repository, clock=lambda: NOW).run(
            actor_user_id=viewer.id, subscription_id=installed.subscription_id,
            target_version=2, idempotency_key="collision-v2",
        )
    assert any(item.record.entity_type == "media" for item in raised.value.conflicts)
    assert local_collision.read_bytes() == b"unrelated-recipient-file"


def test_manual_conflicts_require_all_values_atomically_after_restart(tmp_path):
    storage, repository, _, owner, _, viewer, _, share, guid, installed = _stack(tmp_path)
    recipient = storage.user_paths(viewer.id).collection
    local = Collection(str(recipient), server=False)
    try:
        note = local.get_note(local.db.scalar("SELECT id FROM notes WHERE guid=?", guid))
        note["Front"], note["Back"] = "local front", "local back"
        local.update_note(note)
    finally:
        local.close()
    WorkspaceCollaboration(storage, repository, clock=lambda: NOW).edit_note(
        actor_user_id=owner.id, share_id=share.id, guid=guid,
        fields={"Front": "upstream front", "Back": "upstream back"},
        expected_revision=0,
    )
    ReleasePublisher(storage, repository, clock=lambda: NOW).publish(
        actor_user_id=owner.id, share_id=share.id,
    )
    with pytest.raises(UnresolvedUpdateError) as raised:
        SubscriptionUpdater(storage, repository, clock=lambda: NOW).run(
            actor_user_id=viewer.id, subscription_id=installed.subscription_id,
            target_version=2, idempotency_key="manual-v2",
        )
    by_field = {item.field_name: item for item in raised.value.conflicts}
    for conflict in by_field.values():
        repository.resolve_conflict(
            actor_user_id=viewer.id, job_id=raised.value.job_id,
            conflict_id=conflict.id, resolution="manual", now=NOW,
        )
    restarted = SubscriptionUpdater(storage, repository, clock=lambda: NOW)
    with pytest.raises(UnresolvedUpdateError):
        restarted.run(
            actor_user_id=viewer.id, subscription_id=installed.subscription_id,
            target_version=2, idempotency_key="manual-v2",
            manual_values={by_field["Front"].id: "merged front"},
        )
    restarted.run(
        actor_user_id=viewer.id, subscription_id=installed.subscription_id,
        target_version=2, idempotency_key="manual-v2",
        manual_values={
            by_field["Front"].id: "merged front",
            by_field["Back"].id: "merged back",
        },
    )
    assert _field(recipient, guid, "Front") == "merged front"
    assert _field(recipient, guid, "Back") == "merged back"


@pytest.mark.parametrize("phase", ["intent", "old_quarantined", "new_active", "db_committed"])
def test_update_swap_recovery_yields_verified_old_or_committed_new(tmp_path, phase):
    storage, repository, _, _, _, viewer, _, _, guid, installed = _stack(tmp_path)
    updater = SubscriptionUpdater(storage, repository, clock=lambda: NOW)
    active = storage.user_paths(viewer.id).root
    stage = storage.users_root / f".{viewer.id}.test-stage"
    quarantine = storage.users_root / f".{viewer.id}.test-old"
    shutil.copytree(active, stage)
    staged_collection = stage / "anki" / "collection.anki2"
    col = Collection(str(staged_collection), server=False)
    try:
        note = col.get_note(col.db.scalar("SELECT id FROM notes WHERE guid=?", guid))
        note["Back"] = "recovered new"
        col.update_note(note)
    finally:
        col.close()
    old_hash = _tree_digest(active)
    new_hash = _tree_digest(stage)
    intent = {
        "user_id": viewer.id, "subscription_id": installed.subscription_id,
        "expected_version": 1, "target_version": 2,
        "stage_key": stage.name, "quarantine_key": quarantine.name,
        "old_root_sha256": old_hash, "new_root_sha256": new_hash,
    }
    if phase in {"old_quarantined", "new_active", "db_committed"}:
        os.replace(active, quarantine)
    if phase in {"new_active", "db_committed"}:
        os.replace(stage, active)
    if phase == "db_committed":
        with repository.database.transaction() as conn:
            conn.execute(
                "UPDATE share_subscriptions SET installed_release=2 WHERE id=?",
                (installed.subscription_id,),
            )
    updater._recover_swap("missing-job", viewer.id, intent)
    expected = "recovered new" if phase == "db_committed" else "base"
    assert _field(storage.user_paths(viewer.id).collection, guid, "Back") == expected
    assert not stage.exists() and not quarantine.exists()


def test_recover_all_cleans_pre_intent_roots_and_marks_job_retryable_failure(tmp_path):
    storage, repository, _, _, _, viewer, _, _, _, installed = _stack(tmp_path)
    updater = SubscriptionUpdater(storage, repository, clock=lambda: NOW)
    job, _ = updater.jobs.create_or_get(
        actor_user_id=viewer.id, resource_type="subscription",
        resource_id=installed.subscription_id, capability="share.update",
        idempotency_key="crashed-pre-intent", request_hash=b"x" * 32, now=NOW,
    )
    updater.jobs.transition(
        job.id, expected=JobState.QUEUED, target=JobState.RUNNING, now=NOW,
    )
    stage = storage.users_root / f".{viewer.id}.orphan-stage"
    upstream = storage.users_root / f".{viewer.id}.orphan-upstream"
    stage.mkdir(mode=0o700)
    upstream.mkdir(mode=0o700)
    updater.jobs.append_journal(job.id, phase="stage", payload={
        "user_id": viewer.id, "stage_key": stage.name,
        "upstream_key": upstream.name,
    }, now=NOW)
    assert updater.recover_all() == 1
    assert not stage.exists() and not upstream.exists()
    recovered = updater.jobs.get(job.id)
    assert recovered.state is JobState.FAILED
    assert recovered.error_code == "recovered_pre_swap"
