"""Crash-resumable migration from the legacy collection into ``UserStorage``.

The source is read-only.  All writes occur in a sibling staging directory on the
target filesystem, and a durable journal makes the two directory renames recoverable.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tarfile
from datetime import datetime, timezone
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .storage import StorageLayout, UserPaths


class TenantMigrationError(RuntimeError):
    pass


class UnsafeMigrationPath(TenantMigrationError):
    pass


class CollectionIntegrityError(TenantMigrationError):
    pass


class MigrationPreflightError(TenantMigrationError):
    pass


class AcceptanceGateError(TenantMigrationError):
    pass


class MigrationPhase(IntEnum):
    INITIALIZED = 1
    STAGED = 2
    VERIFIED = 3
    SYNCED = 4
    OLD_QUARANTINED = 5
    ACTIVATED = 6
    COMPLETE = 7


FaultInjector = Callable[[MigrationPhase], None]


@dataclass(frozen=True, slots=True)
class MigrationResult:
    user_id: UUID
    paths: UserPaths
    journal: Path
    quarantine: Path
    phase: MigrationPhase


class LegacyCollectionMigration:
    """Copy one stopped legacy collection into a canonical UUID user root.

    ``fault_injector`` is intentionally called only after a phase and its journal
    record are durable. Tests and operators can therefore prove restart behavior at
    every externally observable boundary without creating an unjournaled test state.
    """

    def __init__(
        self,
        *,
        storage: StorageLayout,
        user_id: UUID | str,
        source_collection: Path,
        source_media: Path,
        offline_marker: Path | None = None,
        backup_evidence: Path | None = None,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self.storage = storage
        # user_paths performs strict UUID parsing; request/path fragments never form paths.
        self.paths = storage.user_paths(user_id)
        self.user_id = UUID(self.paths.root.name)
        self.source_collection = Path(source_collection).absolute()
        self.source_media = Path(source_media).absolute()
        self.offline_marker = Path(offline_marker).absolute() if offline_marker else \
            self.source_collection.with_name(f"{self.source_collection.name}.offline.json")
        self.backup_evidence = Path(backup_evidence).absolute() if backup_evidence else \
            self.source_collection.with_name(f"{self.source_collection.name}.verified-backup.json")
        token = str(self.user_id)
        self.stage = storage.users_root / f".{token}.migration-stage"
        self.quarantine = storage.users_root / f".{token}.migration-quarantine"
        self.journal = storage.users_root / f".{token}.migration.json"
        self.lock = storage.users_root / f".{token}.migration.lock"
        self.fault_injector = fault_injector

    def migrate(self) -> MigrationResult:
        self._prepare_parent()
        with self._exclusive_lock():
            self._validate_source_paths()
            with self._offline_source_lock():
                return self._migrate_locked()

    def _migrate_locked(self) -> MigrationResult:
        record = self._load_or_initialize()
        phase = MigrationPhase(record["phase"])

        if phase is MigrationPhase.COMPLETE:
            self._verify_active()
            self._secure_and_sync_tree(self.paths.root)
            if self.quarantine.exists() or self.quarantine.is_symlink():
                self._secure_and_sync_tree(self.quarantine)
            return self._result(phase)

        if self._source_digest() != record["source_digest"]:
            raise TenantMigrationError("legacy source changed after migration began")
        self._validate_preflight_evidence(record)

        if phase is MigrationPhase.INITIALIZED:
            self._discard_stage()
            self._copy_stage()
            self._assert_stage_matches_source(record["source_digest"])
            phase = self._advance(record, MigrationPhase.STAGED)

        if phase is MigrationPhase.STAGED:
            self._assert_stage_matches_source(record["source_digest"])
            self._verify_sqlite(self._stage_collection)
            self._run_acceptance(record)
            phase = self._advance(record, MigrationPhase.VERIFIED)

        if phase is MigrationPhase.VERIFIED:
            # Reverify on resume; a durable phase never makes an unattended stage trusted.
            self._verify_sqlite(self._stage_collection)
            self._run_acceptance(record)
            self._secure_and_sync_tree(self.stage)
            phase = self._advance(record, MigrationPhase.SYNCED)

        if phase is MigrationPhase.SYNCED:
            self._verify_sqlite(self._stage_collection)
            self._run_acceptance(record)
            if self._source_digest() != record["source_digest"]:
                raise TenantMigrationError("legacy source changed before activation")
            self._quarantine_old_target()
            phase = self._advance(record, MigrationPhase.OLD_QUARANTINED)

        if phase is MigrationPhase.OLD_QUARANTINED:
            self._validate_acceptance_evidence(record)
            self._activate_stage_or_recover_rename()
            phase = self._advance(record, MigrationPhase.ACTIVATED)

        if phase is MigrationPhase.ACTIVATED:
            self._validate_acceptance_evidence(record)
            self._verify_active()
            self._secure_and_sync_tree(self.paths.root)
            phase = self._advance(record, MigrationPhase.COMPLETE)

        return self._result(phase)

    recover = migrate

    def prepare_evidence(self, backup_archive: Path) -> tuple[Path, Path]:
        """Verify an exact backup and attest that the source is stopped.

        This is an explicit operator step run while the legacy server is down. The
        source is exclusively locked for the whole restore drill, and the backup's
        manifest must match every collection/media source file byte-for-byte.
        """
        from ankiweb.adapters.anki import backup

        self._prepare_parent()
        self._validate_source_paths()
        archive = Path(backup_archive).absolute()
        self._assert_no_symlink_components(archive)
        self._assert_regular_single_link(archive)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.source_collection, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnsafeMigrationPath("legacy collection is not a single-link regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MigrationPreflightError(
                    "legacy source is locked; stop the legacy app before preparing migration"
                ) from exc
            source_digest = self._source_digest()
            checks = backup.restore_drill(archive)
            by_id = {check.id: check for check in checks}
            required = ("R1", "R2", "R3", "M10")
            failed = [cid for cid in required if cid not in by_id or by_id[cid].status != "pass"]
            if failed:
                raise MigrationPreflightError(
                    f"backup restore drill did not pass: {', '.join(failed)}"
                )
            try:
                with tarfile.open(archive) as bundle:
                    member = bundle.extractfile("manifest.json")
                    if member is None:
                        raise KeyError("manifest.json")
                    manifest = json.load(member)
            except (OSError, tarfile.TarError, KeyError, ValueError, TypeError) as exc:
                raise MigrationPreflightError("backup manifest is unreadable") from exc
            hashes = manifest.get("sha256")
            if not isinstance(hashes, dict):
                raise MigrationPreflightError("backup manifest lacks file hashes")
            mismatches = [
                relative for relative, path in self._source_files()
                if hashes.get(relative) != self._sha256_file(path)
            ]
            if mismatches:
                raise MigrationPreflightError(
                    "verified backup is not an exact copy of the migration source: "
                    + ", ".join(mismatches[:8])
                )
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._write_json_evidence(self.offline_marker, {
                "version": 1,
                "source_collection": str(self.source_collection),
                "source_digest": source_digest,
                "app_stopped": True,
                "created_at": now,
            })
            self._write_json_evidence(self.backup_evidence, {
                "version": 1,
                "source_digest": source_digest,
                "archive": str(archive),
                "archive_sha256": self._sha256_file(archive),
                "checks": {cid: "pass" for cid in required},
                "verified_at": now,
            })
        finally:
            os.close(fd)
        return self.offline_marker, self.backup_evidence

    @property
    def _stage_collection(self) -> Path:
        return self.stage / "anki" / "collection.anki2"

    @property
    def _stage_media(self) -> Path:
        return self.stage / "anki" / "collection.media"

    def _result(self, phase: MigrationPhase) -> MigrationResult:
        return MigrationResult(
            user_id=self.user_id, paths=self.paths, journal=self.journal,
            quarantine=self.quarantine, phase=phase,
        )

    def _prepare_parent(self) -> None:
        self.storage.prepare()
        self.storage._assert_no_symlink(self.storage.users_root)
        os.chmod(self.storage.users_root, 0o700)

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.lock, flags, 0o600)
        except OSError as exc:
            raise UnsafeMigrationPath(f"unsafe migration lock: {self.lock}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnsafeMigrationPath("migration lock is not a regular file")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @contextmanager
    def _offline_source_lock(self) -> Iterator[None]:
        """Hold an advisory lock and require a digest-bound operator stop attestation.

        The lock prevents two cooperating processes from using the legacy collection during
        migration.  The marker makes the non-cooperating case explicit: it must have been
        written after the legacy app stopped and names the exact source digest being migrated.
        """
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.source_collection, flags)
        except OSError as exc:
            raise MigrationPreflightError("cannot lock the legacy source collection") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnsafeMigrationPath("legacy collection is not a single-link regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MigrationPreflightError(
                    "legacy source is locked; stop the legacy app before migrating"
                ) from exc
            self._read_offline_attestation(self._source_digest())
            yield
        finally:
            os.close(fd)

    def _load_or_initialize(self) -> dict[str, Any]:
        if self.journal.exists() or self.journal.is_symlink():
            self._assert_regular_single_link(self.journal)
            try:
                record = json.loads(self.journal.read_text(encoding="utf-8"))
                phase = MigrationPhase(record["phase"])
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise TenantMigrationError("invalid migration journal") from exc
            expected = {
                "version": 1,
                "user_id": str(self.user_id),
                "source_collection": str(self.source_collection),
                "source_media": str(self.source_media),
            }
            if any(record.get(key) != value for key, value in expected.items()):
                raise TenantMigrationError("migration journal does not match this request")
            record["phase"] = int(phase)
            return record

        if self.stage.exists() or self.stage.is_symlink() or self.quarantine.exists() \
                or self.quarantine.is_symlink():
            raise TenantMigrationError("unowned migration stage or quarantine exists")
        source_digest = self._source_digest()
        record = {
            "version": 1,
            "user_id": str(self.user_id),
            "source_collection": str(self.source_collection),
            "source_media": str(self.source_media),
            "source_digest": source_digest,
            "preflight": self._build_preflight(source_digest),
            "phase": int(MigrationPhase.INITIALIZED),
        }
        self._write_journal(record)
        self._inject(MigrationPhase.INITIALIZED)
        return record

    def _validate_acceptance_evidence(self, record: dict[str, Any]) -> None:
        evidence = record.get("acceptance")
        if not isinstance(evidence, dict):
            raise AcceptanceGateError("migration journal lacks M1-M9 acceptance evidence")
        baseline = evidence.get("baseline")
        results = evidence.get("results")
        if not isinstance(baseline, dict) or not isinstance(results, list):
            raise AcceptanceGateError("migration journal has invalid acceptance evidence")
        baseline_payload = json.dumps(
            baseline, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        result_payload = json.dumps(
            results, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        if evidence.get("baseline_sha256") != hashlib.sha256(baseline_payload).hexdigest() \
                or evidence.get("results_sha256") != hashlib.sha256(result_payload).hexdigest():
            raise AcceptanceGateError("migration acceptance evidence digest is invalid")
        if evidence.get("source_digest") != record.get("source_digest") \
                or evidence.get("stage_digest") != record.get("source_digest"):
            raise AcceptanceGateError("migration acceptance evidence is for another source")
        expected_ids = {f"M{number}" for number in range(1, 10)}
        try:
            found_ids = {result["id"] for result in results}
            failed = [result["id"] for result in results if result["status"] == "fail"]
        except (KeyError, TypeError) as exc:
            raise AcceptanceGateError("migration acceptance results are malformed") from exc
        if found_ids != expected_ids or len(results) != len(expected_ids) or failed:
            raise AcceptanceGateError("migration acceptance evidence does not pass M1-M9")

    def _build_preflight(self, source_digest: str) -> dict[str, Any]:
        offline = self._read_offline_attestation(source_digest)
        backup = self._read_backup_evidence(source_digest)
        source_bytes = sum(path.stat().st_size for _, path in self._source_files())
        required_bytes = source_bytes * 2
        usage = shutil.disk_usage(self.storage.users_root)
        if usage.free < required_bytes:
            raise MigrationPreflightError(
                f"insufficient free space: need at least {required_bytes} bytes, "
                f"found {usage.free}"
            )
        return {
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_lock": "exclusive-flock",
            "offline": offline,
            "backup": backup,
            "source_bytes": source_bytes,
            "required_free_bytes": required_bytes,
            "observed_free_bytes": usage.free,
            "target_filesystem_device": self.storage.users_root.stat().st_dev,
        }

    def _validate_preflight_evidence(self, record: dict[str, Any]) -> None:
        preflight = record.get("preflight")
        if not isinstance(preflight, dict):
            raise MigrationPreflightError("migration journal lacks durable preflight evidence")
        source_digest = record["source_digest"]
        offline = self._read_offline_attestation(source_digest)
        backup = self._read_backup_evidence(source_digest)
        if preflight.get("offline") != offline:
            raise MigrationPreflightError("offline attestation changed after migration began")
        if preflight.get("backup") != backup:
            raise MigrationPreflightError("verified backup evidence changed after migration began")
        required = preflight.get("required_free_bytes")
        observed = preflight.get("observed_free_bytes")
        if not isinstance(required, int) or not isinstance(observed, int) or observed < required:
            raise MigrationPreflightError("migration journal contains invalid free-space evidence")
        if preflight.get("target_filesystem_device") != self.storage.users_root.stat().st_dev:
            raise MigrationPreflightError("migration target filesystem changed after preflight")

    def _read_offline_attestation(self, source_digest: str) -> dict[str, Any]:
        data, evidence_digest = self._read_json_evidence(self.offline_marker, "offline attestation")
        expected = {
            "version": 1,
            "source_collection": str(self.source_collection),
            "source_digest": source_digest,
            "app_stopped": True,
        }
        if any(data.get(key) != value for key, value in expected.items()):
            raise MigrationPreflightError(
                "offline attestation does not match the stopped legacy source"
            )
        created_at = data.get("created_at")
        if not isinstance(created_at, str) or not created_at:
            raise MigrationPreflightError("offline attestation lacks created_at")
        return {
            "path": str(self.offline_marker),
            "sha256": evidence_digest,
            "created_at": created_at,
            "source_digest": source_digest,
        }

    def _read_backup_evidence(self, source_digest: str) -> dict[str, Any]:
        data, evidence_digest = self._read_json_evidence(
            self.backup_evidence, "verified backup evidence"
        )
        if data.get("version") != 1 or data.get("source_digest") != source_digest:
            raise MigrationPreflightError("backup evidence does not match the legacy source")
        checks = data.get("checks")
        required_checks = ("R1", "R2", "R3", "M10")
        if not isinstance(checks, dict) or any(checks.get(check) != "pass" for check in required_checks):
            raise MigrationPreflightError("backup evidence has not passed R1-R3 and M10")
        archive_value = data.get("archive")
        archive_sha256 = data.get("archive_sha256")
        if not isinstance(archive_value, str) or not isinstance(archive_sha256, str):
            raise MigrationPreflightError("backup evidence lacks archive identity")
        archive = Path(archive_value)
        if not archive.is_absolute():
            archive = self.backup_evidence.parent / archive
        archive = archive.absolute()
        self._assert_no_symlink_components(archive)
        self._assert_regular_single_link(archive)
        if self._sha256_file(archive) != archive_sha256:
            raise MigrationPreflightError("verified backup archive digest does not match evidence")
        return {
            "path": str(self.backup_evidence),
            "sha256": evidence_digest,
            "archive": str(archive),
            "archive_sha256": archive_sha256,
            "checks": {check: "pass" for check in required_checks},
        }

    def _read_json_evidence(self, path: Path, label: str) -> tuple[dict[str, Any], str]:
        self._assert_no_symlink_components(path)
        try:
            self._assert_regular_single_link(path)
            payload = path.read_bytes()
            data = json.loads(payload)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise MigrationPreflightError(f"invalid {label}: {path}") from exc
        if not isinstance(data, dict):
            raise MigrationPreflightError(f"invalid {label}: expected a JSON object")
        return data, hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _advance(self, record: dict[str, Any], phase: MigrationPhase) -> MigrationPhase:
        record["phase"] = int(phase)
        self._write_journal(record)
        self._inject(phase)
        return phase

    def _inject(self, phase: MigrationPhase) -> None:
        if self.fault_injector is not None:
            self.fault_injector(phase)

    def _write_journal(self, record: dict[str, Any]) -> None:
        self._write_json_evidence(self.journal, record)

    def _write_json_evidence(self, destination: Path, record: dict[str, Any]) -> None:
        temporary = destination.with_name(f"{destination.name}.{uuid4().hex}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temporary, flags, 0o600)
        try:
            payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, destination)
        self._fsync_directory(destination.parent)

    def _validate_source_paths(self) -> None:
        self._assert_no_symlink_components(self.source_collection)
        self._assert_regular_single_link(self.source_collection)
        if self.source_media.exists() or self.source_media.is_symlink():
            self._assert_no_symlink_components(self.source_media)
            if not self.source_media.is_dir():
                raise UnsafeMigrationPath("source media must be a directory")
            self._walk_regular_files(self.source_media)
        for source in (self.source_collection, self.source_media):
            try:
                source.resolve().relative_to(self.paths.root.resolve())
            except ValueError:
                pass
            else:
                raise UnsafeMigrationPath("legacy source overlaps target user storage")

    def _source_files(self) -> list[tuple[str, Path]]:
        files: list[tuple[str, Path]] = [("anki/collection.anki2", self.source_collection)]
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.source_collection}{suffix}")
            if sidecar.exists() or sidecar.is_symlink():
                self._assert_no_symlink_components(sidecar)
                self._assert_regular_single_link(sidecar)
                files.append((f"anki/collection.anki2{suffix}", sidecar))
        if self.source_media.exists():
            for path in self._walk_regular_files(self.source_media):
                relative = path.relative_to(self.source_media).as_posix()
                files.append((f"anki/collection.media/{relative}", path))
        return sorted(files)

    def _source_digest(self) -> str:
        digest = hashlib.sha256()
        for relative, path in self._source_files():
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            with self._open_source(path) as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            digest.update(b"\0")
        return digest.hexdigest()

    def _copy_stage(self) -> None:
        self._mkdir(self.stage)
        self._mkdir(self.stage / "anki")
        self._mkdir(self._stage_media)
        self._mkdir(self.stage / "tmp")
        self._mkdir(self.stage / "app")
        for relative, source in self._source_files():
            target = self.stage / relative
            self._mkdir(target.parent)
            self._copy_file(source, target)
        self._fsync_tree(self.stage)

    def _copy_file(self, source: Path, target: Path) -> None:
        read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(source, read_flags)
        try:
            source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1:
                raise UnsafeMigrationPath(f"source must be a single-link regular file: {source}")
            target_fd = os.open(target, write_flags, 0o600)
            try:
                while chunk := os.read(source_fd, 1024 * 1024):
                    view = memoryview(chunk)
                    while view:
                        written = os.write(target_fd, view)
                        view = view[written:]
                os.fchmod(target_fd, 0o600)
                os.fsync(target_fd)
            finally:
                os.close(target_fd)
        finally:
            os.close(source_fd)

    def _assert_stage_matches_source(self, expected_source_digest: str) -> None:
        if not self.stage.is_dir() or self.stage.is_symlink():
            raise TenantMigrationError("migration stage is missing or unsafe")
        if self._source_digest() != expected_source_digest:
            raise TenantMigrationError("legacy source changed during copy")
        digest = hashlib.sha256()
        source_relatives = {relative for relative, _ in self._source_files()}
        stage_files = self._walk_regular_files(self.stage)
        actual_relatives = {path.relative_to(self.stage).as_posix() for path in stage_files}
        if actual_relatives != source_relatives:
            raise TenantMigrationError("staged file set does not match source")
        for relative in sorted(source_relatives):
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            with self._open_source(self.stage / relative) as staged:
                while chunk := staged.read(1024 * 1024):
                    digest.update(chunk)
            digest.update(b"\0")
        if digest.hexdigest() != expected_source_digest:
            raise TenantMigrationError("staged file content does not match source")

    def _verify_sqlite(self, collection: Path) -> None:
        self._assert_regular_single_link(collection)
        try:
            conn = sqlite3.connect(collection)
            try:
                # Anki indexes use its custom Unicode case-insensitive collation. Register a
                # read-only equivalent so SQLite can traverse those indexes during integrity
                # checking without opening (and potentially upgrading) the collection in Anki.
                conn.create_collation(
                    "unicase",
                    lambda left, right: (left.casefold() > right.casefold())
                    - (left.casefold() < right.casefold()),
                )
                rows = conn.execute("PRAGMA integrity_check").fetchall()
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            raise CollectionIntegrityError("staged collection is not a valid SQLite database") from exc
        if rows != [("ok",)]:
            raise CollectionIntegrityError(f"SQLite integrity check failed: {rows[:3]}")

    def _run_acceptance(self, record: dict[str, Any]) -> None:
        """Run and journal M1-M9 before any target directory is renamed."""
        from ankiweb.adapters.anki import acceptance

        previous = record.get("acceptance")
        baseline = previous.get("baseline") if isinstance(previous, dict) else None
        try:
            if not isinstance(baseline, dict):
                baseline = acceptance.snapshot(self.source_collection)
            checks = acceptance.verify(self._stage_collection, baseline)
        except Exception as exc:
            raise AcceptanceGateError(
                f"M1-M9 acceptance could not run: {type(exc).__name__}: {exc}"
            ) from exc

        expected_ids = {f"M{number}" for number in range(1, 10)}
        selected = [check for check in checks if check.id in expected_ids]
        found_ids = {check.id for check in selected}
        if found_ids != expected_ids or len(selected) != len(expected_ids):
            missing = sorted(expected_ids - found_ids)
            raise AcceptanceGateError(
                f"M1-M9 acceptance returned an incomplete result; missing: {', '.join(missing)}"
            )
        results = [
            {
                "id": check.id,
                "name": check.name,
                "status": check.status,
                "detail": check.detail,
            }
            for check in sorted(selected, key=lambda check: int(check.id[1:]))
        ]
        failures = [result["id"] for result in results if result["status"] == "fail"]
        invalid = [
            result["id"] for result in results
            if result["status"] not in {"pass", "warn", "skip", "fail"}
        ]
        if failures or invalid:
            failed = failures + invalid
            raise AcceptanceGateError(f"migration acceptance failed: {', '.join(failed)}")

        baseline_payload = json.dumps(
            baseline, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        result_payload = json.dumps(
            results, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        record["acceptance"] = {
            "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_digest": record["source_digest"],
            "stage_digest": record["source_digest"],
            "baseline_sha256": hashlib.sha256(baseline_payload).hexdigest(),
            "results_sha256": hashlib.sha256(result_payload).hexdigest(),
            "baseline": baseline,
            "results": results,
        }
        # Acceptance evidence is durable independently of the next phase update. A crash
        # here simply reruns the checks; activation can never rely on an in-memory verdict.
        self._write_journal(record)

    def _quarantine_old_target(self) -> None:
        # Also covers a crash after target->quarantine but before the phase record.
        if self.quarantine.exists() or self.quarantine.is_symlink():
            if self.paths.root.exists() or self.paths.root.is_symlink():
                raise TenantMigrationError("both active target and migration quarantine exist")
            self._assert_safe_tree(self.quarantine)
            self._secure_and_sync_tree(self.quarantine)
            return
        if self.paths.root.exists() or self.paths.root.is_symlink():
            self._assert_safe_tree(self.paths.root)
            os.replace(self.paths.root, self.quarantine)
            self._secure_and_sync_tree(self.quarantine)
            self._fsync_directory(self.storage.users_root)

    def _activate_stage_or_recover_rename(self) -> None:
        # Covers a crash after stage->target but before ACTIVATED reached the journal.
        if self.paths.root.exists() or self.paths.root.is_symlink():
            if self.stage.exists() or self.stage.is_symlink():
                raise TenantMigrationError("both active target and migration stage exist")
            self._verify_active()
            return
        self._assert_safe_tree(self.stage)
        self._verify_sqlite(self._stage_collection)
        os.replace(self.stage, self.paths.root)
        self._fsync_directory(self.storage.users_root)

    def _verify_active(self) -> None:
        self._assert_safe_tree(self.paths.root)
        self._verify_sqlite(self.paths.collection)

    def _discard_stage(self) -> None:
        if self.stage.is_symlink():
            raise UnsafeMigrationPath("migration stage may not be a symlink")
        if self.stage.exists():
            shutil.rmtree(self.stage)
            self._fsync_directory(self.storage.users_root)

    def _secure_and_sync_tree(self, root: Path) -> None:
        self._assert_safe_tree(root)
        for path in self._walk_regular_files(root):
            os.chmod(path, 0o600, follow_symlinks=False)
        for directory in sorted(
            (path for path in root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts), reverse=True,
        ):
            os.chmod(directory, 0o700, follow_symlinks=False)
        os.chmod(root, 0o700, follow_symlinks=False)
        self._fsync_tree(root)

    def _fsync_tree(self, root: Path) -> None:
        for path in self._walk_regular_files(root):
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
        for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
            self._fsync_directory(directory)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _mkdir(path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise UnsafeMigrationPath(f"migration directory is unsafe: {path}")
        os.chmod(path, 0o700, follow_symlinks=False)

    def _assert_safe_tree(self, root: Path) -> None:
        if root.is_symlink() or not root.is_dir():
            raise UnsafeMigrationPath(f"unsafe migration tree: {root}")
        self._walk_regular_files(root)

    def _walk_regular_files(self, root: Path) -> list[Path]:
        files: list[Path] = []
        for current, directories, names in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in directories:
                path = current_path / name
                if path.is_symlink():
                    raise UnsafeMigrationPath(f"symlink is not allowed: {path}")
            for name in names:
                path = current_path / name
                self._assert_regular_single_link(path)
                files.append(path)
        return sorted(files)

    @staticmethod
    def _assert_regular_single_link(path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise UnsafeMigrationPath(f"required file is missing: {path}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise UnsafeMigrationPath(f"symlink is not allowed: {path}")
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeMigrationPath(f"non-regular file is not allowed: {path}")
        if info.st_nlink != 1:
            raise UnsafeMigrationPath(f"hardlinked file is not allowed: {path}")

    @staticmethod
    def _assert_no_symlink_components(path: Path) -> None:
        current = path
        while True:
            if current.is_symlink():
                raise UnsafeMigrationPath(f"symlink path component is not allowed: {current}")
            if current.parent == current:
                return
            current = current.parent

    @staticmethod
    @contextmanager
    def _open_source(path: Path):
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnsafeMigrationPath(f"unsafe source file: {path}")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                yield stream
        finally:
            os.close(fd)
