"""Anki-backed workspace, immutable release, and staged install operations."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from anki.collection import Collection
import anki.import_export_pb2 as ie
from anki.generic_pb2 import Empty

from ankiweb.sharing.repository import SharingRepository
from ankiweb.tenancy import SharePaths, StorageLayout


class ImmutableReleaseError(RuntimeError):
    pass


class QuotaExceededError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PublishedRelease:
    share_id: str
    version: int
    manifest_path: Path
    bundle_path: Path
    manifest_sha256: str
    bundle_sha256: str


@dataclass(frozen=True, slots=True)
class InstallResult:
    share_id: str
    installed_release: int
    mode: str
    subscription_id: str | None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _json_hash(value) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _safe_files(root: Path) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"unsafe managed root: {root}")
    files: list[Path] = []
    for current, directories, names in os.walk(root, followlinks=False):
        base = Path(current)
        for name in directories:
            path = base / name
            if path.is_symlink():
                raise ValueError(f"symlink not allowed: {path}")
        for name in names:
            path = base / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError(f"non-regular or hardlinked file not allowed: {path}")
            files.append(path)
    return sorted(files)


def _secure_tree(root: Path) -> None:
    for file in _safe_files(root):
        os.chmod(file, 0o600, follow_symlinks=False)
        fd = os.open(file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
    for directory in sorted(directories, key=lambda value: len(value.parts), reverse=True):
        os.chmod(directory, 0o700, follow_symlinks=False)
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _tree_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in _safe_files(root)) if root.exists() else 0


def _copy_tree(source: Path, target: Path) -> None:
    _safe_files(source)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.mkdir(mode=0o700, parents=True)
    for directory in sorted(
        (path for path in source.rglob("*") if path.is_dir()), key=lambda item: len(item.parts),
    ):
        if directory.is_symlink():
            raise ValueError(f"symlink not allowed: {directory}")
        (target / directory.relative_to(source)).mkdir(mode=0o700, parents=True, exist_ok=True)
    for file in _safe_files(source):
        destination = target / file.relative_to(source)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        source_fd = os.open(file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        target_fd = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
            getattr(os, "O_NOFOLLOW", 0), 0o600,
        )
        try:
            while chunk := os.read(source_fd, 1024 * 1024):
                view = memoryview(chunk)
                while view:
                    view = view[os.write(target_fd, view):]
            os.fsync(target_fd)
        finally:
            os.close(target_fd)
            os.close(source_fd)
    _secure_tree(target)


def _export_deck(collection: Path, deck_id: int, package: Path) -> None:
    col = Collection(str(collection), server=False)
    try:
        deck = col.decks.get(deck_id, default=False)
        if deck is None:
            raise ValueError("source deck not found")
        # The current backend's deck/note ExportLimit includes unrelated notes. Prune only
        # the disposable snapshot to the chosen deck hierarchy, then export it whole.
        root_name = deck["name"]
        subtree_ids = sorted(
            int(candidate.id)
            for candidate in col.decks.all_names_and_ids()
            if candidate.name == root_name or candidate.name.startswith(f"{root_name}::")
        )
        marks = ",".join("?" for _ in subtree_ids)
        selected = set(col.db.list(
            f"SELECT DISTINCT nid FROM cards WHERE did IN ({marks})", *subtree_ids,
        ))
        outside = set(col.find_notes("")) - selected
        if outside:
            col.remove_notes(list(outside))
        options = ie.ExportAnkiPackageOptions(
            with_scheduling=False, with_media=True, with_deck_configs=False, legacy=False,
        )
        col.export_anki_package(
            out_path=str(package), options=options,
            limit=ie.ExportLimit(whole_collection=Empty()),
        )
    finally:
        col.close()


def _import_package(collection: Path, package: Path) -> None:
    col = Collection(str(collection), server=False)
    try:
        col.import_anki_package(ie.ImportAnkiPackageRequest(package_path=str(package)))
    finally:
        col.close()


def _integrity_ok(collection: Path) -> bool:
    col = Collection(str(collection), server=False)
    try:
        return col.db.list("PRAGMA integrity_check") == ["ok"]
    finally:
        col.close()


def _whole_collection_package(collection: Path, package: Path) -> None:
    col = Collection(str(collection), server=False)
    try:
        options = ie.ExportAnkiPackageOptions(
            with_scheduling=False, with_media=True, with_deck_configs=False, legacy=False,
        )
        col.export_anki_package(
            out_path=str(package), options=options,
            limit=ie.ExportLimit(whole_collection=Empty()),
        )
    finally:
        col.close()


class WorkspaceProvisioner:
    review_enabled = False

    def __init__(self, storage: StorageLayout) -> None:
        self.storage = storage

    def create_from_owner_deck(
        self, *, actor_user_id: str, owner_collection: Path, share_id: str,
        deck_id: int, authorize,
    ) -> SharePaths:
        share = authorize(actor_user_id=actor_user_id, share_id=share_id)
        paths = self.storage.share_paths(share_id)
        if paths.collection.exists() or paths.root.is_symlink():
            raise FileExistsError("share workspace already exists")
        source = Path(owner_collection)
        source_root = source.parent
        _safe_files(source_root)
        for suffix in ("-wal", "-shm"):
            if Path(f"{source}{suffix}").exists():
                raise RuntimeError("owner collection must be quiesced before workspace copy")
        self.storage.prepare_share(share_id)
        snapshot = paths.root / f".source-{uuid.uuid4().hex}"
        package = paths.root / f".workspace-{uuid.uuid4().hex}.apkg"
        try:
            _copy_tree(source_root, snapshot)
            _export_deck(snapshot / source.name, deck_id, package)
            _import_package(paths.collection, package)
            self._validate_workspace(paths.collection)
            shutil.rmtree(snapshot)
            package.unlink()
            self._check_owner_quota(share.owner_user_id, paths.root)
            _secure_tree(paths.root)
            return paths
        except BaseException:
            if paths.root.exists() and not paths.root.is_symlink():
                shutil.rmtree(paths.root)
            raise
        finally:
            if snapshot.exists() and not snapshot.is_symlink():
                shutil.rmtree(snapshot)
            package.unlink(missing_ok=True)

    def _check_owner_quota(self, owner_user_id: str, share_root: Path) -> None:
        with sqlite3.connect(self.storage.app_db) as conn:
            row = conn.execute(
                "SELECT storage_bytes FROM user_quotas WHERE user_id=?", (owner_user_id,),
            ).fetchone()
        if row and self.storage.user_usage_bytes(owner_user_id) + _tree_bytes(share_root) > row[0]:
            raise QuotaExceededError("owner storage quota exceeded")

    @staticmethod
    def _validate_workspace(collection: Path) -> None:
        if not _integrity_ok(collection):
            raise RuntimeError("workspace integrity validation failed")


class ReleasePublisher:
    def __init__(self, storage: StorageLayout, repository: SharingRepository, *, clock=None) -> None:
        self.storage = storage
        self.repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def publish(
        self, *, actor_user_id: str, share_id: str, version: int | None = None,
    ) -> PublishedRelease:
        paths = self.storage.share_paths(share_id)
        share = self.repository.require_owner(actor_user_id=actor_user_id, share_id=share_id)
        if not paths.collection.exists():
            raise FileNotFoundError("share workspace is not provisioned")
        self.storage.prepare_share(share_id)
        with self._publish_lock(paths):
            expected = self.repository.next_release_version(
                actor_user_id=actor_user_id, share_id=share_id,
            )
            requested = expected if version is None else version
            if requested != expected:
                raise ImmutableReleaseError("release version already exists or is not next")
            final = paths.releases / str(requested)
            if final.exists() or final.is_symlink():
                raise ImmutableReleaseError("release directory already exists")
            stage = paths.releases / f".stage-{requested}-{uuid.uuid4().hex}"
            stage.mkdir(mode=0o700)
            bundle = stage / "deck.apkg"
            manifest_path = stage / "manifest.json"
            try:
                _whole_collection_package(paths.collection, bundle)
                bundle_sha = _sha256(bundle)
                manifest = self._manifest(
                    paths, share_id=share_id, version=requested,
                    parent_version=requested - 1 or None, bundle_sha=bundle_sha,
                )
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                manifest_sha = _sha256(manifest_path)
                self._validate_reimport(bundle, manifest)
                self._check_owner_quota(share.owner_user_id, paths.root)
                _secure_tree(stage)
                os.replace(stage, final)
                _secure_tree(final)
                record = self.repository.commit_release(
                    actor_user_id=actor_user_id, share_id=share_id, version=requested,
                    manifest_path=f"releases/{requested}/manifest.json",
                    bundle_path=f"releases/{requested}/deck.apkg",
                    manifest_sha256=manifest_sha, bundle_sha256=bundle_sha,
                    now=self._clock(),
                )
                return PublishedRelease(
                    share_id=share_id, version=record.version,
                    manifest_path=final / "manifest.json", bundle_path=final / "deck.apkg",
                    manifest_sha256=manifest_sha, bundle_sha256=bundle_sha,
                )
            except BaseException:
                if stage.exists() and not stage.is_symlink():
                    shutil.rmtree(stage)
                # No DB row can point here if commit failed; lock prevents a concurrent winner.
                if final.exists() and self.repository.next_release_version(
                    actor_user_id=actor_user_id, share_id=share_id,
                ) == requested:
                    shutil.rmtree(final)
                raise

    def validate_release(
        self, *, actor_user_id: str, share_id: str, version: int,
    ) -> PublishedRelease:
        record = self.repository.get_release(
            actor_user_id=actor_user_id, share_id=share_id, version=version,
        )
        paths = self.storage.share_paths(share_id)
        expected_manifest = paths.releases / str(version) / "manifest.json"
        expected_bundle = paths.releases / str(version) / "deck.apkg"
        if record.manifest_path != f"releases/{version}/manifest.json" or \
                record.bundle_path != f"releases/{version}/deck.apkg":
            raise ImmutableReleaseError("release path metadata is invalid")
        _safe_files(expected_manifest.parent)
        if record.manifest_sha256 is None or \
                _sha256(expected_manifest) != record.manifest_sha256:
            raise ImmutableReleaseError("release manifest checksum mismatch")
        if _sha256(expected_bundle) != record.bundle_sha256:
            raise ImmutableReleaseError("release bundle checksum mismatch")
        try:
            manifest = json.loads(expected_manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ImmutableReleaseError("release manifest is invalid") from exc
        if manifest.get("share_id") != share_id or manifest.get("version") != version or \
                manifest.get("bundle_sha256") != record.bundle_sha256:
            raise ImmutableReleaseError("release manifest identity mismatch")
        return PublishedRelease(
            share_id=share_id, version=version, manifest_path=expected_manifest,
            bundle_path=expected_bundle, manifest_sha256=record.manifest_sha256,
            bundle_sha256=record.bundle_sha256,
        )

    def _manifest(
        self, paths: SharePaths, *, share_id: str, version: int,
        parent_version: int | None, bundle_sha: str,
    ) -> dict:
        col = Collection(str(paths.collection), server=False)
        try:
            notes: dict[str, dict] = {}
            model_ids: set[int] = set()
            deck_ids: set[int] = set()
            for note_id in col.find_notes(""):
                note = col.get_note(note_id)
                model_ids.add(int(note.mid))
                fields = {name: _json_hash(value) for name, value in note.items()}
                notes[note.guid] = {
                    "fields": fields,
                    "tags": _json_hash(sorted(note.tags)),
                    "hash": _json_hash({"fields": fields, "tags": sorted(note.tags)}),
                }
                deck_ids.update(int(col.get_card(card_id).did) for card_id in note.card_ids())
            templates: dict[str, dict] = {}
            for model_id in model_ids:
                model = col.models.get(model_id)
                for ordinal, template in enumerate(model.get("tmpls", [])):
                    stable_id = f"{model['name']}:{ordinal}"
                    templates[stable_id] = {
                        "name": template.get("name", ""), "ordinal": ordinal,
                        "hash": _json_hash(template),
                    }
            decks = {}
            for deck_id in deck_ids:
                deck = col.decks.get(deck_id)
                decks[deck["name"]] = {"hash": _json_hash({
                    "name": deck["name"], "description": deck.get("desc", ""),
                })}
        finally:
            col.close()
        media = {
            file.relative_to(paths.media).as_posix(): _sha256(file)
            for file in _safe_files(paths.media)
        }
        tombstones: list[str] = []
        if parent_version:
            parent = paths.releases / str(parent_version) / "manifest.json"
            if parent.exists():
                previous = json.loads(parent.read_text(encoding="utf-8"))
                tombstones = sorted(set(previous["entities"]["notes"]) - set(notes))
        return {
            "share_id": share_id, "version": version, "parent_version": parent_version,
            "anki_version": "26.09.3", "created_at": self._clock().isoformat(),
            "entities": {"notes": notes, "templates": templates, "decks": decks},
            "media_sha256": media, "tombstones": tombstones,
            "bundle_sha256": bundle_sha,
        }

    def _validate_reimport(self, bundle: Path, manifest: dict) -> None:
        scratch = bundle.parent / "validation.anki2"
        try:
            _import_package(scratch, bundle)
            if not _integrity_ok(scratch):
                raise ImmutableReleaseError("release re-import integrity failed")
            with sqlite3.connect(scratch) as conn:
                guids = {row[0] for row in conn.execute("SELECT guid FROM notes")}
            if guids != set(manifest["entities"]["notes"]):
                raise ImmutableReleaseError("release re-import entity mismatch")
            media_root = Path(f"{scratch.parent}/{scratch.stem}.media")
            for name, expected in manifest["media_sha256"].items():
                file = media_root / name
                if not file.is_file() or _sha256(file) != expected:
                    raise ImmutableReleaseError("release re-import media mismatch")
        finally:
            scratch.unlink(missing_ok=True)
            shutil.rmtree(scratch.with_name("collection.media"), ignore_errors=True)
            shutil.rmtree(scratch.with_suffix(".media"), ignore_errors=True)

    def _check_owner_quota(self, owner_user_id: str, share_root: Path) -> None:
        with self.repository.database.read() as conn:
            limit = conn.execute(
                "SELECT storage_bytes FROM user_quotas WHERE user_id=?", (owner_user_id,),
            ).fetchone()[0]
        if self.storage.user_usage_bytes(owner_user_id) + _tree_bytes(share_root) > limit:
            raise QuotaExceededError("owner storage quota exceeded")

    @staticmethod
    @contextmanager
    def _publish_lock(paths: SharePaths):
        lock = paths.root / ".publish.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("unsafe publish lock")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


class ReleaseInstaller:
    def __init__(self, storage: StorageLayout, repository: SharingRepository, *, clock=None) -> None:
        self.storage = storage
        self.repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def install(
        self, *, actor_user_id: str, share_id: str, version: int, mode: str,
    ) -> InstallResult:
        if mode not in {"copy", "follow"}:
            raise ValueError("install mode must be copy or follow")
        release = ReleasePublisher(
            self.storage, self.repository, clock=self._clock,
        ).validate_release(actor_user_id=actor_user_id, share_id=share_id, version=version)
        paths = self.storage.user_paths(actor_user_id)
        _safe_files(paths.root)
        stage = self.storage.users_root / f".{actor_user_id}.install-{uuid.uuid4().hex}"
        quarantine = self.storage.users_root / f".{actor_user_id}.install-old-{uuid.uuid4().hex}"
        _copy_tree(paths.root, stage)
        stage_collection = stage / "anki" / "collection.anki2"
        try:
            manifest = json.loads(release.manifest_path.read_text(encoding="utf-8"))
            with sqlite3.connect(stage_collection) as conn:
                existing_guids = {
                    row[0] for row in conn.execute("SELECT guid FROM notes").fetchall()
                }
            if existing_guids & set(manifest["entities"]["notes"]):
                raise RuntimeError("release note GUID collides with recipient content")
            before_cards, before_reviews = self._schedule_snapshot(stage_collection)
            _import_package(stage_collection, release.bundle_path)
            after_cards, after_reviews = self._schedule_snapshot(
                stage_collection, card_ids={row[0] for row in before_cards},
            )
            if before_cards != after_cards or before_reviews != after_reviews:
                raise RuntimeError("release install changed recipient scheduling or review history")
            if not _integrity_ok(stage_collection):
                raise RuntimeError("installed collection integrity failed")
            self._check_recipient_quota(actor_user_id, stage)
            entities, target_deck_id = self._entity_mappings(stage_collection, manifest)
            _secure_tree(stage)
            os.replace(paths.root, quarantine)
            try:
                os.replace(stage, paths.root)
                self._fsync_directory(self.storage.users_root)
            except BaseException:
                os.replace(quarantine, paths.root)
                self._fsync_directory(self.storage.users_root)
                raise
            subscription_id = None
            try:
                if mode == "follow":
                    subscription = self.repository.commit_follow_install(
                        actor_user_id=actor_user_id, share_id=share_id, version=version,
                        target_deck_id=target_deck_id, entities=entities, now=self._clock(),
                    )
                    subscription_id = subscription.id
            except BaseException:
                failed = self.storage.users_root / f".{actor_user_id}.failed-{uuid.uuid4().hex}"
                os.replace(paths.root, failed)
                os.replace(quarantine, paths.root)
                self._fsync_directory(self.storage.users_root)
                shutil.rmtree(failed)
                raise
            shutil.rmtree(quarantine)
            self._fsync_directory(self.storage.users_root)
            return InstallResult(
                share_id=share_id, installed_release=version, mode=mode,
                subscription_id=subscription_id,
            )
        finally:
            if stage.exists() and not stage.is_symlink():
                shutil.rmtree(stage)

    @staticmethod
    def _schedule_snapshot(collection: Path, card_ids: set[int] | None = None):
        with sqlite3.connect(collection) as conn:
            if card_ids is None:
                cards = conn.execute(
                    """SELECT id,due,ivl,factor,reps,lapses,queue,type,odue,odid,left
                       FROM cards ORDER BY id"""
                ).fetchall()
            elif card_ids:
                marks = ",".join("?" for _ in card_ids)
                cards = conn.execute(
                    f"""SELECT id,due,ivl,factor,reps,lapses,queue,type,odue,odid,left
                        FROM cards WHERE id IN ({marks}) ORDER BY id""",
                    tuple(sorted(card_ids)),
                ).fetchall()
            else:
                cards = []
            ids = [row[0] for row in cards]
            if ids:
                marks = ",".join("?" for _ in ids)
                reviews = conn.execute(
                    f"SELECT * FROM revlog WHERE cid IN ({marks}) ORDER BY id", ids,
                ).fetchall()
            else:
                reviews = []
        return cards, reviews

    def _check_recipient_quota(self, user_id: str, stage: Path) -> None:
        with self.repository.database.read() as conn:
            limit = conn.execute(
                "SELECT storage_bytes FROM user_quotas WHERE user_id=?", (user_id,),
            ).fetchone()[0]
        backups = self.storage.user_paths(user_id).backups
        if _tree_bytes(stage) + (_tree_bytes(backups) if backups.exists() else 0) > limit:
            raise QuotaExceededError("recipient storage quota exceeded")

    @staticmethod
    def _entity_mappings(collection: Path, manifest: dict) -> tuple[list[dict], int | None]:
        entities: list[dict] = []
        col = Collection(str(collection), server=False)
        try:
            for guid, value in manifest["entities"]["notes"].items():
                note_id = col.db.scalar("SELECT id FROM notes WHERE guid=?", guid)
                if note_id is None:
                    raise RuntimeError("installed note mapping is missing")
                entities.append({
                    "entity_type": "note", "source_id": guid,
                    "recipient_id": str(note_id), "base_hash": value["hash"],
                })
            target_deck = None
            for name, value in manifest["entities"]["decks"].items():
                deck = col.decks.by_name(name)
                if deck:
                    target_deck = int(deck["id"])
                    entities.append({
                        "entity_type": "deck", "source_id": name,
                        "recipient_id": str(deck["id"]), "base_hash": value["hash"],
                    })
            for source_id, value in manifest["entities"]["templates"].items():
                model_name, ordinal_text = source_id.rsplit(":", 1)
                model = col.models.by_name(model_name)
                ordinal = int(ordinal_text)
                if model is None or ordinal >= len(model.get("tmpls", [])):
                    raise RuntimeError("installed template mapping is missing")
                entities.append({
                    "entity_type": "template", "source_id": source_id,
                    "recipient_id": f"{model['id']}:{ordinal}", "base_hash": value["hash"],
                })
        finally:
            col.close()
        for name, digest in manifest["media_sha256"].items():
            entities.append({
                "entity_type": "media", "source_id": name, "recipient_id": name,
                "base_hash": digest, "media_name": name,
            })
        return entities, target_deck

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
