from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from anki.collection import Collection

from ankiweb.adapters.anki.sharing import ReleasePublisher, WorkspaceProvisioner
from ankiweb.adapters.anki.collaboration import UnresolvedUpdateError
from ankiweb.identity import (
    IdentityDatabase, IdentityRepository, JobRepository, JobState, request_digest,
)
from ankiweb.sharing import ShareRole, SharingRepository, SharingService
from ankiweb.sharing.jobs import (
    PUBLISH_RELEASE, SharingJobRunner, WORKSPACE_PROVISION,
)
from ankiweb.tenancy import ResourceKey, RuntimeRegistry, StorageLayout

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


class FakeRuntime:
    def __init__(self) -> None:
        self.closed = False

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class RecordingProvisioner:
    def __init__(self, storage: StorageLayout) -> None:
        self.storage = storage
        self.calls: list[tuple[str, int]] = []

    def create_from_owner_deck(
        self, *, actor_user_id, owner_collection, share_id, deck_id, authorize,
    ):
        authorize(actor_user_id=actor_user_id, share_id=share_id)
        paths = self.storage.prepare_share(share_id)
        paths.collection.write_bytes(b"workspace")
        self.calls.append((share_id, deck_id))
        return paths

    @staticmethod
    def _validate_workspace(collection: Path) -> None:
        if collection.read_bytes() != b"workspace":
            raise RuntimeError("invalid workspace")


class RecordingPublisher:
    def __init__(self, storage: StorageLayout) -> None:
        self.storage = storage
        self.calls: list[tuple[str, int]] = []

    def publish(self, *, actor_user_id, share_id, version):
        self.calls.append((share_id, version))
        paths = self.storage.share_paths(share_id)
        release = paths.releases / str(version)
        release.mkdir(parents=True, mode=0o700)
        manifest = release / "manifest.json"
        bundle = release / "deck.apkg"
        manifest.write_bytes(b"manifest")
        bundle.write_bytes(b"bundle")
        return SimpleNamespace(
            share_id=share_id, version=version, manifest_path=manifest,
            bundle_path=bundle, manifest_sha256="a" * 64, bundle_sha256="b" * 64,
        )

    def validate_release(self, *, actor_user_id, share_id, version):
        release = self.storage.share_paths(share_id).releases / str(version)
        if not (release / "manifest.json").exists():
            raise FileNotFoundError("release missing")
        return SimpleNamespace(
            share_id=share_id, version=version,
            manifest_path=release / "manifest.json", bundle_path=release / "deck.apkg",
            manifest_sha256="a" * 64, bundle_sha256="b" * 64,
        )


class RecordingUpdater:
    def __init__(self, jobs) -> None:
        self.jobs = jobs
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        job = self.jobs.get_by_idempotency(
            actor_user_id=kwargs["actor_user_id"],
            idempotency_key=kwargs["idempotency_key"],
        )
        if len(self.calls) == 1:
            raise UnresolvedUpdateError(job.id, ())
        self.jobs.transition(
            job.id, expected=JobState.RUNNING, target=JobState.SUCCEEDED,
            now=NOW, progress={"installed_release": kwargs["target_version"]},
        )
        return SimpleNamespace(job_id=job.id, applied=True)

def _stack(tmp_path):
    storage = StorageLayout(tmp_path / "data")
    identities = IdentityRepository(IdentityDatabase(storage.app_db))
    identities.initialize()
    owner = identities.create_user(username="owner", now=NOW)
    other = identities.create_user(username="other", now=NOW)
    owner_paths = storage.prepare_user(owner.id)
    other_paths = storage.prepare_user(other.id)
    Collection(str(other_paths.collection), server=False).close()
    col = Collection(str(owner_paths.collection), server=False)
    try:
        deck_id = int(col.decks.id("Greek"))
        note = col.new_note(col.models.by_name("Basic"))
        note["Front"], note["Back"] = "γειά", "hello"
        col.add_note(note, deck_id)
    finally:
        col.close()
    sharing = SharingRepository(identities.database)
    share = SharingService(sharing, clock=lambda: NOW).create_share(
        actor_user_id=owner.id, name="Greek",
    )
    registry = RuntimeRegistry(lambda _key: FakeRuntime(), wait_seconds=1)
    provisioner = RecordingProvisioner(storage)
    runner = SharingJobRunner(
        jobs=JobRepository(identities.database), sharing=sharing, storage=storage,
        registry=registry, provisioner=provisioner, clock=lambda: NOW,
    )
    return storage, identities, owner, other, share, deck_id, registry, provisioner, runner


async def _finished(runner: SharingJobRunner, job_id: str):
    for _ in range(200):
        job = await asyncio.to_thread(runner.jobs.get, job_id)
        if job is not None and job.state in {JobState.SUCCEEDED, JobState.FAILED}:
            return job
        await asyncio.sleep(0.005)
    raise AssertionError("job did not finish")


@pytest.mark.asyncio
async def test_provision_job_is_durable_idempotent_and_waits_for_active_runtime(tmp_path):
    storage, _, owner, _, share, deck_id, registry, provisioner, runner = _stack(tmp_path)
    await runner.start()
    lease = await registry.acquire(ResourceKey.user(owner.id))
    first = await runner.enqueue_workspace_provision(
        actor_user_id=owner.id, share_id=share.id, deck_id=deck_id,
        idempotency_key="provision-1",
    )
    duplicate = await runner.enqueue_workspace_provision(
        actor_user_id=owner.id, share_id=share.id, deck_id=deck_id,
        idempotency_key="provision-1",
    )
    assert duplicate.id == first.id
    await asyncio.sleep(0.02)
    assert provisioner.calls == []
    assert not storage.share_paths(share.id).collection.exists()

    waiter = asyncio.create_task(registry.acquire(ResourceKey.user(owner.id)))
    await lease.release()
    done = await _finished(runner, first.id)
    assert done.state is JobState.SUCCEEDED
    assert provisioner.calls == [(share.id, deck_id)]
    replacement = await waiter
    await replacement.release()
    await runner.stop()
    await registry.drain()


@pytest.mark.asyncio
async def test_restart_reconciles_queued_and_completed_running_jobs(tmp_path):
    storage, _, owner, _, share, deck_id, registry, provisioner, runner = _stack(tmp_path)
    payload = json.dumps(
        {"share_id": share.id, "deck_id": deck_id}, sort_keys=True,
        separators=(",", ":"),
    ).encode()
    queued, _ = runner.jobs.create_or_get(
        actor_user_id=owner.id, resource_type="share", resource_id=share.id,
        capability=WORKSPACE_PROVISION, idempotency_key="restart-queued",
        request_hash=request_digest(payload), progress={"deck_id": deck_id}, now=NOW,
    )
    await runner.start()
    assert (await _finished(runner, queued.id)).state is JobState.SUCCEEDED
    await runner.stop()
    await registry.drain()

    # A crash after filesystem completion but before the terminal DB transition is
    # reconciled without executing the destructive operation again.
    registry2 = RuntimeRegistry(lambda _key: FakeRuntime(), wait_seconds=1)
    runner2 = SharingJobRunner(
        jobs=runner.jobs, sharing=runner.sharing, storage=storage, registry=registry2,
        provisioner=provisioner, clock=lambda: NOW,
    )
    second_share = SharingService(runner.sharing, clock=lambda: NOW).create_share(
        actor_user_id=owner.id, name="Greek 2",
    )
    running, _ = runner.jobs.create_or_get(
        actor_user_id=owner.id, resource_type="share", resource_id=second_share.id,
        capability=WORKSPACE_PROVISION, idempotency_key="restart-running",
        request_hash=request_digest(b"running"), progress={"deck_id": deck_id}, now=NOW,
    )
    runner.jobs.transition(
        running.id, expected=JobState.QUEUED, target=JobState.RUNNING, now=NOW,
    )
    paths = storage.prepare_share(second_share.id)
    paths.collection.write_bytes(b"workspace")
    before = list(provisioner.calls)
    await runner2.start()
    assert runner2.jobs.get(running.id).state is JobState.SUCCEEDED
    assert provisioner.calls == before
    await runner2.stop()
    await registry2.drain()


@pytest.mark.asyncio
async def test_revocation_and_final_reauthorization_fail_closed_and_rollback(tmp_path):
    storage, identities, owner, _, share, deck_id, registry, provisioner, runner = _stack(tmp_path)
    await runner.start()
    lease = await registry.acquire(ResourceKey.user(owner.id))
    job = await runner.enqueue_workspace_provision(
        actor_user_id=owner.id, share_id=share.id, deck_id=deck_id,
        idempotency_key="revoked",
    )
    with identities.database.transaction() as conn:
        conn.execute(
            "UPDATE share_members SET state='removed' WHERE share_id=? AND user_id=?",
            (share.id, owner.id),
        )
    await lease.release()
    failed = await _finished(runner, job.id)
    assert failed.state is JobState.FAILED
    assert failed.error_code == "authorization_revoked"
    assert not storage.share_paths(share.id).root.exists()
    await runner.stop()
    await registry.drain()


@pytest.mark.asyncio
async def test_final_reauthorization_rolls_back_new_workspace(tmp_path):
    storage, _, owner, _, share, deck_id, registry, _, runner = _stack(tmp_path)
    original = runner.sharing.require_owner
    calls = 0

    def revoked_after_write(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 4:  # enqueue, worker preflight, provisioner, final commit gate
            from ankiweb.identity.repository import AuthorizationError
            raise AuthorizationError("revoked")
        return original(**kwargs)

    runner.sharing.require_owner = revoked_after_write
    await runner.start()
    job = await runner.enqueue_workspace_provision(
        actor_user_id=owner.id, share_id=share.id, deck_id=deck_id,
        idempotency_key="late-revocation",
    )
    failed = await _finished(runner, job.id)
    assert failed.state is JobState.FAILED
    assert failed.error_code == "authorization_revoked"
    assert not storage.share_paths(share.id).root.exists()
    await runner.stop()
    await registry.drain()


@pytest.mark.asyncio
async def test_failed_reprovision_never_deletes_existing_workspace(tmp_path):
    storage, _, owner, _, share, deck_id, registry, _, runner = _stack(tmp_path)
    await runner.start()
    first = await runner.enqueue_workspace_provision(
        actor_user_id=owner.id, share_id=share.id, deck_id=deck_id,
        idempotency_key="first-workspace",
    )
    assert (await _finished(runner, first.id)).state is JobState.SUCCEEDED
    original = storage.share_paths(share.id).collection.read_bytes()
    second = await runner.enqueue_workspace_provision(
        actor_user_id=owner.id, share_id=share.id, deck_id=deck_id,
        idempotency_key="second-workspace",
    )
    assert (await _finished(runner, second.id)).state is JobState.FAILED
    assert storage.share_paths(share.id).collection.read_bytes() == original
    await runner.stop()
    await registry.drain()


@pytest.mark.asyncio
async def test_shutdown_drains_admitted_job_and_rejects_new_work(tmp_path):
    _, _, owner, _, share, deck_id, registry, _, runner = _stack(tmp_path)
    await runner.start()
    lease = await registry.acquire(ResourceKey.user(owner.id))
    job = await runner.enqueue_workspace_provision(
        actor_user_id=owner.id, share_id=share.id, deck_id=deck_id,
        idempotency_key="shutdown-drain",
    )
    stopping = asyncio.create_task(runner.stop())
    await asyncio.sleep(0.01)
    assert not stopping.done()
    await lease.release()
    await stopping
    assert runner.jobs.get(job.id).state is JobState.SUCCEEDED
    with pytest.raises(RuntimeError, match="unavailable"):
        await runner.enqueue_workspace_provision(
            actor_user_id=owner.id, share_id=share.id, deck_id=deck_id,
            idempotency_key="after-stop",
        )
    await registry.drain()


@pytest.mark.asyncio
async def test_publish_job_waits_for_share_runtime_and_records_release_result(tmp_path):
    storage, _, owner, _, share, _, registry, _, base = _stack(tmp_path)
    storage.prepare_share(share.id).collection.write_bytes(b"workspace")
    publisher = RecordingPublisher(storage)
    runner = SharingJobRunner(
        jobs=base.jobs, sharing=base.sharing, storage=storage, registry=registry,
        provisioner=base.provisioner, publisher=publisher, clock=lambda: NOW,
    )
    await runner.start()
    with pytest.raises(ValueError, match="idempotency"):
        await runner.enqueue_release_publish(
            actor_user_id=owner.id, share_id=share.id, idempotency_key="",
        )
    lease = await registry.acquire(ResourceKey.share(share.id))
    job = await runner.enqueue_release_publish(
        actor_user_id=owner.id, share_id=share.id, idempotency_key="publish-1",
    )
    await asyncio.sleep(0.01)
    assert publisher.calls == []
    await lease.release()
    done = await _finished(runner, job.id)
    assert done.state is JobState.SUCCEEDED
    assert done.capability == PUBLISH_RELEASE
    assert done.progress == {
        "version": 1, "manifest_sha256": "a" * 64, "bundle_sha256": "b" * 64,
    }
    assert publisher.calls == [(share.id, 1)]
    replay = await runner.enqueue_release_publish(
        actor_user_id=owner.id, share_id=share.id, idempotency_key="publish-1",
    )
    assert replay.id == done.id
    await runner.stop()
    await registry.drain()


@pytest.mark.asyncio
async def test_publish_restart_reconciles_completed_release_without_republishing(tmp_path):
    storage, _, owner, _, share, _, registry, _, base = _stack(tmp_path)
    storage.prepare_share(share.id).collection.write_bytes(b"workspace")
    publisher = RecordingPublisher(storage)
    release = publisher.publish(actor_user_id=owner.id, share_id=share.id, version=1)
    job, _ = base.jobs.create_or_get(
        actor_user_id=owner.id, resource_type="share", resource_id=share.id,
        capability=PUBLISH_RELEASE, idempotency_key="publish-crash",
        request_hash=request_digest(b"publish-crash"), progress={"version": 1}, now=NOW,
    )
    base.jobs.transition(
        job.id, expected=JobState.QUEUED, target=JobState.RUNNING, now=NOW,
    )
    publisher.calls.clear()
    runner = SharingJobRunner(
        jobs=base.jobs, sharing=base.sharing, storage=storage, registry=registry,
        provisioner=base.provisioner, publisher=publisher, clock=lambda: NOW,
    )
    await runner.start()
    recovered = runner.jobs.get(job.id)
    assert recovered.state is JobState.SUCCEEDED
    assert recovered.progress["manifest_sha256"] == release.manifest_sha256
    assert publisher.calls == []
    await runner.stop()
    await registry.drain()


@pytest.mark.asyncio
async def test_follow_install_job_uses_recipient_maintenance_and_recovers_restart(tmp_path):
    storage, _, owner, recipient, share, deck_id, registry, _, base = _stack(tmp_path)
    service = SharingService(base.sharing, clock=lambda: NOW)
    invite = service.create_invite(
        actor_user_id=owner.id, share_id=share.id, role=ShareRole.VIEWER,
        intended_user_id=recipient.id,
    )
    service.accept_invite(actor_user_id=recipient.id, token=invite.token)
    WorkspaceProvisioner(storage).create_from_owner_deck(
        actor_user_id=owner.id, owner_collection=storage.user_paths(owner.id).collection,
        share_id=share.id, deck_id=deck_id, authorize=base.sharing.require_owner,
    )
    ReleasePublisher(storage, base.sharing, clock=lambda: NOW).publish(
        actor_user_id=owner.id, share_id=share.id,
    )
    runner = SharingJobRunner(
        jobs=base.jobs, sharing=base.sharing, storage=storage, registry=registry,
        clock=lambda: NOW,
    )
    await runner.start()
    lease = await registry.acquire(ResourceKey.user(recipient.id))
    job = await runner.enqueue_release_install(
        actor_user_id=recipient.id, share_id=share.id, version=1, mode="follow",
        idempotency_key="follow-install-1",
    )
    await asyncio.sleep(0.01)
    assert runner.jobs.get(job.id).state is JobState.RUNNING
    await lease.release()
    done = await _finished(runner, job.id)
    assert done.state is JobState.SUCCEEDED
    assert done.progress["mode"] == "follow"
    assert done.progress["subscription_id"]
    replay = await runner.enqueue_release_install(
        actor_user_id=recipient.id, share_id=share.id, version=1, mode="follow",
        idempotency_key="follow-install-1",
    )
    assert replay.id == done.id
    updater = RecordingUpdater(runner.jobs)
    runner.updater = updater
    update_lease = await registry.acquire(ResourceKey.user(recipient.id))
    update = await runner.enqueue_subscription_update(
        actor_user_id=recipient.id,
        subscription_id=done.progress["subscription_id"], target_version=2,
        idempotency_key="update-1",
    )
    await asyncio.sleep(0.01)
    assert updater.calls == []
    await update_lease.release()
    for _ in range(100):
        if updater.calls:
            break
        await asyncio.sleep(0.005)
    assert runner.jobs.get(update.id).state is JobState.RUNNING
    await runner.continue_subscription_update(
        actor_user_id=recipient.id, job_id=update.id, manual_values={},
    )
    assert (await _finished(runner, update.id)).state is JobState.SUCCEEDED
    assert len(updater.calls) == 2
    col = Collection(str(storage.user_paths(recipient.id).collection), server=False)
    try:
        assert col.find_notes('"γειά"')
    finally:
        col.close()
    await runner.stop()
    await registry.drain()

    # Simulate process loss after the filesystem/database install committed but before
    # the terminal job state became durable.
    with base.jobs.database.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET state='running',finished_at=NULL WHERE id=?", (job.id,),
        )
    registry2 = RuntimeRegistry(lambda _key: FakeRuntime(), wait_seconds=1)
    resumed = SharingJobRunner(
        jobs=base.jobs, sharing=base.sharing, storage=storage, registry=registry2,
        clock=lambda: NOW,
    )
    await resumed.start()
    assert resumed.jobs.get(job.id).state is JobState.SUCCEEDED
    await resumed.stop()
    await registry2.drain()
