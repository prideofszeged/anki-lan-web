from __future__ import annotations

import asyncio
import io
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from anki.collection import Collection
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ankiweb.adapters.anki.sharing import ReleasePublisher, WorkspaceProvisioner
from ankiweb.identity import (
    IdentityDatabase, IdentityRepository, IdentityService, request_digest,
)
from ankiweb.identity.http import CSRF_HEADER, build_identity_http
from ankiweb.identity import JobRepository, JobState
from ankiweb.identity.repository import AuthorizationError
from ankiweb.sharing import ShareRole, SharingRepository, SharingService
from ankiweb.sharing.backup import BackupIntegrityError, ShareBackupManager
from ankiweb.sharing.http import build_sharing_router
from ankiweb.sharing.jobs import SharingJobRunner
from ankiweb.tenancy import ResourceKey, RuntimeRegistry, StorageLayout


NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


class _Runtime:
    async def open(self):
        pass

    async def close(self):
        pass


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


@pytest.mark.asyncio
async def test_backup_and_restore_jobs_hold_share_maintenance_and_are_idempotent(tmp_path: Path):
    storage, repository, owner, _, _, share, _, _, manager = _stack(tmp_path)
    registry = RuntimeRegistry(lambda _key: _Runtime(), wait_seconds=1)
    runner = SharingJobRunner(
        jobs=JobRepository(repository.database), sharing=repository, storage=storage,
        registry=registry, backup_manager=manager, clock=lambda: NOW,
    )
    await runner.start()
    lease = await registry.acquire(ResourceKey.share(share.id))
    job = await runner.enqueue_share_backup(
        actor_user_id=owner.id, share_id=share.id, idempotency_key="backup-1",
    )
    replay = await runner.enqueue_share_backup(
        actor_user_id=owner.id, share_id=share.id, idempotency_key="backup-1",
    )
    assert replay.id == job.id
    await asyncio.sleep(0.02)
    assert runner.jobs.get(job.id).state is JobState.RUNNING
    await lease.release()
    for _ in range(200):
        done = runner.jobs.get(job.id)
        if done.state in {JobState.SUCCEEDED, JobState.FAILED}:
            break
        await asyncio.sleep(0.005)
    assert done.state is JobState.SUCCEEDED
    assert set(done.progress) == {"backup_name", "sha256"}

    paths = storage.share_paths(share.id)
    original = paths.collection.read_bytes()
    paths.collection.write_bytes(b"broken")
    restore = await runner.enqueue_share_restore(
        actor_user_id=owner.id, share_id=share.id,
        backup_name=done.progress["backup_name"], idempotency_key="restore-1",
    )
    for _ in range(200):
        restored = runner.jobs.get(restore.id)
        if restored.state in {JobState.SUCCEEDED, JobState.FAILED}:
            break
        await asyncio.sleep(0.005)
    assert restored.state is JobState.SUCCEEDED
    assert paths.collection.read_bytes() == original
    await runner.stop()
    await registry.drain()


def test_backup_http_is_csrf_protected_owner_only_and_path_safe(tmp_path: Path):
    _, repository, owner, member, _, share, _, _, manager = _stack(tmp_path)
    identity = IdentityService(
        IdentityRepository(repository.database),
        password_hasher=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1),
        clock=lambda: NOW,
    )
    identity.set_password(owner.id, "owner-password")
    identity.set_password(member.id, "member-password")
    identity_http = build_identity_http(identity, secure_cookie=False)
    jobs = JobRepository(repository.database)

    class _EnqueueOnly:
        backup_manager = manager

        @staticmethod
        def _key(value):
            if not value:
                raise ValueError("idempotency key required")

        async def enqueue_share_backup(self, *, actor_user_id, share_id, idempotency_key):
            self._key(idempotency_key)
            repository.require_owner(actor_user_id=actor_user_id, share_id=share_id)
            return jobs.create_or_get(
                actor_user_id=actor_user_id, resource_type="share",
                resource_id=share_id, capability="share.backup.create",
                idempotency_key=idempotency_key, request_hash=request_digest(b"backup"),
                progress={}, now=NOW,
            )[0]

        async def enqueue_share_restore(
            self, *, actor_user_id, share_id, backup_name, idempotency_key,
        ):
            self._key(idempotency_key)
            manager.resolve_archive(
                actor_user_id=actor_user_id, share_id=share_id, name=backup_name,
            )
            return jobs.create_or_get(
                actor_user_id=actor_user_id, resource_type="share",
                resource_id=share_id, capability="share.backup.restore",
                idempotency_key=idempotency_key, request_hash=request_digest(backup_name.encode()),
                progress={"backup_name": backup_name}, now=NOW,
            )[0]

    app = FastAPI()
    app.include_router(identity_http.router)
    app.include_router(build_sharing_router(
        SharingService(repository, clock=lambda: NOW), identity_http,
        job_runner=_EnqueueOnly(),
    ))

    def login(username, password):
        client = TestClient(app)
        response = client.post("/api/v1/auth/login", json={
            "username": username, "password": password,
        })
        assert response.status_code == 200
        return client, response.json()["csrf_token"]

    owner_client, owner_csrf = login("owner", "owner-password")
    member_client, member_csrf = login("member", "member-password")
    assert owner_client.get(f"/api/v1/shares/{share.id}/backups").json() == {"backups": []}
    assert member_client.get(f"/api/v1/shares/{share.id}/backups").status_code == 403
    endpoint = f"/api/v1/shares/{share.id}/backups"
    assert owner_client.post(endpoint, headers={"idempotency-key": "backup"}).status_code == 403
    assert owner_client.post(endpoint, headers={CSRF_HEADER: owner_csrf}).status_code == 422
    created = owner_client.post(endpoint, headers={
        CSRF_HEADER: owner_csrf, "idempotency-key": "backup",
    })
    assert created.status_code == 202
    assert member_client.post(endpoint, headers={
        CSRF_HEADER: member_csrf, "idempotency-key": "member-backup",
    }).status_code == 403
    restore = owner_client.post(f"/api/v1/shares/{share.id}/restores", headers={
        CSRF_HEADER: owner_csrf, "idempotency-key": "restore",
    }, json={"backup_name": "../escape.tar.gz"})
    assert restore.status_code == 422
