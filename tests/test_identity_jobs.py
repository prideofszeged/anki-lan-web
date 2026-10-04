from datetime import datetime, timezone

import pytest

from ankiweb.identity import (
    ConflictError,
    IdentityDatabase,
    IdentityRepository,
    IdentityService,
    JobRepository,
    JobState,
    request_digest,
)

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def _jobs(tmp_path):
    database = IdentityDatabase(tmp_path / "app.db")
    service = IdentityService(IdentityRepository(database), clock=lambda: NOW)
    service.initialize()
    owner = service.bootstrap_owner(password="safe-password")
    return JobRepository(database), owner


def test_idempotency_reuses_same_request_and_rejects_different_request(tmp_path):
    jobs, owner = _jobs(tmp_path)
    first, created = jobs.create_or_get(
        actor_user_id=owner.id,
        resource_type="user",
        resource_id=owner.id,
        capability="user.backup",
        idempotency_key="backup-1",
        request_hash=request_digest(b'{"kind":"backup"}'),
        now=NOW,
    )
    same, created_again = jobs.create_or_get(
        actor_user_id=owner.id,
        resource_type="user",
        resource_id=owner.id,
        capability="user.backup",
        idempotency_key="backup-1",
        request_hash=request_digest(b'{"kind":"backup"}'),
        now=NOW,
    )
    assert created is True and created_again is False
    assert first == same
    with pytest.raises(ConflictError, match="different request"):
        jobs.create_or_get(
            actor_user_id=owner.id,
            resource_type="user",
            resource_id=owner.id,
            capability="user.restore",
            idempotency_key="backup-1",
            request_hash=request_digest(b'{"kind":"restore"}'),
            now=NOW,
        )
    with pytest.raises(ConflictError, match="different request"):
        jobs.create_or_get(
            actor_user_id=owner.id,
            resource_type="user",
            resource_id=owner.id,
            capability="user.restore",
            idempotency_key="backup-1",
            request_hash=request_digest(b'{"kind":"backup"}'),
            now=NOW,
        )


def test_job_transition_is_optimistic_and_journal_is_ordered(tmp_path):
    jobs, owner = _jobs(tmp_path)
    job, _ = jobs.create_or_get(
        actor_user_id=owner.id,
        resource_type="user",
        resource_id=owner.id,
        capability="user.import",
        idempotency_key="import-1",
        request_hash=request_digest(b"request"),
        now=NOW,
    )
    assert jobs.append_journal(job.id, phase="staged", payload={}, now=NOW) == 0
    assert jobs.append_journal(job.id, phase="fsynced", payload={}, now=NOW) == 1
    running = jobs.transition(
        job.id, expected=JobState.QUEUED, target=JobState.RUNNING, now=NOW,
    )
    assert running.generation == 1
    done = jobs.transition(
        job.id,
        expected=JobState.RUNNING,
        target=JobState.SUCCEEDED,
        now=NOW,
        progress={"result": "ok"},
    )
    assert done.progress == {"result": "ok"}
    with pytest.raises(ConflictError, match="concurrently"):
        jobs.transition(
            job.id, expected=JobState.RUNNING, target=JobState.FAILED, now=NOW,
        )


def test_job_payload_survives_start_and_status_is_actor_scoped(tmp_path):
    jobs, owner = _jobs(tmp_path)
    job, _ = jobs.create_or_get(
        actor_user_id=owner.id, resource_type="share", resource_id=owner.id,
        capability="share.workspace.provision", idempotency_key="workspace-1",
        request_hash=request_digest(b"workspace"), progress={"deck_id": 42}, now=NOW,
    )
    running = jobs.transition(
        job.id, expected=JobState.QUEUED, target=JobState.RUNNING, now=NOW,
    )
    assert running.progress == {"deck_id": 42}
    assert jobs.get_for_actor(job.id, actor_user_id=owner.id) == running
    assert jobs.get_for_actor(job.id, actor_user_id="00000000-0000-0000-0000-000000000001") is None
    assert jobs.list_recoverable(capability="share.workspace.provision") == [running]
    queued = jobs.requeue_running(job.id)
    assert queued.state is JobState.QUEUED
    assert queued.progress == {"deck_id": 42}
