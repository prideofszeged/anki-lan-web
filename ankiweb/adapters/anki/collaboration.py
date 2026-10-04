"""Three-way subscription updates and optimistic share-workspace edits."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from anki.collection import Collection

from ankiweb.identity.jobs import JobRepository, JobState, request_digest
from ankiweb.identity.repository import ConflictError
from ankiweb.sharing.models import UpdateConflict
from ankiweb.sharing.repository import SharingRepository
from ankiweb.tenancy import StorageLayout

from .sharing import (
    QuotaExceededError, ReleaseInstaller, ReleasePublisher, _copy_tree,
    _import_package, _integrity_ok, _json_hash, _safe_files, _secure_tree,
)


class DestructiveTemplateChangeError(RuntimeError):
    pass


class MirrorPreviewRequired(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WorkspaceNote:
    guid: str
    fields: dict[str, str]
    tags: tuple[str, ...]
    revision: int


class EditConflictError(ConflictError):
    def __init__(self, latest: WorkspaceNote) -> None:
        super().__init__("workspace entity revision changed")
        self.latest = latest


@dataclass(frozen=True, slots=True)
class VisibleUpdateConflict:
    id: str
    field_name: str
    local_value: str
    upstream_value: str
    record: UpdateConflict


class UnresolvedUpdateError(RuntimeError):
    def __init__(self, job_id: str, conflicts: tuple[VisibleUpdateConflict, ...]) -> None:
        super().__init__("subscription update has unresolved conflicts")
        self.job_id = job_id
        self.conflicts = conflicts


@dataclass(frozen=True, slots=True)
class MirrorPreview:
    subscription_id: str
    target_version: int
    tombstones: tuple[str, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class UpdateResult:
    job_id: str
    applied: bool
    installed_release: int


def _field_source(guid: str, field_name: str) -> str:
    return json.dumps([guid, field_name], ensure_ascii=False, separators=(",", ":"))


def _preview_digest(subscription_id: str, version: int, tombstones: tuple[str, ...]) -> str:
    return hashlib.sha256(json.dumps(
        [subscription_id, version, tombstones], separators=(",", ":"),
    ).encode()).hexdigest()


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for file in _safe_files(root):
        relative = file.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(file.stat().st_size.to_bytes(8, "big"))
        with file.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


class WorkspaceCollaboration:
    def __init__(self, storage: StorageLayout, repository: SharingRepository, *, clock=None) -> None:
        self.storage = storage
        self.repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def edit_note(
        self, *, actor_user_id: str, share_id: str, guid: str,
        fields: dict[str, str], expected_revision: int,
    ) -> WorkspaceNote:
        if not fields or any(not isinstance(value, str) for value in fields.values()):
            raise ValueError("at least one string field is required")
        self.repository.require_editor(actor_user_id=actor_user_id, share_id=share_id)
        paths = self.storage.share_paths(share_id)
        with self._lock(paths.root / ".workspace-edit.lock"):
            revision = self.repository.get_workspace_revision(
                actor_user_id=actor_user_id, share_id=share_id,
                entity_type="note", entity_id=guid,
            )
            current = revision.revision if revision else 0
            if current != expected_revision:
                raise EditConflictError(self._read_note(paths.collection, guid, current))
            col = Collection(str(paths.collection), server=False)
            old: dict[str, str] | None = None
            try:
                note_id = col.db.scalar("SELECT id FROM notes WHERE guid=?", guid)
                if note_id is None:
                    raise ValueError("workspace note not found")
                note = col.get_note(note_id)
                old = dict(note.items())
                unknown = set(fields) - set(old)
                if unknown:
                    raise ValueError(f"unknown note fields: {sorted(unknown)}")
                for name, value in fields.items():
                    note[name] = value
                col.update_note(note)
            finally:
                col.close()
            try:
                committed = self.repository.commit_workspace_revision(
                    actor_user_id=actor_user_id, share_id=share_id,
                    entity_type="note", entity_id=guid,
                    expected_revision=expected_revision, now=self._clock(),
                )
            except BaseException:
                if old is not None:
                    self._restore_fields(paths.collection, guid, old)
                raise
            return self._read_note(paths.collection, guid, committed.revision)

    @staticmethod
    def _read_note(collection: Path, guid: str, revision: int) -> WorkspaceNote:
        col = Collection(str(collection), server=False)
        try:
            note_id = col.db.scalar("SELECT id FROM notes WHERE guid=?", guid)
            if note_id is None:
                raise ValueError("workspace note not found")
            note = col.get_note(note_id)
            return WorkspaceNote(
                guid=guid, fields=dict(note.items()), tags=tuple(sorted(note.tags)),
                revision=revision,
            )
        finally:
            col.close()

    @staticmethod
    def _restore_fields(collection: Path, guid: str, fields: dict[str, str]) -> None:
        col = Collection(str(collection), server=False)
        try:
            note = col.get_note(col.db.scalar("SELECT id FROM notes WHERE guid=?", guid))
            for name, value in fields.items():
                note[name] = value
            col.update_note(note)
        finally:
            col.close()

    @staticmethod
    @contextmanager
    def _lock(path: Path):
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


class SubscriptionUpdater:
    def __init__(self, storage: StorageLayout, repository: SharingRepository, *, clock=None) -> None:
        self.storage = storage
        self.repository = repository
        self.jobs = JobRepository(repository.database)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def preview_mirror(
        self, *, actor_user_id: str, subscription_id: str, target_version: int,
    ) -> MirrorPreview:
        subscription = self.repository.get_subscription(
            actor_user_id=actor_user_id, subscription_id=subscription_id,
        )
        tombstones = self._tombstones(
            actor_user_id, subscription.share_id,
            subscription.installed_release, target_version,
        )
        return MirrorPreview(
            subscription_id=subscription_id, target_version=target_version,
            tombstones=tombstones,
            digest=_preview_digest(subscription_id, target_version, tombstones),
        )

    def run(
        self, *, actor_user_id: str, subscription_id: str, target_version: int,
        idempotency_key: str, mirror_preview_digest: str | None = None,
        approve_templates: bool = False, manual_values: dict[str, str] | None = None,
    ) -> UpdateResult:
        subscription = self.repository.get_subscription(
            actor_user_id=actor_user_id, subscription_id=subscription_id,
        )
        if target_version <= subscription.installed_release:
            raise ValueError("target release must be newer than installed release")
        target = ReleasePublisher(self.storage, self.repository, clock=self._clock).validate_release(
            actor_user_id=actor_user_id, share_id=subscription.share_id,
            version=target_version,
        )
        installed = ReleasePublisher(
            self.storage, self.repository, clock=self._clock,
        ).validate_release(
            actor_user_id=actor_user_id, share_id=subscription.share_id,
            version=subscription.installed_release,
        )
        self._check_templates(
            json.loads(installed.manifest_path.read_text(encoding="utf-8")),
            json.loads(target.manifest_path.read_text(encoding="utf-8")),
            approve_templates,
        )
        tombstones = self._tombstones(
            actor_user_id, subscription.share_id,
            subscription.installed_release, target_version,
        )
        if subscription.conflict_policy == "mirror" and tombstones:
            expected = _preview_digest(subscription_id, target_version, tombstones)
            if mirror_preview_digest != expected:
                raise MirrorPreviewRequired("mirror deletion requires an exact current preview")
        request = json.dumps({
            "subscription_id": subscription_id, "target_version": target_version,
            "approve_templates": approve_templates,
            "mirror_preview_digest": mirror_preview_digest,
        }, sort_keys=True, separators=(",", ":")).encode()
        job, created = self.jobs.create_or_get(
            actor_user_id=actor_user_id, resource_type="subscription",
            resource_id=subscription_id, capability="share.update",
            idempotency_key=idempotency_key, request_hash=request_digest(request),
            now=self._clock(),
        )
        if job.state is JobState.SUCCEEDED:
            return UpdateResult(job.id, True, target_version)
        if created or job.state is JobState.QUEUED:
            job = self.jobs.transition(
                job.id, expected=JobState.QUEUED, target=JobState.RUNNING,
                now=self._clock(), progress={
                    "target_version": target_version,
                    "mirror_preview_digest": mirror_preview_digest,
                    "approve_templates": approve_templates,
                },
            )
        elif job.state is not JobState.RUNNING:
            raise ConflictError("update job is not resumable")
        return self._apply(
            actor_user_id=actor_user_id, subscription=subscription,
            target=target, installed=installed, tombstones=tombstones,
            job_id=job.id, approve_templates=approve_templates,
            manual_values=manual_values or {},
        )

    def _apply(
        self, *, actor_user_id, subscription, target, installed,
        tombstones: tuple[str, ...], job_id: str, approve_templates: bool,
        manual_values: dict[str, str],
    ) -> UpdateResult:
        target_manifest = json.loads(target.manifest_path.read_text(encoding="utf-8"))
        installed_manifest = json.loads(installed.manifest_path.read_text(encoding="utf-8"))
        self._check_templates(installed_manifest, target_manifest, approve_templates)
        user_paths = self.storage.user_paths(actor_user_id)
        _safe_files(user_paths.root)
        stage = self.storage.users_root / f".{actor_user_id}.update-{uuid.uuid4().hex}"
        quarantine = self.storage.users_root / f".{actor_user_id}.update-old-{uuid.uuid4().hex}"
        upstream_root = self.storage.users_root / f".{actor_user_id}.upstream-{uuid.uuid4().hex}"
        _copy_tree(user_paths.root, stage)
        upstream_root.mkdir(mode=0o700)
        upstream_collection = upstream_root / "collection.anki2"
        self.jobs.append_journal(job_id, phase="stage", payload={
            "user_id": actor_user_id, "stage_key": stage.name,
            "upstream_key": upstream_root.name,
        }, now=self._clock())
        try:
            _import_package(upstream_collection, target.bundle_path)
            stage_collection = stage / "anki" / "collection.anki2"
            before = ReleaseInstaller._schedule_snapshot(stage_collection)
            conflicts = self._merge_content(
                stage_collection=stage_collection, upstream_collection=upstream_collection,
                target_manifest=target_manifest, subscription_id=subscription.id,
                actor_user_id=actor_user_id, job_id=job_id,
                manual_values=manual_values,
            )
            if conflicts:
                self.jobs.append_journal(
                    job_id, phase="awaiting_conflicts", payload={}, now=self._clock(),
                )
                raise UnresolvedUpdateError(job_id, conflicts)
            media_conflicts, media_to_copy = self._merge_media(
                stage_collection=stage_collection,
                upstream_collection=upstream_collection,
                target_manifest=target_manifest,
                subscription_id=subscription.id,
                actor_user_id=actor_user_id, job_id=job_id,
            )
            if media_conflicts:
                self.jobs.append_journal(
                    job_id, phase="awaiting_conflicts", payload={}, now=self._clock(),
                )
                raise UnresolvedUpdateError(job_id, media_conflicts)
            self._apply_tombstones(
                stage_collection, subscription, tombstones,
            )
            if approve_templates:
                self._apply_safe_templates(
                    stage_collection, upstream_collection, target_manifest,
                )
            after = ReleaseInstaller._schedule_snapshot(
                stage_collection, card_ids={row[0] for row in before[0]},
            )
            surviving_ids = {row[0] for row in after[0]}
            before_surviving_cards = [row for row in before[0] if row[0] in surviving_ids]
            before_surviving_reviews = [row for row in before[1] if row[1] in surviving_ids]
            if (before_surviving_cards, before_surviving_reviews) != after:
                raise RuntimeError("subscription update changed scheduling or review history")
            self._copy_media(upstream_collection, stage_collection, media_to_copy)
            if not _integrity_ok(stage_collection):
                raise RuntimeError("updated collection integrity failed")
            # Collection close/checkpoint can leave transient WAL names visible for a
            # moment; no runtime owns this disposable stage, so remove empty sidecars
            # before deterministic quota and tree-digest walks.
            for suffix in ("-wal", "-shm"):
                Path(f"{stage_collection}{suffix}").unlink(missing_ok=True)
            ReleaseInstaller(self.storage, self.repository)._check_recipient_quota(
                actor_user_id, stage,
            )
            mappings, _ = ReleaseInstaller._entity_mappings(stage_collection, target_manifest)
            _secure_tree(stage)
            self.jobs.append_journal(job_id, phase="validated", payload={}, now=self._clock())
            self.jobs.append_journal(job_id, phase="swap_intent", payload={
                "user_id": actor_user_id,
                "subscription_id": subscription.id,
                "expected_version": subscription.installed_release,
                "target_version": target.version,
                "stage_key": stage.name,
                "quarantine_key": quarantine.name,
                "old_root_sha256": _tree_digest(user_paths.root),
                "new_root_sha256": _tree_digest(stage),
            }, now=self._clock())
            os.replace(user_paths.root, quarantine)
            ReleaseInstaller._fsync_directory(self.storage.users_root)
            self.jobs.append_journal(
                job_id, phase="old_quarantined", payload={}, now=self._clock(),
            )
            try:
                os.replace(stage, user_paths.root)
                ReleaseInstaller._fsync_directory(self.storage.users_root)
                self.jobs.append_journal(job_id, phase="new_active", payload={}, now=self._clock())
                try:
                    self.repository.commit_subscription_update(
                        actor_user_id=actor_user_id, subscription_id=subscription.id,
                        expected_version=subscription.installed_release,
                        target_version=target.version, entities=mappings, now=self._clock(),
                    )
                except BaseException:
                    failed = self.storage.users_root / f".{actor_user_id}.failed-{uuid.uuid4().hex}"
                    os.replace(user_paths.root, failed)
                    os.replace(quarantine, user_paths.root)
                    ReleaseInstaller._fsync_directory(self.storage.users_root)
                    shutil.rmtree(failed)
                    raise
            except BaseException:
                if not user_paths.root.exists() and quarantine.exists():
                    os.replace(quarantine, user_paths.root)
                raise
            self.jobs.append_journal(job_id, phase="db_commit", payload={}, now=self._clock())
            shutil.rmtree(quarantine)
            self.jobs.transition(
                job_id, expected=JobState.RUNNING, target=JobState.SUCCEEDED,
                now=self._clock(), progress={"installed_release": target.version},
            )
            return UpdateResult(job_id, True, target.version)
        finally:
            if stage.exists() and not stage.is_symlink():
                shutil.rmtree(stage)
            if upstream_root.exists() and not upstream_root.is_symlink():
                shutil.rmtree(upstream_root)

    def recover_all(self) -> int:
        """Recover interrupted swaps before runtimes begin opening user collections."""
        with self.repository.database.read() as conn:
            jobs = conn.execute(
                """SELECT id,actor_user_id,resource_id FROM jobs
                   WHERE capability='share.update' AND state='running'"""
            ).fetchall()
        recovered = 0
        for job in jobs:
            with self.repository.database.read() as conn:
                rows = conn.execute(
                    """SELECT phase,payload_json FROM job_journal
                       WHERE job_id=? ORDER BY sequence""", (job["id"],),
                ).fetchall()
            intent = next(
                (json.loads(row["payload_json"]) for row in reversed(rows)
                 if row["phase"] == "swap_intent"),
                None,
            )
            if intent is not None:
                self._recover_swap(job["id"], job["actor_user_id"], intent)
                recovered += 1
            elif rows and rows[-1]["phase"] != "awaiting_conflicts":
                stage_payload = next(
                    (json.loads(row["payload_json"]) for row in rows
                     if row["phase"] == "stage"), None,
                )
                if stage_payload is not None:
                    self._recover_pre_swap(job["id"], job["actor_user_id"], stage_payload)
                    recovered += 1
        return recovered

    def _recover_pre_swap(self, job_id: str, actor_user_id: str, payload: dict) -> None:
        if payload.get("user_id") != actor_user_id:
            raise RuntimeError("update recovery actor mismatch")
        for key_name in ("stage_key", "upstream_key"):
            key = payload.get(key_name, "")
            if not key or Path(key).name != key:
                raise RuntimeError("unsafe update recovery path key")
            path = self.storage.users_root / key
            if path.exists():
                if path.is_symlink():
                    raise RuntimeError("unsafe update recovery temporary root")
                shutil.rmtree(path)
        current = self.jobs.get(job_id)
        if current and current.state is JobState.RUNNING:
            self.jobs.transition(
                job_id, expected=JobState.RUNNING, target=JobState.FAILED,
                now=self._clock(), progress={"recovered": True},
                error_code="recovered_pre_swap",
            )

    def _recover_swap(self, job_id: str, actor_user_id: str, intent: dict) -> None:
        if intent.get("user_id") != actor_user_id:
            raise RuntimeError("update recovery actor mismatch")
        for key_name in ("stage_key", "quarantine_key"):
            key = intent.get(key_name, "")
            if not key or Path(key).name != key:
                raise RuntimeError("unsafe update recovery path key")
        active = self.storage.user_paths(actor_user_id).root
        stage = self.storage.users_root / intent["stage_key"]
        quarantine = self.storage.users_root / intent["quarantine_key"]
        with self.repository.database.read() as conn:
            row = conn.execute(
                """SELECT installed_release FROM share_subscriptions
                   WHERE id=? AND user_id=?""",
                (intent["subscription_id"], actor_user_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("update recovery subscription missing")
        committed = int(row["installed_release"]) == int(intent["target_version"])

        def verified(root: Path, expected: str) -> bool:
            collection = root / "anki" / "collection.anki2"
            return (
                root.is_dir() and not root.is_symlink() and collection.is_file()
                and _tree_digest(root) == expected
                and _integrity_ok(collection)
            )

        if committed:
            if not verified(active, intent["new_root_sha256"]):
                raise RuntimeError("committed update root failed recovery validation")
            if quarantine.exists() and not quarantine.is_symlink():
                shutil.rmtree(quarantine)
            if stage.exists() and not stage.is_symlink():
                shutil.rmtree(stage)
            current = self.jobs.get(job_id)
            if current and current.state is JobState.RUNNING:
                self.jobs.transition(
                    job_id, expected=JobState.RUNNING, target=JobState.SUCCEEDED,
                    now=self._clock(), progress={
                        "installed_release": intent["target_version"], "recovered": True,
                    },
                )
        else:
            if verified(quarantine, intent["old_root_sha256"]):
                if active.exists():
                    failed = self.storage.users_root / f".{actor_user_id}.recovery-failed-{uuid.uuid4().hex}"
                    os.replace(active, failed)
                    os.replace(quarantine, active)
                    shutil.rmtree(failed)
                else:
                    os.replace(quarantine, active)
            elif not verified(active, intent["old_root_sha256"]):
                raise RuntimeError("no verified pre-update root available for recovery")
            if stage.exists() and not stage.is_symlink():
                shutil.rmtree(stage)
        ReleaseInstaller._fsync_directory(self.storage.users_root)

    def _merge_content(
        self, *, stage_collection: Path, upstream_collection: Path,
        target_manifest: dict, subscription_id: str, actor_user_id: str,
        job_id: str, manual_values: dict[str, str],
    ) -> tuple[VisibleUpdateConflict, ...]:
        mappings = self.repository.list_subscription_entities(
            actor_user_id=actor_user_id, subscription_id=subscription_id,
        )
        base = {(item.entity_type, item.source_id): item for item in mappings}
        existing = {
            (item.entity_type, item.source_id, item.field_name): item
            for item in self.repository.list_job_conflicts(
                actor_user_id=actor_user_id, job_id=job_id,
            )
        }
        pending: list[dict] = []
        visible_values: dict[tuple[str, str, str], tuple[str, str]] = {}
        local = Collection(str(stage_collection), server=False)
        upstream = Collection(str(upstream_collection), server=False)
        try:
            for guid, note_meta in target_manifest["entities"]["notes"].items():
                upstream_id = upstream.db.scalar("SELECT id FROM notes WHERE guid=?", guid)
                upstream_note = upstream.get_note(upstream_id)
                local_id = local.db.scalar("SELECT id FROM notes WHERE guid=?", guid)
                if local_id is None:
                    self._copy_new_note(local, upstream, upstream_note)
                    continue
                local_note = local.get_note(local_id)
                changed = False
                for field_name, upstream_hash in note_meta["fields"].items():
                    source = _field_source(guid, field_name)
                    prior = base.get(("note_field", source))
                    if prior is None:
                        raise RuntimeError("subscription lacks field-granular base mapping")
                    local_value, upstream_value = local_note[field_name], upstream_note[field_name]
                    local_hash = _json_hash(local_value)
                    if upstream_hash == prior.base_hash or local_hash == upstream_hash:
                        continue
                    if local_hash == prior.base_hash:
                        local_note[field_name] = upstream_value
                        changed = True
                        continue
                    key = ("note", guid, field_name)
                    resolution = existing.get(key)
                    if resolution and resolution.resolution == "upstream":
                        local_note[field_name] = upstream_value
                        changed = True
                    elif resolution and resolution.resolution == "mine":
                        pass
                    elif resolution and resolution.resolution == "manual" and resolution.id in manual_values:
                        local_note[field_name] = manual_values[resolution.id]
                        changed = True
                    else:
                        pending.append({
                            "entity_type": "note", "source_id": guid,
                            "field_name": field_name, "base_hash": prior.base_hash,
                            "local_hash": local_hash, "upstream_hash": upstream_hash,
                        })
                        visible_values[key] = (local_value, upstream_value)
                tags_source = ("note_tags", guid)
                tags_base = base.get(tags_source)
                if tags_base is None:
                    raise RuntimeError("subscription lacks tag base mapping")
                local_tags = sorted(local_note.tags)
                upstream_tags = sorted(upstream_note.tags)
                local_tags_hash = _json_hash(local_tags)
                upstream_tags_hash = note_meta["tags"]
                if upstream_tags_hash != tags_base.base_hash and local_tags_hash != upstream_tags_hash:
                    if local_tags_hash == tags_base.base_hash:
                        local_note.tags = upstream_tags
                        changed = True
                    else:
                        key = ("note", guid, "__tags__")
                        resolution = existing.get(key)
                        if resolution and resolution.resolution == "upstream":
                            local_note.tags = upstream_tags
                            changed = True
                        elif resolution and resolution.resolution == "mine":
                            pass
                        elif resolution and resolution.resolution == "manual" and resolution.id in manual_values:
                            try:
                                manual_tags = json.loads(manual_values[resolution.id])
                            except (TypeError, ValueError) as exc:
                                raise ValueError("manual tag resolution must be a JSON list") from exc
                            if not isinstance(manual_tags, list) or not all(
                                isinstance(tag, str) for tag in manual_tags
                            ):
                                raise ValueError("manual tag resolution must be a JSON list")
                            local_note.tags = manual_tags
                            changed = True
                        else:
                            pending.append({
                                "entity_type": "note", "source_id": guid,
                                "field_name": "__tags__", "base_hash": tags_base.base_hash,
                                "local_hash": local_tags_hash,
                                "upstream_hash": upstream_tags_hash,
                            })
                            visible_values[key] = (
                                json.dumps(local_tags, ensure_ascii=False),
                                json.dumps(upstream_tags, ensure_ascii=False),
                            )
                if changed:
                    local.update_note(local_note)
        finally:
            upstream.close()
            local.close()
        if not pending:
            return ()
        records = self.repository.store_update_conflicts(
            actor_user_id=actor_user_id, job_id=job_id,
            subscription_id=subscription_id, conflicts=pending, now=self._clock(),
        )
        return tuple(
            VisibleUpdateConflict(
                id=record.id, field_name=record.field_name or "",
                local_value=visible_values[(record.entity_type, record.source_id, record.field_name)][0],
                upstream_value=visible_values[(record.entity_type, record.source_id, record.field_name)][1],
                record=record,
            )
            for record in records
            if (record.entity_type, record.source_id, record.field_name) in visible_values
            and (
                record.resolution is None
                or record.resolution == "manual" and record.id not in manual_values
            )
        )

    def _merge_media(
        self, *, stage_collection: Path, upstream_collection: Path,
        target_manifest: dict, subscription_id: str, actor_user_id: str, job_id: str,
    ) -> tuple[tuple[VisibleUpdateConflict, ...], tuple[str, ...]]:
        mappings = self.repository.list_subscription_entities(
            actor_user_id=actor_user_id, subscription_id=subscription_id,
        )
        base = {
            item.source_id: item for item in mappings if item.entity_type == "media"
        }
        existing = {
            (item.entity_type, item.source_id, item.field_name): item
            for item in self.repository.list_job_conflicts(
                actor_user_id=actor_user_id, job_id=job_id,
            )
        }
        local_root = stage_collection.with_name("collection.media")
        upstream_root = upstream_collection.with_name("collection.media")
        pending: list[dict] = []
        values: dict[tuple[str, str, str], tuple[str, str]] = {}
        copy_names: list[str] = []
        for name, upstream_hash in target_manifest["media_sha256"].items():
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != name:
                raise ValueError("unsafe release media name")
            upstream_file = upstream_root / relative
            if not upstream_file.is_file():
                raise RuntimeError("release media is missing")
            local_file = local_root / relative
            prior = base.get(name)
            if not local_file.exists():
                copy_names.append(name)
                continue
            local_hash = hashlib.sha256(local_file.read_bytes()).hexdigest()
            if prior is None:
                if local_hash == upstream_hash:
                    continue
                key = ("media", name, "__bytes__")
                resolution = existing.get(key)
                if resolution and resolution.resolution == "upstream":
                    copy_names.append(name)
                elif resolution and resolution.resolution == "mine":
                    pass
                else:
                    absent_hash = _json_hash(None)
                    pending.append({
                        "entity_type": "media", "source_id": name,
                        "field_name": "__bytes__", "base_hash": absent_hash,
                        "local_hash": local_hash, "upstream_hash": upstream_hash,
                    })
                    values[key] = (local_hash, upstream_hash)
                continue
            if upstream_hash == prior.base_hash or local_hash == upstream_hash:
                continue
            if local_hash == prior.base_hash:
                copy_names.append(name)
                continue
            key = ("media", name, "__bytes__")
            resolution = existing.get(key)
            if resolution and resolution.resolution == "upstream":
                copy_names.append(name)
            elif resolution and resolution.resolution == "mine":
                pass
            else:
                pending.append({
                    "entity_type": "media", "source_id": name,
                    "field_name": "__bytes__", "base_hash": prior.base_hash,
                    "local_hash": local_hash, "upstream_hash": upstream_hash,
                })
                values[key] = (local_hash, upstream_hash)
        if not pending:
            return (), tuple(copy_names)
        records = self.repository.store_update_conflicts(
            actor_user_id=actor_user_id, job_id=job_id,
            subscription_id=subscription_id, conflicts=pending, now=self._clock(),
        )
        visible = tuple(
            VisibleUpdateConflict(
                id=record.id, field_name=record.field_name or "",
                local_value=values[(record.entity_type, record.source_id, record.field_name)][0],
                upstream_value=values[(record.entity_type, record.source_id, record.field_name)][1],
                record=record,
            )
            for record in records
            if (record.entity_type, record.source_id, record.field_name) in values
            and record.resolution not in {"upstream", "mine"}
        )
        return visible, tuple(copy_names)

    @staticmethod
    def _copy_new_note(local: Collection, upstream: Collection, upstream_note) -> None:
        upstream_model = upstream_note.note_type()
        model = local.models.by_name(upstream_model["name"])
        if model is None:
            raise RuntimeError("new upstream note requires an unavailable note type")
        note = local.new_note(model)
        note.guid = upstream_note.guid
        for name, value in upstream_note.items():
            note[name] = value
        note.tags = list(upstream_note.tags)
        card_id = upstream_note.card_ids()[0]
        deck_name = upstream.decks.get(upstream.get_card(card_id).did)["name"]
        local.add_note(note, local.decks.id(deck_name))

    @staticmethod
    def _apply_tombstones(collection: Path, subscription, tombstones: tuple[str, ...]) -> None:
        col = Collection(str(collection), server=False)
        try:
            for guid in tombstones:
                note_id = col.db.scalar("SELECT id FROM notes WHERE guid=?", guid)
                if note_id is None:
                    continue
                if subscription.conflict_policy == "mirror":
                    col.remove_notes([note_id])
                else:
                    note = col.get_note(note_id)
                    note.add_tag("ankiweb::retired")
                    col.update_note(note)
        finally:
            col.close()

    @staticmethod
    def _copy_media(
        upstream_collection: Path, stage_collection: Path, names: tuple[str, ...],
    ) -> None:
        source = upstream_collection.with_name("collection.media")
        target = stage_collection.with_name("collection.media")
        target.mkdir(mode=0o700, exist_ok=True)
        for name in names:
            source_file = source / name
            target_file = target / name
            target_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(source_file, target_file)
            os.chmod(target_file, 0o600)

    @staticmethod
    def _template_sequences(manifest: dict) -> dict[str, list[str]]:
        grouped: dict[str, list[tuple[int, str]]] = {}
        for source_id, value in manifest["entities"]["templates"].items():
            model, _ = source_id.rsplit(":", 1)
            grouped.setdefault(model, []).append((int(value["ordinal"]), value["name"]))
        return {model: [name for _, name in sorted(items)] for model, items in grouped.items()}

    def _check_templates(self, old: dict, new: dict, approved: bool) -> None:
        old_sequences = self._template_sequences(old)
        new_sequences = self._template_sequences(new)
        target_models = {
            note.get("model") for note in new["entities"]["notes"].values()
            if note.get("model")
        }
        relevant_old = {
            key: value for key, value in old["entities"]["templates"].items()
            if key.rsplit(":", 1)[0] in target_models
        }
        relevant_new = {
            key: value for key, value in new["entities"]["templates"].items()
            if key.rsplit(":", 1)[0] in target_models
        }
        if relevant_old == relevant_new:
            return
        destructive = any(
            len(new_sequences.get(model, [])) < len(names)
            or new_sequences.get(model, [])[:len(names)] != names
            for model, names in old_sequences.items()
            if model in target_models
        )
        if destructive:
            raise DestructiveTemplateChangeError(
                "template removal or reorder cannot be applied while retaining cards"
            )
        if not approved:
            raise DestructiveTemplateChangeError("template changes require recipient approval")

    @staticmethod
    def _apply_safe_templates(local_path: Path, upstream_path: Path, manifest: dict) -> None:
        local = Collection(str(local_path), server=False)
        upstream = Collection(str(upstream_path), server=False)
        try:
            allowed_models = {
                source_id.rsplit(":", 1)[0]
                for source_id in manifest["entities"]["templates"]
            }
            for item in upstream.models.all_names_and_ids():
                if item.name not in allowed_models:
                    continue
                source = upstream.models.get(item.id)
                target = local.models.by_name(source["name"])
                if target is None:
                    continue
                if len(source.get("tmpls", [])) < len(target.get("tmpls", [])):
                    raise DestructiveTemplateChangeError("template removal is not safely supported")
                for ordinal, source_template in enumerate(source.get("tmpls", [])):
                    if ordinal >= len(target.get("tmpls", [])):
                        added = local.models.new_template(source_template.get("name", "Card"))
                        for key in ("name", "qfmt", "afmt", "bqfmt", "bafmt", "did", "bfont", "bsize"):
                            if key in source_template:
                                added[key] = copy.deepcopy(source_template[key])
                        local.models.add_template(target, added)
                    else:
                        destination = target["tmpls"][ordinal]
                        for key in ("name", "qfmt", "afmt", "bqfmt", "bafmt", "did", "bfont", "bsize"):
                            if key in source_template:
                                destination[key] = copy.deepcopy(source_template[key])
                target["css"] = source.get("css", "")
                local.models.update_dict(target)
        finally:
            upstream.close()
            local.close()

    def _tombstones(
        self, actor_user_id: str, share_id: str, installed: int, target: int,
    ) -> tuple[str, ...]:
        if target <= installed:
            return ()
        publisher = ReleasePublisher(self.storage, self.repository, clock=self._clock)
        removed: set[str] = set()
        for version in range(installed + 1, target + 1):
            release = publisher.validate_release(
                actor_user_id=actor_user_id, share_id=share_id, version=version,
            )
            manifest = json.loads(release.manifest_path.read_text(encoding="utf-8"))
            removed.update(manifest.get("tombstones", []))
        return tuple(sorted(removed))
