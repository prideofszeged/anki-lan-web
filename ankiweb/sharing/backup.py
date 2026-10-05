from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable

from ankiweb.adapters.anki.sharing import WorkspaceProvisioner
from ankiweb.tenancy import StorageLayout

from .repository import SharingRepository


MANIFEST_NAME = "manifest.json"
PAYLOAD_PREFIX = "payload"
SCHEMA_VERSION = 1


class BackupIntegrityError(RuntimeError):
    """A backup is incomplete, unsafe, corrupt, or belongs to another share."""


@dataclass(frozen=True, slots=True)
class ShareBackup:
    share_id: str
    archive: Path
    checksum: Path
    sha256: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedShareBackup:
    share_id: str
    archive: Path
    sha256: str
    created_at: datetime
    file_count: int
    release_count: int
    comment_count: int


@dataclass(frozen=True, slots=True)
class ShareBackupSummary:
    name: str
    size: int
    created_at: datetime


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ShareBackupManager:
    """Create and atomically restore owner-authorized share snapshots.

    Callers must hold ``RuntimeRegistry.maintenance(ResourceKey.share(id))`` for
    create/restore. This class independently verifies authorization immediately
    before the active-root swap so a queued job cannot outlive membership changes.
    """

    def __init__(
        self, storage: StorageLayout, repository: SharingRepository, *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.storage = storage
        self.repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def create(
        self, *, actor_user_id: str, share_id: str,
        operation_id: str | None = None,
    ) -> ShareBackup:
        self.repository.require_owner(actor_user_id=actor_user_id, share_id=share_id)
        paths = self.storage.share_paths(share_id)
        if not paths.collection.is_file():
            raise FileNotFoundError("share workspace does not exist")
        paths.backups.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(paths.backups, 0o700)
        files = self._source_files(paths.root)
        fingerprints = {
            relative: self._fingerprint(path) for relative, path in files.items()
        }
        hashes = {relative: self.sha256_file(path) for relative, path in files.items()}
        comments = self.repository.export_share_comments_for_backup(
            actor_user_id=actor_user_id, share_id=share_id,
        )
        now = self._clock().replace(microsecond=0)
        manifest = {
            "schema": SCHEMA_VERSION,
            "share_id": share_id,
            "created_at": now.isoformat(),
            "files": {
                relative: {"sha256": hashes[relative], "size": path.stat().st_size}
                for relative, path in files.items()
            },
            "comments": [self._comment_payload(item) for item in comments],
        }
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        archive = (
            paths.backups / f"share-{share_id}-job-{uuid.UUID(operation_id)}.tar.gz"
            if operation_id else
            paths.backups / f"share-{share_id}-{stamp}-{uuid.uuid4().hex[:8]}.tar.gz"
        )
        if operation_id and archive.exists():
            verified = self.verify(archive)
            if verified.share_id != share_id:
                raise BackupIntegrityError("backup belongs to a different share")
            return ShareBackup(
                share_id, archive, archive.with_name(archive.name + ".sha256"),
                verified.sha256, verified.created_at,
            )
        partial = archive.with_name(archive.name + ".partial")
        try:
            with partial.open("xb") as raw:
                os.chmod(partial, 0o600)
                with tarfile.open(fileobj=raw, mode="w:gz") as bundle:
                    encoded = (json.dumps(
                        manifest, separators=(",", ":"), sort_keys=True,
                    ) + "\n").encode()
                    self._add_bytes(bundle, MANIFEST_NAME, encoded, now)
                    for relative, path in files.items():
                        self._add_file(bundle, path, f"{PAYLOAD_PREFIX}/{relative}", now)
                raw.flush()
                os.fsync(raw.fileno())
            for relative, path in files.items():
                if self._fingerprint(path) != fingerprints[relative]:
                    raise RuntimeError("share changed while backup was being created")
            os.replace(partial, archive)
            _fsync_directory(paths.backups)
        finally:
            partial.unlink(missing_ok=True)
        archive_sha = self.sha256_file(archive)
        checksum = archive.with_name(archive.name + ".sha256")
        checksum_partial = checksum.with_name(checksum.name + ".partial")
        try:
            with checksum_partial.open("x", encoding="ascii") as stream:
                os.chmod(checksum_partial, 0o600)
                stream.write(f"{archive_sha}  {archive.name}\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(checksum_partial, checksum)
            _fsync_directory(paths.backups)
        finally:
            checksum_partial.unlink(missing_ok=True)
        return ShareBackup(share_id, archive, checksum, archive_sha, now)

    def list(self, *, actor_user_id: str, share_id: str) -> list[ShareBackupSummary]:
        self.repository.require_owner(actor_user_id=actor_user_id, share_id=share_id)
        root = self.storage.share_paths(share_id).backups
        if not root.exists():
            return []
        prefix = f"share-{share_id}-"
        results: list[ShareBackupSummary] = []
        for archive in sorted(root.glob(f"{prefix}*.tar.gz"), reverse=True):
            if archive.is_symlink() or not archive.is_file():
                continue
            details = archive.stat()
            results.append(ShareBackupSummary(
                name=archive.name, size=details.st_size,
                created_at=datetime.fromtimestamp(details.st_mtime, timezone.utc),
            ))
        return results

    def resolve_archive(
        self, *, actor_user_id: str, share_id: str, name: str,
    ) -> Path:
        self.repository.require_owner(actor_user_id=actor_user_id, share_id=share_id)
        if not name or name != Path(name).name or not name.startswith(f"share-{share_id}-"):
            raise ValueError("invalid backup name")
        archive = self.storage.share_paths(share_id).backups / name
        if archive.is_symlink() or not archive.is_file():
            raise FileNotFoundError("share backup not found")
        return archive

    def verify(self, archive: Path) -> VerifiedShareBackup:
        archive = archive.resolve()
        expected = self._expected_archive_sha(archive)
        actual = self.sha256_file(archive)
        if actual != expected:
            raise BackupIntegrityError("archive checksum does not match sidecar")
        scratch = Path(tempfile.mkdtemp(prefix=".verify-", dir=archive.parent))
        os.chmod(scratch, 0o700)
        try:
            manifest = self._extract_verified(archive, scratch)
            self._validate_payload(scratch, manifest)
        finally:
            shutil.rmtree(scratch)
        return self._verified(archive, actual, manifest)

    def restore(self, *, actor_user_id: str, share_id: str, archive: Path) -> None:
        self.repository.require_owner(actor_user_id=actor_user_id, share_id=share_id)
        archive = archive.resolve()
        expected = self._expected_archive_sha(archive)
        if self.sha256_file(archive) != expected:
            raise BackupIntegrityError("archive checksum does not match sidecar")
        paths = self.storage.share_paths(share_id)
        self.storage.prepare()
        stage = self.storage.shares_root / f".{share_id}.restore-stage-{uuid.uuid4().hex}"
        quarantine = self.storage.shares_root / f".{share_id}.restore-old-{uuid.uuid4().hex}"
        failed = self.storage.shares_root / f".{share_id}.restore-failed-{uuid.uuid4().hex}"
        stage.mkdir(mode=0o700)
        swapped = False
        try:
            manifest = self._extract_verified(archive, stage)
            if manifest["share_id"] != share_id:
                raise BackupIntegrityError("backup belongs to a different share")
            self._validate_payload(stage, manifest)
            # Re-authorize at the writer guard immediately before activation.
            self.repository.require_owner(
                actor_user_id=actor_user_id, share_id=share_id,
            )
            if paths.root.exists():
                os.replace(paths.root, quarantine)
            os.replace(stage, paths.root)
            _fsync_directory(self.storage.shares_root)
            swapped = True
            try:
                self.repository.restore_share_comments_from_backup(
                    actor_user_id=actor_user_id, share_id=share_id,
                    comments=list(manifest["comments"]), backup_sha256=expected,
                    now=self._clock(),
                )
            except BaseException:
                os.replace(paths.root, failed)
                if quarantine.exists():
                    os.replace(quarantine, paths.root)
                _fsync_directory(self.storage.shares_root)
                raise
        finally:
            for temporary in (stage, failed):
                if temporary.exists():
                    shutil.rmtree(temporary)
            if swapped and quarantine.exists():
                shutil.rmtree(quarantine)
            _fsync_directory(self.storage.shares_root)

    @staticmethod
    def _comment_payload(comment) -> dict:
        def epoch(value):
            return int(value.timestamp()) if value is not None else None

        return {
            "id": comment.id,
            "entity_type": comment.entity_type,
            "entity_id": comment.entity_id,
            "author_user_id": comment.author_user_id,
            "body": comment.body,
            "resolved_at": epoch(comment.resolved_at),
            "created_at": epoch(comment.created_at),
            "updated_at": epoch(comment.updated_at),
        }

    @staticmethod
    def _fingerprint(path: Path) -> tuple[int, int, int, int]:
        details = path.stat(follow_symlinks=False)
        return details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns

    def _source_files(self, root: Path) -> dict[str, Path]:
        if root.is_symlink() or not root.is_dir():
            raise BackupIntegrityError("share root must be a regular directory")
        files: dict[str, Path] = {}
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise BackupIntegrityError("links are not allowed in share storage")
            details = path.stat(follow_symlinks=False)
            if stat.S_ISDIR(details.st_mode):
                continue
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise BackupIntegrityError("share files must be regular and unlinked")
            relative = path.relative_to(root).as_posix()
            self._safe_relative(relative)
            files[relative] = path
        return files

    @staticmethod
    def _add_bytes(
        archive: tarfile.TarFile, name: str, payload: bytes, when: datetime,
    ) -> None:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        info.mtime = int(when.timestamp())
        info.mode = 0o600
        archive.addfile(info, io.BytesIO(payload))

    @staticmethod
    def _add_file(
        archive: tarfile.TarFile, path: Path, name: str, when: datetime,
    ) -> None:
        info = tarfile.TarInfo(name)
        info.size = path.stat().st_size
        info.mtime = int(when.timestamp())
        info.mode = 0o600
        with path.open("rb") as stream:
            archive.addfile(info, stream)

    def _expected_archive_sha(self, archive: Path) -> str:
        checksum = archive.with_name(archive.name + ".sha256")
        try:
            parts = checksum.read_text(encoding="ascii").split()
        except OSError as exc:
            raise BackupIntegrityError("archive checksum sidecar is missing") from exc
        if len(parts) != 2 or parts[1] != archive.name or len(parts[0]) != 64:
            raise BackupIntegrityError("archive checksum sidecar is invalid")
        try:
            int(parts[0], 16)
        except ValueError as exc:
            raise BackupIntegrityError("archive checksum sidecar is invalid") from exc
        return parts[0]

    def _extract_verified(self, archive: Path, destination: Path) -> dict:
        manifest: dict | None = None
        seen: set[str] = set()
        extracted: set[str] = set()
        try:
            with tarfile.open(archive, "r:gz") as bundle:
                members = bundle.getmembers()
                if len(members) > 100_000:
                    raise BackupIntegrityError("backup contains too many files")
                for member in members:
                    if not member.isfile():
                        raise BackupIntegrityError("backup may contain regular files only")
                    if member.name in seen:
                        raise BackupIntegrityError("backup contains duplicate paths")
                    seen.add(member.name)
                    if member.name == MANIFEST_NAME:
                        if member.size > 16 * 1024 * 1024:
                            raise BackupIntegrityError("backup manifest is too large")
                        manifest = json.load(self._member_stream(bundle, member))
                        continue
                    prefix = f"{PAYLOAD_PREFIX}/"
                    if not member.name.startswith(prefix):
                        raise BackupIntegrityError("backup contains an unexpected path")
                    relative = member.name[len(prefix):]
                    self._safe_relative(relative)
                    target = destination / Path(*PurePosixPath(relative).parts)
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    os.chmod(target.parent, 0o700)
                    with target.open("xb") as output, self._member_stream(bundle, member) as source:
                        os.chmod(target, 0o600)
                        shutil.copyfileobj(source, output, length=1 << 20)
                        output.flush()
                        os.fsync(output.fileno())
                    extracted.add(relative)
        except (tarfile.TarError, OSError, EOFError, UnicodeError, json.JSONDecodeError) as exc:
            if isinstance(exc, BackupIntegrityError):
                raise
            raise BackupIntegrityError("backup archive is unreadable") from exc
        if not isinstance(manifest, dict):
            raise BackupIntegrityError("backup manifest is missing")
        files = manifest.get("files")
        if manifest.get("schema") != SCHEMA_VERSION or not isinstance(files, dict):
            raise BackupIntegrityError("unsupported backup manifest")
        if set(files) != extracted:
            raise BackupIntegrityError("backup manifest file list does not match payload")
        for relative, expected in files.items():
            self._safe_relative(relative)
            if not isinstance(expected, dict):
                raise BackupIntegrityError("backup file metadata is invalid")
            target = destination / Path(*PurePosixPath(relative).parts)
            if target.stat().st_size != expected.get("size"):
                raise BackupIntegrityError("backup file size does not match manifest")
            if self.sha256_file(target) != expected.get("sha256"):
                raise BackupIntegrityError("backup file checksum does not match manifest")
        if not isinstance(manifest.get("comments"), list):
            raise BackupIntegrityError("backup comments metadata is invalid")
        return manifest

    @staticmethod
    def _member_stream(bundle: tarfile.TarFile, member: tarfile.TarInfo) -> BinaryIO:
        stream = bundle.extractfile(member)
        if stream is None:
            raise BackupIntegrityError("backup member cannot be read")
        return stream

    def _validate_payload(self, root: Path, manifest: dict) -> None:
        collection = root / "anki" / "collection.anki2"
        if not collection.is_file():
            raise BackupIntegrityError("backup workspace collection is missing")
        try:
            WorkspaceProvisioner._validate_workspace(collection)
        except Exception as exc:
            raise BackupIntegrityError("backup workspace collection is invalid")
        releases = root / "releases"
        if releases.exists():
            for release_root in releases.iterdir():
                if not release_root.is_dir() or not release_root.name.isdigit():
                    raise BackupIntegrityError("backup release path is invalid")
                manifest_path = release_root / "manifest.json"
                bundle_path = release_root / "deck.apkg"
                try:
                    release_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise BackupIntegrityError("backup release manifest is invalid") from exc
                if (
                    release_manifest.get("share_id") != manifest.get("share_id")
                    or release_manifest.get("version") != int(release_root.name)
                    or self.sha256_file(bundle_path) != release_manifest.get("bundle_sha256")
                ):
                    raise BackupIntegrityError("backup release checksum is invalid")

    @staticmethod
    def _safe_relative(value: str) -> None:
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts or "." in path.parts:
            raise BackupIntegrityError("backup path is unsafe")
        if "\\" in value or "\x00" in value:
            raise BackupIntegrityError("backup path is unsafe")

    @staticmethod
    def _verified(archive: Path, digest: str, manifest: dict) -> VerifiedShareBackup:
        try:
            created_at = datetime.fromisoformat(manifest["created_at"])
            share_id = str(uuid.UUID(manifest["share_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise BackupIntegrityError("backup identity metadata is invalid") from exc
        release_count = len({
            PurePosixPath(name).parts[1]
            for name in manifest["files"]
            if len(PurePosixPath(name).parts) > 2
            and PurePosixPath(name).parts[0] == "releases"
            and PurePosixPath(name).parts[1].isdigit()
        })
        return VerifiedShareBackup(
            share_id=share_id, archive=archive, sha256=digest,
            created_at=created_at, file_count=len(manifest["files"]),
            release_count=release_count, comment_count=len(manifest["comments"]),
        )
