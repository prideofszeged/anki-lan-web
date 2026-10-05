from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Callable

from ankiweb.adapters.anki.sharing import (
    ReleaseInstaller, ReleasePublisher, WorkspaceProvisioner,
)
from ankiweb.adapters.anki.collaboration import (
    SubscriptionUpdater, UnresolvedUpdateError, WorkspaceCollaboration,
)
from ankiweb.identity.jobs import Job, JobRepository, JobState, request_digest
from ankiweb.identity.repository import AuthorizationError, ConflictError
from ankiweb.tenancy import (
    ResourceKey, RuntimeCapacityError, RuntimeRegistry, StorageLayout,
)

from .repository import SharingRepository
from .backup import ShareBackupManager


WORKSPACE_PROVISION = "share.workspace.provision"
PUBLISH_RELEASE = "share.release.publish"
INSTALL_RELEASE = "share.release.install"
UPDATE_SUBSCRIPTION = "share.update"
BACKUP_SHARE = "share.backup.create"
RESTORE_SHARE = "share.backup.restore"


class SharingJobRunner:
    """Lifecycle-owned, durable executor for explicit sharing operations.

    A job contains identifiers and scalar options only. Collection content never enters
    app.db. Every filesystem mutation runs with ordered runtime-maintenance reservations.
    """

    def __init__(
        self,
        *,
        jobs: JobRepository,
        sharing: SharingRepository,
        storage: StorageLayout,
        registry: RuntimeRegistry,
        provisioner: WorkspaceProvisioner | None = None,
        publisher: ReleasePublisher | None = None,
        installer: ReleaseInstaller | None = None,
        updater: SubscriptionUpdater | None = None,
        workspace: WorkspaceCollaboration | None = None,
        backup_manager: ShareBackupManager | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.jobs = jobs
        self.sharing = sharing
        self.storage = storage
        self.registry = registry
        self.provisioner = provisioner or WorkspaceProvisioner(storage)
        self.publisher = publisher or ReleasePublisher(storage, sharing, clock=clock)
        self.installer = installer or ReleaseInstaller(storage, sharing, clock=clock)
        self.updater = updater or SubscriptionUpdater(storage, sharing, clock=clock)
        self.workspace = workspace or WorkspaceCollaboration(storage, sharing, clock=clock)
        self.backup_manager = backup_manager or ShareBackupManager(
            storage, sharing, clock=clock,
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._scheduled: set[str] = set()
        self._schedule_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._continuations: dict[str, dict[str, str]] = {}
        self._worker: asyncio.Task[None] | None = None
        self._accepting = False

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._worker is not None:
                return
            self._accepting = True
            self._worker = asyncio.create_task(self._run(), name="sharing-job-runner")
        for capability in (
            WORKSPACE_PROVISION, PUBLISH_RELEASE, INSTALL_RELEASE, UPDATE_SUBSCRIPTION,
            BACKUP_SHARE, RESTORE_SHARE,
        ):
            for job in await asyncio.to_thread(
                self.jobs.list_recoverable, capability=capability,
            ):
                if job.state is JobState.RUNNING:
                    job = await self._reconcile_interrupted(job)
                if job.state is JobState.QUEUED:
                    await self._schedule(job.id)

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._accepting = False
            worker, self._worker = self._worker, None
            if worker is None:
                return
            await self._queue.put(None)
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            await asyncio.shield(worker)
            raise

    async def enqueue_workspace_provision(
        self, *, actor_user_id: str, share_id: str, deck_id: int,
        idempotency_key: str,
    ) -> Job:
        async with self._lifecycle_lock:
            if not self._accepting:
                raise RuntimeError("sharing job runner is unavailable")
            self._validate_idempotency_key(idempotency_key)
            if deck_id <= 0:
                raise ValueError("deck_id must be positive")
            await asyncio.to_thread(
                self.sharing.require_owner,
                actor_user_id=actor_user_id, share_id=share_id,
            )
            progress = {"deck_id": deck_id}
            request = json.dumps(
                {"share_id": share_id, "deck_id": deck_id},
                sort_keys=True, separators=(",", ":"),
            ).encode()
            job, created = await asyncio.to_thread(
                self.jobs.create_or_get,
                actor_user_id=actor_user_id,
                resource_type="share",
                resource_id=share_id,
                capability=WORKSPACE_PROVISION,
                idempotency_key=idempotency_key,
                request_hash=request_digest(request),
                progress=progress,
                now=self._clock(),
            )
            if created or job.state is JobState.QUEUED:
                await self._schedule(job.id)
            return job

    async def enqueue_release_publish(
        self, *, actor_user_id: str, share_id: str, idempotency_key: str,
    ) -> Job:
        async with self._lifecycle_lock:
            if not self._accepting:
                raise RuntimeError("sharing job runner is unavailable")
            self._validate_idempotency_key(idempotency_key)
            existing = await asyncio.to_thread(
                self.jobs.get_by_idempotency,
                actor_user_id=actor_user_id, idempotency_key=idempotency_key,
            )
            if existing is not None:
                if existing.capability != PUBLISH_RELEASE or existing.resource_id != share_id:
                    raise ConflictError("idempotency key was used for a different request")
                return existing
            version = await asyncio.to_thread(
                self.sharing.next_release_version,
                actor_user_id=actor_user_id, share_id=share_id,
            )
            progress = {"version": version}
            request = json.dumps(
                {"share_id": share_id, "version": version},
                sort_keys=True, separators=(",", ":"),
            ).encode()
            job, created = await asyncio.to_thread(
                self.jobs.create_or_get,
                actor_user_id=actor_user_id, resource_type="share",
                resource_id=share_id, capability=PUBLISH_RELEASE,
                idempotency_key=idempotency_key, request_hash=request_digest(request),
                progress=progress, now=self._clock(),
            )
            if created or job.state is JobState.QUEUED:
                await self._schedule(job.id)
            return job

    async def enqueue_release_install(
        self, *, actor_user_id: str, share_id: str, version: int, mode: str,
        idempotency_key: str,
    ) -> Job:
        async with self._lifecycle_lock:
            if not self._accepting:
                raise RuntimeError("sharing job runner is unavailable")
            self._validate_idempotency_key(idempotency_key)
            if mode not in {"copy", "follow"} or version < 1:
                raise ValueError("valid version and copy/follow mode required")
            await asyncio.to_thread(
                self.sharing.require_member,
                actor_user_id=actor_user_id, share_id=share_id,
            )
            progress = {"version": version, "mode": mode}
            request = json.dumps(
                {"share_id": share_id, **progress},
                sort_keys=True, separators=(",", ":"),
            ).encode()
            job, created = await asyncio.to_thread(
                self.jobs.create_or_get,
                actor_user_id=actor_user_id, resource_type="share",
                resource_id=share_id, capability=INSTALL_RELEASE,
                idempotency_key=idempotency_key, request_hash=request_digest(request),
                progress=progress, now=self._clock(),
            )
            if created or job.state is JobState.QUEUED:
                await self._schedule(job.id)
            return job

    async def enqueue_subscription_update(
        self, *, actor_user_id: str, subscription_id: str, target_version: int,
        idempotency_key: str, mirror_preview_digest: str | None = None,
        approve_templates: bool = False,
    ) -> Job:
        async with self._lifecycle_lock:
            if not self._accepting:
                raise RuntimeError("sharing job runner is unavailable")
            self._validate_idempotency_key(idempotency_key)
            subscription = await asyncio.to_thread(
                self.sharing.get_subscription,
                actor_user_id=actor_user_id, subscription_id=subscription_id,
            )
            if target_version <= subscription.installed_release:
                raise ValueError("target release must be newer than installed release")
            progress = {
                "target_version": target_version,
                "mirror_preview_digest": mirror_preview_digest,
                "approve_templates": approve_templates,
            }
            request = json.dumps({
                "subscription_id": subscription_id, **progress,
            }, sort_keys=True, separators=(",", ":")).encode()
            job, created = await asyncio.to_thread(
                self.jobs.create_or_get,
                actor_user_id=actor_user_id, resource_type="subscription",
                resource_id=subscription_id, capability=UPDATE_SUBSCRIPTION,
                idempotency_key=idempotency_key, request_hash=request_digest(request),
                progress=progress, now=self._clock(),
            )
            if created or job.state is JobState.QUEUED:
                await self._schedule(job.id)
            return job

    async def enqueue_share_backup(
        self, *, actor_user_id: str, share_id: str, idempotency_key: str,
    ) -> Job:
        async with self._lifecycle_lock:
            if not self._accepting:
                raise RuntimeError("sharing job runner is unavailable")
            self._validate_idempotency_key(idempotency_key)
            await asyncio.to_thread(
                self.sharing.require_owner,
                actor_user_id=actor_user_id, share_id=share_id,
            )
            request = json.dumps(
                {"share_id": share_id}, sort_keys=True, separators=(",", ":"),
            ).encode()
            job, created = await asyncio.to_thread(
                self.jobs.create_or_get,
                actor_user_id=actor_user_id, resource_type="share",
                resource_id=share_id, capability=BACKUP_SHARE,
                idempotency_key=idempotency_key, request_hash=request_digest(request),
                progress={}, now=self._clock(),
            )
            if created or job.state is JobState.QUEUED:
                await self._schedule(job.id)
            return job

    async def enqueue_share_restore(
        self, *, actor_user_id: str, share_id: str, backup_name: str,
        idempotency_key: str,
    ) -> Job:
        async with self._lifecycle_lock:
            if not self._accepting:
                raise RuntimeError("sharing job runner is unavailable")
            self._validate_idempotency_key(idempotency_key)
            await asyncio.to_thread(
                self.backup_manager.resolve_archive,
                actor_user_id=actor_user_id, share_id=share_id, name=backup_name,
            )
            progress = {"backup_name": backup_name}
            request = json.dumps(
                {"share_id": share_id, **progress},
                sort_keys=True, separators=(",", ":"),
            ).encode()
            job, created = await asyncio.to_thread(
                self.jobs.create_or_get,
                actor_user_id=actor_user_id, resource_type="share",
                resource_id=share_id, capability=RESTORE_SHARE,
                idempotency_key=idempotency_key, request_hash=request_digest(request),
                progress=progress, now=self._clock(),
            )
            if created or job.state is JobState.QUEUED:
                await self._schedule(job.id)
            return job

    async def continue_subscription_update(
        self, *, actor_user_id: str, job_id: str,
        manual_values: dict[str, str],
    ) -> Job:
        async with self._lifecycle_lock:
            job = await asyncio.to_thread(
                self.jobs.get_for_actor, job_id, actor_user_id=actor_user_id,
            )
            if job is None or job.capability != UPDATE_SUBSCRIPTION:
                raise ValueError("update job not found")
            if job.state is not JobState.RUNNING:
                raise ConflictError("update job is not awaiting continuation")
            await asyncio.to_thread(
                self.sharing.get_subscription,
                actor_user_id=actor_user_id, subscription_id=job.resource_id,
            )
            conflicts = await asyncio.to_thread(
                self.sharing.list_job_conflicts,
                actor_user_id=actor_user_id, job_id=job.id,
            )
            if any(item.resolution is None for item in conflicts):
                raise ConflictError("all update conflicts must be resolved")
            required_manual = {
                item.id for item in conflicts if item.resolution == "manual"
            }
            if set(manual_values) != required_manual:
                raise ValueError("all manual conflict values must be supplied atomically")
            if any(not isinstance(value, str) or len(value) > 1_000_000
                   for value in manual_values.values()):
                raise ValueError("manual conflict values are invalid")
            self._continuations[job.id] = dict(manual_values)
            await self._schedule(job.id)
            return job

    async def edit_workspace_note(
        self, *, actor_user_id: str, share_id: str, guid: str,
        fields: dict[str, str], expected_revision: int,
    ):
        async with self.registry.maintenance(ResourceKey.share(share_id)):
            return await asyncio.to_thread(
                self.workspace.edit_note,
                actor_user_id=actor_user_id, share_id=share_id, guid=guid,
                fields=fields, expected_revision=expected_revision,
            )

    async def _schedule(self, job_id: str) -> None:
        async with self._schedule_lock:
            if job_id in self._scheduled:
                return
            self._scheduled.add(job_id)
            await self._queue.put(job_id)

    async def _run(self) -> None:
        while True:
            job_id = await self._queue.get()
            if job_id is None:
                self._queue.task_done()
                return
            try:
                await self._execute(job_id)
            finally:
                async with self._schedule_lock:
                    self._scheduled.discard(job_id)
                self._queue.task_done()

    async def _execute(self, job_id: str) -> None:
        job = await asyncio.to_thread(self.jobs.get, job_id)
        continuation = self._continuations.pop(job_id, None)
        if job is None:
            return
        if job.state is JobState.QUEUED:
            try:
                job = await asyncio.to_thread(
                    self.jobs.transition, job.id,
                    expected=JobState.QUEUED, target=JobState.RUNNING, now=self._clock(),
                )
            except ConflictError:
                return
        elif not (
            job.state is JobState.RUNNING
            and job.capability == UPDATE_SUBSCRIPTION
            and continuation is not None
        ):
            return
        try:
            if job.capability == WORKSPACE_PROVISION:
                await self._provision(job)
            elif job.capability == PUBLISH_RELEASE:
                await self._publish(job)
            elif job.capability == INSTALL_RELEASE:
                await self._install(job)
            elif job.capability == UPDATE_SUBSCRIPTION:
                await self._update(job, continuation or {})
            elif job.capability == BACKUP_SHARE:
                await self._backup_share(job)
            elif job.capability == RESTORE_SHARE:
                await self._restore_share(job)
            else:
                raise ValueError("unsupported sharing job capability")
        except asyncio.CancelledError:
            # The blocking operation has reached its rollback boundary; preserve retryability.
            try:
                await asyncio.to_thread(self.jobs.requeue_running, job.id)
            except ConflictError:
                pass
            raise
        except UnresolvedUpdateError:
            return
        except Exception as exc:
            try:
                await asyncio.to_thread(
                    self.jobs.transition, job.id,
                    expected=JobState.RUNNING, target=JobState.FAILED,
                    now=self._clock(), error_code=self._safe_error(exc),
                )
            except ConflictError:
                pass

    async def _provision(self, job: Job) -> None:
        user_key = ResourceKey.user(job.actor_user_id)
        share_key = ResourceKey.share(job.resource_id)
        async with self.registry.maintenance(user_key):
            async with self.registry.maintenance(share_key):
                await asyncio.to_thread(
                    self.sharing.require_owner,
                    actor_user_id=job.actor_user_id, share_id=job.resource_id,
                )
                paths = self.storage.share_paths(job.resource_id)
                root_existed = paths.root.exists() or paths.root.is_symlink()
                if root_existed:
                    raise FileExistsError("share workspace already exists")
                try:
                    await self._uncancellable_thread(
                        self.provisioner.create_from_owner_deck,
                        actor_user_id=job.actor_user_id,
                        owner_collection=self.storage.user_paths(job.actor_user_id).collection,
                        share_id=job.resource_id,
                        deck_id=int(job.progress["deck_id"]),
                        authorize=self.sharing.require_owner,
                    )
                    # The job commit takes the app-db writer lock before its final
                    # authorization guard, so revocation cannot race the commit.
                    await asyncio.to_thread(
                        self.jobs.transition, job.id,
                        expected=JobState.RUNNING, target=JobState.SUCCEEDED,
                        now=self._clock(),
                        progress={**job.progress, "workspace": "ready"},
                        guard=lambda: self.sharing.require_owner(
                            actor_user_id=job.actor_user_id,
                            share_id=job.resource_id,
                        ),
                    )
                except BaseException:
                    if not root_existed:
                        await asyncio.to_thread(
                            self.storage.discard_share_workspace, job.resource_id,
                        )
                    raise

    async def _publish(self, job: Job) -> None:
        async with self.registry.maintenance(ResourceKey.share(job.resource_id)):
            await asyncio.to_thread(
                self.sharing.require_owner,
                actor_user_id=job.actor_user_id, share_id=job.resource_id,
            )
            release = await self._uncancellable_thread(
                self.publisher.publish,
                actor_user_id=job.actor_user_id, share_id=job.resource_id,
                version=int(job.progress["version"]),
            )
            await asyncio.to_thread(
                self.publisher.validate_release,
                actor_user_id=job.actor_user_id, share_id=job.resource_id,
                version=release.version,
            )
            await asyncio.to_thread(
                self.jobs.transition, job.id,
                expected=JobState.RUNNING, target=JobState.SUCCEEDED,
                now=self._clock(), progress={
                    "version": release.version,
                    "manifest_sha256": release.manifest_sha256,
                    "bundle_sha256": release.bundle_sha256,
                },
            )

    async def _install(self, job: Job) -> None:
        async with self.registry.maintenance(ResourceKey.user(job.actor_user_id)):
            await asyncio.to_thread(
                self.sharing.require_member,
                actor_user_id=job.actor_user_id, share_id=job.resource_id,
            )
            result = await self._uncancellable_thread(
                self.installer.install,
                actor_user_id=job.actor_user_id, share_id=job.resource_id,
                version=int(job.progress["version"]), mode=str(job.progress["mode"]),
                operation_id=job.id, authorize_before_commit=self.sharing.require_member,
            )
            await asyncio.to_thread(
                self.jobs.transition, job.id,
                expected=JobState.RUNNING, target=JobState.SUCCEEDED,
                now=self._clock(), progress={
                    "version": result.installed_release, "mode": result.mode,
                    "subscription_id": result.subscription_id,
                },
            )

    async def _update(self, job: Job, manual_values: dict[str, str]) -> None:
        async with self.registry.maintenance(ResourceKey.user(job.actor_user_id)):
            await self._uncancellable_thread(
                self.updater.run,
                actor_user_id=job.actor_user_id, subscription_id=job.resource_id,
                target_version=int(job.progress["target_version"]),
                idempotency_key=job.idempotency_key,
                mirror_preview_digest=job.progress.get("mirror_preview_digest"),
                approve_templates=bool(job.progress.get("approve_templates")),
                manual_values=manual_values,
            )

    async def _backup_share(self, job: Job) -> None:
        async with self.registry.maintenance(ResourceKey.share(job.resource_id)):
            result = await self._uncancellable_thread(
                self.backup_manager.create,
                actor_user_id=job.actor_user_id, share_id=job.resource_id,
                operation_id=job.id,
            )
            await asyncio.to_thread(
                self.jobs.transition, job.id,
                expected=JobState.RUNNING, target=JobState.SUCCEEDED,
                now=self._clock(), progress={
                    "backup_name": result.archive.name, "sha256": result.sha256,
                },
                guard=lambda: self.sharing.require_owner(
                    actor_user_id=job.actor_user_id, share_id=job.resource_id,
                ),
            )

    async def _restore_share(self, job: Job) -> None:
        async with self.registry.maintenance(ResourceKey.share(job.resource_id)):
            archive = await asyncio.to_thread(
                self.backup_manager.resolve_archive,
                actor_user_id=job.actor_user_id, share_id=job.resource_id,
                name=str(job.progress["backup_name"]),
            )
            await self._uncancellable_thread(
                self.backup_manager.restore,
                actor_user_id=job.actor_user_id, share_id=job.resource_id,
                archive=archive,
            )
            await asyncio.to_thread(
                self.jobs.transition, job.id,
                expected=JobState.RUNNING, target=JobState.SUCCEEDED,
                now=self._clock(), progress={"backup_name": archive.name},
                guard=lambda: self.sharing.require_owner(
                    actor_user_id=job.actor_user_id, share_id=job.resource_id,
                ),
            )

    @staticmethod
    async def _uncancellable_thread(function, /, *args, **kwargs):
        """Let an in-flight blocking mutation reach its own rollback boundary."""
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise

    async def _reconcile_interrupted(self, job: Job) -> Job:
        if job.capability == PUBLISH_RELEASE:
            return await self._reconcile_publish(job)
        if job.capability == INSTALL_RELEASE:
            return await self._reconcile_install(job)
        if job.capability == UPDATE_SUBSCRIPTION:
            return job
        if job.capability in {BACKUP_SHARE, RESTORE_SHARE}:
            return await asyncio.to_thread(self.jobs.requeue_running, job.id)
        paths = self.storage.share_paths(job.resource_id)
        if paths.collection.exists():
            try:
                await asyncio.to_thread(
                    self.sharing.require_owner,
                    actor_user_id=job.actor_user_id, share_id=job.resource_id,
                )
                await asyncio.to_thread(
                    self.provisioner._validate_workspace, paths.collection,
                )
            except Exception:
                await asyncio.to_thread(
                    self.storage.discard_share_workspace, job.resource_id,
                )
            else:
                return await asyncio.to_thread(
                    self.jobs.transition, job.id,
                    expected=JobState.RUNNING, target=JobState.SUCCEEDED,
                    now=self._clock(), progress={**job.progress, "workspace": "ready"},
                )
        elif paths.root.exists():
            await asyncio.to_thread(self.storage.discard_share_workspace, job.resource_id)
        return await asyncio.to_thread(self.jobs.requeue_running, job.id)

    async def _reconcile_publish(self, job: Job) -> Job:
        version = int(job.progress["version"])
        try:
            recover = getattr(self.publisher, "recover_interrupted", None)
            if recover is None:
                release = await asyncio.to_thread(
                    self.publisher.validate_release,
                    actor_user_id=job.actor_user_id,
                    share_id=job.resource_id, version=version,
                )
            else:
                release = await asyncio.to_thread(
                    recover, actor_user_id=job.actor_user_id,
                    share_id=job.resource_id, version=version,
                )
        except Exception:
            return await asyncio.to_thread(self.jobs.requeue_running, job.id)
        if release is None:
            return await asyncio.to_thread(self.jobs.requeue_running, job.id)
        return await asyncio.to_thread(
            self.jobs.transition, job.id,
            expected=JobState.RUNNING, target=JobState.SUCCEEDED,
            now=self._clock(), progress={
                "version": release.version,
                "manifest_sha256": release.manifest_sha256,
                "bundle_sha256": release.bundle_sha256,
            },
        )

    async def _reconcile_install(self, job: Job) -> Job:
        try:
            result = await asyncio.to_thread(
                self.installer.recover_interrupted,
                actor_user_id=job.actor_user_id, share_id=job.resource_id,
                version=int(job.progress["version"]), mode=str(job.progress["mode"]),
                operation_id=job.id,
            )
        except Exception:
            result = None
        if result is None:
            return await asyncio.to_thread(self.jobs.requeue_running, job.id)
        return await asyncio.to_thread(
            self.jobs.transition, job.id,
            expected=JobState.RUNNING, target=JobState.SUCCEEDED,
            now=self._clock(), progress={
                "version": result.installed_release, "mode": result.mode,
                "subscription_id": result.subscription_id,
            },
        )

    @staticmethod
    def _safe_error(exc: BaseException) -> str:
        if isinstance(exc, AuthorizationError):
            return "authorization_revoked"
        if isinstance(exc, RuntimeCapacityError):
            return "runtime_capacity"
        if isinstance(exc, TimeoutError):
            return "maintenance_timeout"
        return "operation_failed"

    @staticmethod
    def _validate_idempotency_key(value: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value):
            raise ValueError(
                "idempotency key must be 1 to 256 URL-safe ASCII characters"
            )
