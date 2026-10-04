from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping

from .database import IdentityDatabase
from .repository import ConflictError, NotFoundError


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    actor_user_id: str
    resource_type: str
    resource_id: str
    capability: str
    state: JobState
    idempotency_key: str
    generation: int
    progress: Mapping[str, Any]
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def request_digest(payload: bytes) -> bytes:
    return hashlib.sha256(payload).digest()


def _datetime(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value, timezone.utc) if value is not None else None


def _epoch(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return int(value.timestamp())


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        actor_user_id=row["actor_user_id"],
        resource_type=row["resource_type"],
        resource_id=row["resource_id"],
        capability=row["capability"],
        state=JobState(row["state"]),
        idempotency_key=row["idempotency_key"],
        generation=row["generation"],
        progress=json.loads(row["progress_json"]),
        error_code=row["error_code"],
        created_at=_datetime(row["created_at"]),
        started_at=_datetime(row["started_at"]),
        finished_at=_datetime(row["finished_at"]),
    )


class JobRepository:
    """Durable idempotency and crash-recovery journal for heavy tenant operations."""

    def __init__(self, database: IdentityDatabase) -> None:
        self.database = database

    def create_or_get(
        self,
        *,
        actor_user_id: str,
        resource_type: str,
        resource_id: str,
        capability: str,
        idempotency_key: str,
        request_hash: bytes,
        now: datetime,
    ) -> tuple[Job, bool]:
        if not idempotency_key or len(idempotency_key) > 256:
            raise ValueError("idempotency key must contain 1 to 256 characters")
        if len(request_hash) != 32:
            raise ValueError("request hash must be SHA-256")
        with self.database.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM jobs WHERE actor_user_id=? AND idempotency_key=?",
                (actor_user_id, idempotency_key),
            ).fetchone()
            if existing:
                if bytes(existing["request_hash"]) != request_hash:
                    raise ConflictError("idempotency key was used for a different request")
                return _job(existing), False
            job_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO jobs(
                   id, actor_user_id, resource_type, resource_id, capability, state,
                   idempotency_key, request_hash, created_at)
                   VALUES(?,?,?,?,?,'queued',?,?,?)""",
                (
                    job_id, actor_user_id, resource_type, resource_id, capability,
                    idempotency_key, request_hash, _epoch(now),
                ),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return _job(row), True

    def get(self, job_id: str) -> Job | None:
        with self.database.read() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return _job(row) if row else None

    def transition(
        self,
        job_id: str,
        *,
        expected: JobState,
        target: JobState,
        now: datetime,
        progress: Mapping[str, Any] | None = None,
        error_code: str | None = None,
    ) -> Job:
        allowed = {
            JobState.QUEUED: {JobState.RUNNING, JobState.CANCELLED},
            JobState.RUNNING: {
                JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED,
            },
        }
        if target not in allowed.get(expected, set()):
            raise ValueError(f"invalid job transition: {expected.value} -> {target.value}")
        payload = json.dumps(progress or {}, separators=(",", ":"), sort_keys=True)
        if len(payload.encode()) > 64 * 1024:
            raise ValueError("job progress exceeds 64 KiB")
        started_at = _epoch(now) if target is JobState.RUNNING else None
        finished_at = _epoch(now) if target in {
            JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED,
        } else None
        with self.database.transaction() as conn:
            changed = conn.execute(
                """UPDATE jobs SET state=?, generation=generation+1, progress_json=?,
                   error_code=?, started_at=COALESCE(started_at, ?), finished_at=?
                   WHERE id=? AND state=?""",
                (
                    target.value, payload, error_code, started_at, finished_at,
                    job_id, expected.value,
                ),
            ).rowcount
            if not changed:
                if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
                    raise NotFoundError("job not found")
                raise ConflictError("job state changed concurrently")
            return _job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def append_journal(
        self,
        job_id: str,
        *,
        phase: str,
        payload: Mapping[str, Any] | None,
        now: datetime,
    ) -> int:
        encoded = json.dumps(payload or {}, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode()) > 64 * 1024:
            raise ValueError("job journal payload exceeds 64 KiB")
        with self.database.transaction() as conn:
            if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
                raise NotFoundError("job not found")
            sequence = conn.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 FROM job_journal WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO job_journal(job_id,sequence,phase,payload_json,committed_at)
                   VALUES(?,?,?,?,?)""",
                (job_id, sequence, phase, encoded, _epoch(now)),
            )
            return int(sequence)
