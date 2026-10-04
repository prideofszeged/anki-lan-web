from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import fcntl
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ankiweb.tenancy.migration import (
    AcceptanceGateError, CollectionIntegrityError, LegacyCollectionMigration,
    MigrationPhase, MigrationPreflightError, TenantMigrationError, UnsafeMigrationPath,
)
from ankiweb.tenancy.storage import StorageLayout
from ankiweb.adapters.anki.acceptance import Check


class InjectedCrash(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def acceptance_adapter_stub(monkeypatch):
    """The migration contract is tested here; the real Anki adapter has its own suite."""
    from ankiweb.adapters.anki import acceptance
    original = (acceptance.snapshot, acceptance.verify)

    def snapshot(collection):
        return {"fixture": True, "source_sha256": _digest(collection)}

    def verify(collection, baseline):
        assert baseline["fixture"] is True
        assert collection.name == "collection.anki2"
        return [
            Check(f"M{number}", f"gate {number}", "pass", "fixture passed")
            for number in range(1, 10)
        ] + [Check("M10", "restore", "skip", "not a restore")]

    monkeypatch.setattr(acceptance, "snapshot", snapshot)
    monkeypatch.setattr(acceptance, "verify", verify)
    return original


def _source(root: Path) -> tuple[Path, Path]:
    root.mkdir()
    collection = root / "collection.anki2"
    with sqlite3.connect(collection) as conn:
        conn.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, text TEXT NOT NULL)")
        conn.execute("INSERT INTO notes(text) VALUES('γειά σου')")
    media = root / "collection.media"
    media.mkdir()
    (media / "lesson.mp3").write_bytes(b"ID3\x00audio")
    nested = media / "images"
    nested.mkdir()
    (nested / "alpha.png").write_bytes(b"PNG\x00image")
    return collection, media


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_safety_evidence(migration: LegacyCollectionMigration) -> None:
    source_digest = migration._source_digest()
    marker = {
        "version": 1,
        "source_collection": str(migration.source_collection),
        "source_digest": source_digest,
        "app_stopped": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    migration.offline_marker.write_text(json.dumps(marker), encoding="utf-8")
    migration.offline_marker.chmod(0o600)

    archive = migration.source_collection.with_name("verified-backup.tar.gz")
    archive.write_bytes(b"verified backup fixture")
    archive.chmod(0o600)
    evidence = {
        "version": 1,
        "source_digest": source_digest,
        "archive": str(archive),
        "archive_sha256": _digest(archive),
        "checks": {"R1": "pass", "R2": "pass", "R3": "pass", "M10": "pass"},
    }
    migration.backup_evidence.write_text(json.dumps(evidence), encoding="utf-8")
    migration.backup_evidence.chmod(0o600)


def _migration(tmp_path: Path, *, phase=None):
    collection, media = _source(tmp_path / "legacy")
    layout = StorageLayout(tmp_path / "data")
    user_id = uuid4()
    target = layout.prepare_user(user_id)
    (target.app / "old-state").write_text("old", encoding="utf-8")

    def inject(current):
        if current is phase:
            raise InjectedCrash(current.name)

    migration = LegacyCollectionMigration(
        storage=layout, user_id=user_id, source_collection=collection,
        source_media=media, fault_injector=inject if phase else None,
    )
    _write_safety_evidence(migration)
    return migration, collection, media


def test_migration_preserves_source_and_atomically_quarantines_old_target(tmp_path):
    migration, source_collection, source_media = _migration(tmp_path)
    source_hash = _digest(source_collection)
    source_media_hash = _digest(source_media / "lesson.mp3")
    source_mtime = source_collection.stat().st_mtime_ns

    result = migration.migrate()

    assert result.phase is MigrationPhase.COMPLETE
    assert result.paths.root.name == str(result.user_id)
    assert result.paths.collection.exists()
    with sqlite3.connect(result.paths.collection) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("SELECT text FROM notes").fetchone() == ("γειά σου",)
    assert (result.paths.media / "lesson.mp3").read_bytes() == b"ID3\x00audio"
    assert (result.paths.media / "images/alpha.png").read_bytes() == b"PNG\x00image"
    assert (result.quarantine / "app/old-state").read_text() == "old"

    assert _digest(source_collection) == source_hash
    assert _digest(source_media / "lesson.mp3") == source_media_hash
    assert source_collection.stat().st_mtime_ns == source_mtime
    assert result.paths.collection.stat().st_ino != source_collection.stat().st_ino
    assert result.paths.collection.stat().st_nlink == 1

    for directory in [result.paths.root, *(
        path for path in result.paths.root.rglob("*") if path.is_dir()
    )]:
        assert stat_mode(directory) == 0o700
    for file in (path for path in result.paths.root.rglob("*") if path.is_file()):
        assert stat_mode(file) == 0o600
    for directory in [result.quarantine, *(
        path for path in result.quarantine.rglob("*") if path.is_dir()
    )]:
        assert stat_mode(directory) == 0o700
    for file in (path for path in result.quarantine.rglob("*") if path.is_file()):
        assert stat_mode(file) == 0o600
    assert stat_mode(result.journal) == 0o600
    assert json.loads(result.journal.read_text())["phase"] == int(MigrationPhase.COMPLETE)


@pytest.mark.parametrize("phase", list(MigrationPhase))
def test_fault_after_every_durable_phase_is_resumable(tmp_path, phase):
    migration, source_collection, _ = _migration(tmp_path, phase=phase)
    source_hash = _digest(source_collection)

    with pytest.raises(InjectedCrash, match=phase.name):
        migration.migrate()

    # Before quarantine the old target remains authoritative; after activation only a
    # verified new target may be present. The quarantine phase is the journaled recovery gap.
    if phase <= MigrationPhase.SYNCED:
        assert (migration.paths.app / "old-state").read_text() == "old"
    elif phase is MigrationPhase.OLD_QUARANTINED:
        assert not migration.paths.root.exists()
        assert (migration.quarantine / "app/old-state").read_text() == "old"
    else:
        with sqlite3.connect(migration.paths.collection) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    recovered = LegacyCollectionMigration(
        storage=migration.storage, user_id=migration.user_id,
        source_collection=migration.source_collection, source_media=migration.source_media,
    ).recover()
    assert recovered.phase is MigrationPhase.COMPLETE
    with sqlite3.connect(recovered.paths.collection) as conn:
        assert conn.execute("SELECT text FROM notes").fetchone() == ("γειά σου",)
    assert (recovered.quarantine / "app/old-state").read_text() == "old"
    assert _digest(source_collection) == source_hash


@pytest.mark.parametrize("rename_phase", [
    MigrationPhase.OLD_QUARANTINED, MigrationPhase.ACTIVATED,
])
def test_recovery_handles_crash_after_rename_before_journal(tmp_path, rename_phase, monkeypatch):
    migration, _, _ = _migration(tmp_path)
    original = migration._advance

    def fail_before_journal(record, phase):
        if phase is rename_phase:
            raise InjectedCrash(phase.name)
        return original(record, phase)

    monkeypatch.setattr(migration, "_advance", fail_before_journal)
    with pytest.raises(InjectedCrash):
        migration.migrate()

    recovered = LegacyCollectionMigration(
        storage=migration.storage, user_id=migration.user_id,
        source_collection=migration.source_collection, source_media=migration.source_media,
    ).recover()
    assert recovered.phase is MigrationPhase.COMPLETE
    assert recovered.paths.collection.exists()
    assert (recovered.quarantine / "app/old-state").exists()


def test_corrupt_sqlite_never_replaces_old_target(tmp_path):
    migration, collection, _ = _migration(tmp_path)
    collection.write_bytes(b"not sqlite")
    _write_safety_evidence(migration)
    with pytest.raises(CollectionIntegrityError):
        migration.migrate()
    assert (migration.paths.app / "old-state").read_text() == "old"
    assert not migration.quarantine.exists()


def test_source_change_during_resumption_is_rejected(tmp_path):
    migration, collection, _ = _migration(tmp_path, phase=MigrationPhase.INITIALIZED)
    with pytest.raises(InjectedCrash):
        migration.migrate()
    collection.write_bytes(collection.read_bytes() + b"changed")
    resumed = LegacyCollectionMigration(
        storage=migration.storage, user_id=migration.user_id,
        source_collection=collection, source_media=migration.source_media,
    )
    with pytest.raises(TenantMigrationError, match="offline attestation|source changed"):
        resumed.recover()
    assert (migration.paths.app / "old-state").exists()


def test_symlink_and_hardlink_sources_are_rejected(tmp_path):
    collection, media = _source(tmp_path / "legacy")
    layout = StorageLayout(tmp_path / "data")

    collection_link = tmp_path / "collection-link"
    collection_link.symlink_to(collection)
    with pytest.raises(UnsafeMigrationPath, match="symlink"):
        LegacyCollectionMigration(
            storage=layout, user_id=uuid4(), source_collection=collection_link,
            source_media=media,
        ).migrate()

    hardlink = media / "duplicate.mp3"
    os.link(media / "lesson.mp3", hardlink)
    with pytest.raises(UnsafeMigrationPath, match="hardlinked"):
        LegacyCollectionMigration(
            storage=layout, user_id=uuid4(), source_collection=collection,
            source_media=media,
        ).migrate()


def test_unsafe_existing_target_is_never_quarantined(tmp_path):
    collection, media = _source(tmp_path / "legacy")
    layout = StorageLayout(tmp_path / "data")
    user_id = uuid4()
    paths = layout.prepare_user(user_id)
    outside = tmp_path / "outside"
    outside.mkdir()
    (paths.app / "escape").symlink_to(outside, target_is_directory=True)
    migration = LegacyCollectionMigration(
        storage=layout, user_id=user_id, source_collection=collection, source_media=media,
    )
    _write_safety_evidence(migration)
    with pytest.raises(UnsafeMigrationPath, match="symlink"):
        migration.migrate()
    assert paths.root.exists()
    assert not migration.quarantine.exists()


def test_target_is_derived_only_from_canonical_uuid(tmp_path):
    collection, media = _source(tmp_path / "legacy")
    with pytest.raises(ValueError):
        LegacyCollectionMigration(
            storage=StorageLayout(tmp_path / "data"), user_id="../../escape",
            source_collection=collection, source_media=media,
        )


def test_journal_persists_preflight_and_m1_to_m9_acceptance_evidence(tmp_path):
    migration, _, _ = _migration(tmp_path)

    migration.migrate()

    journal = json.loads(migration.journal.read_text())
    preflight = journal["preflight"]
    assert preflight["source_lock"] == "exclusive-flock"
    assert preflight["observed_free_bytes"] >= preflight["required_free_bytes"]
    assert preflight["required_free_bytes"] == preflight["source_bytes"] * 2
    assert preflight["offline"]["source_digest"] == journal["source_digest"]
    assert preflight["backup"]["checks"] == {
        "R1": "pass", "R2": "pass", "R3": "pass", "M10": "pass",
    }
    acceptance = journal["acceptance"]
    assert [result["id"] for result in acceptance["results"]] == [
        f"M{number}" for number in range(1, 10)
    ]
    assert all(result["status"] == "pass" for result in acceptance["results"])
    assert len(acceptance["baseline_sha256"]) == 64
    assert len(acceptance["results_sha256"]) == 64


def test_migration_runs_real_anki_acceptance_adapter(
    tmp_path, monkeypatch, acceptance_adapter_stub,
):
    from ankiweb.adapters.anki import acceptance
    from fixture_collection import build

    monkeypatch.setattr(acceptance, "snapshot", acceptance_adapter_stub[0])
    monkeypatch.setattr(acceptance, "verify", acceptance_adapter_stub[1])
    collection = build(tmp_path / "legacy" / "collection.anki2")
    media = collection.with_suffix(".media")
    layout = StorageLayout(tmp_path / "data")
    migration = LegacyCollectionMigration(
        storage=layout, user_id=uuid4(), source_collection=collection, source_media=media,
    )
    _write_safety_evidence(migration)

    migration.migrate()

    journal = json.loads(migration.journal.read_text())
    failures = [
        check for check in journal["acceptance"]["results"] if check["status"] == "fail"
    ]
    assert not failures
    assert {check["id"] for check in journal["acceptance"]["results"]} == {
        f"M{number}" for number in range(1, 10)
    }


def test_acceptance_failure_never_quarantines_old_target(tmp_path, monkeypatch):
    migration, _, _ = _migration(tmp_path)
    from ankiweb.adapters.anki import acceptance

    checks = [
        Check(f"M{number}", f"gate {number}", "fail" if number == 7 else "pass", "")
        for number in range(1, 10)
    ]
    monkeypatch.setattr(acceptance, "verify", lambda collection, baseline: checks)

    with pytest.raises(AcceptanceGateError, match="M7"):
        migration.migrate()
    assert (migration.paths.app / "old-state").exists()
    assert not migration.quarantine.exists()
    journal = json.loads(migration.journal.read_text())
    assert journal["phase"] == int(MigrationPhase.STAGED)


def test_offline_attestation_is_required(tmp_path):
    collection, media = _source(tmp_path / "legacy")
    migration = LegacyCollectionMigration(
        storage=StorageLayout(tmp_path / "data"), user_id=uuid4(),
        source_collection=collection, source_media=media,
    )

    with pytest.raises((MigrationPreflightError, UnsafeMigrationPath), match="offline|missing"):
        migration.migrate()
    assert not migration.journal.exists()


def test_source_lock_contention_rejects_live_migration(tmp_path):
    migration, collection, _ = _migration(tmp_path)
    fd = os.open(collection, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(MigrationPreflightError, match="locked|stop"):
            migration.migrate()
    finally:
        os.close(fd)
    assert not migration.journal.exists()


def test_verified_backup_archive_is_required_and_digest_checked(tmp_path):
    migration, _, _ = _migration(tmp_path)
    evidence = json.loads(migration.backup_evidence.read_text())
    Path(evidence["archive"]).write_bytes(b"changed after verification")

    with pytest.raises(MigrationPreflightError, match="archive digest"):
        migration.migrate()
    assert not migration.journal.exists()


def test_free_space_below_twice_source_size_fails_before_staging(tmp_path, monkeypatch):
    migration, _, _ = _migration(tmp_path)
    monkeypatch.setattr(
        "ankiweb.tenancy.migration.shutil.disk_usage",
        lambda path: SimpleNamespace(total=100, used=99, free=1),
    )

    with pytest.raises(MigrationPreflightError, match="insufficient free space"):
        migration.migrate()
    assert not migration.journal.exists()
    assert not migration.stage.exists()


def test_preflight_is_durable_before_first_copy(tmp_path):
    migration, _, _ = _migration(tmp_path, phase=MigrationPhase.INITIALIZED)

    with pytest.raises(InjectedCrash):
        migration.migrate()

    journal = json.loads(migration.journal.read_text())
    assert journal["phase"] == int(MigrationPhase.INITIALIZED)
    assert journal["preflight"]["backup"]["checks"]["M10"] == "pass"
    assert journal["preflight"]["observed_free_bytes"] \
        >= journal["preflight"]["required_free_bytes"]
    assert not migration.stage.exists()


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
