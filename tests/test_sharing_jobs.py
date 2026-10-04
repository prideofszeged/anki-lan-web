from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from anki.collection import Collection

from ankiweb.identity import (
    IdentityDatabase, IdentityRepository, JobRepository, JobState, request_digest,
)
from ankiweb.sharing import SharingRepository, SharingService
from ankiweb.sharing.jobs import SharingJobRunner, WORKSPACE_PROVISION
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


def _stack(tmp_path):
    storage = StorageLayout(tmp_path / "data")
    identities = IdentityRepository(IdentityDatabase(storage.app_db))
    identities.initialize()
    owner = identities.create_user(username="owner", now=NOW)
    other = identities.create_user(username="other", now=NOW)
    owner_paths = storage.prepare_user(owner.id)
    col = Collection(str(owner_paths.collection), server=False)
    try:
        deck_id = int(col.decks.id("Greek"))
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
