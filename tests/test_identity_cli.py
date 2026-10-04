from pathlib import Path

import pytest

from ankiweb.config import Settings
from ankiweb.identity import ConflictError, IdentityDatabase, IdentityRepository
from ankiweb.identity.cli import bootstrap_owner
from ankiweb.identity.cli import migrate_legacy_collection, prepare_legacy_migration
from ankiweb.tenancy import StorageLayout


def test_bootstrap_creates_owner_db_and_private_root(tmp_path: Path) -> None:
    settings = Settings(
        collection_path=tmp_path / "legacy/collection.anki2",
        data_root=tmp_path / "data",
    )
    owner = bootstrap_owner(
        settings,
        username="local",
        display_name="Local owner",
        password="safe-password",
    )

    storage = StorageLayout(tmp_path / "data")
    repo = IdentityRepository(IdentityDatabase(storage.app_db))
    assert repo.get_user(owner.id).global_role.value == "owner"
    assert storage.user_paths(owner.id).root.is_dir()


def test_bootstrap_refuses_second_owner(tmp_path: Path) -> None:
    settings = Settings(
        collection_path=tmp_path / "legacy/collection.anki2",
        data_root=tmp_path / "data",
    )
    bootstrap_owner(
        settings,
        username="local",
        display_name="Local owner",
        password="safe-password",
    )
    with pytest.raises(ConflictError, match="already provisioned"):
        bootstrap_owner(
            settings,
            username="second",
            display_name="Second",
            password="safe-password",
        )


def test_bootstrap_storage_failure_rolls_back_identity(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        collection_path=tmp_path / "legacy/collection.anki2",
        data_root=tmp_path / "data",
    )

    def fail(_self, _user_id):
        raise OSError("disk unavailable")

    monkeypatch.setattr(StorageLayout, "prepare_user", fail)
    with pytest.raises(OSError, match="disk unavailable"):
        bootstrap_owner(
            settings,
            username="local",
            display_name="Local owner",
            password="safe-password",
        )

    repo = IdentityRepository(IdentityDatabase(StorageLayout(tmp_path / "data").app_db))
    assert repo.get_user_by_username("local") is None


def test_migrate_legacy_collection_requires_existing_user_and_completes(tmp_path: Path) -> None:
    from fixture_collection import build

    settings = Settings(
        collection_path=tmp_path / "legacy/anki/collection.anki2",
        data_root=tmp_path / "data",
    )
    build(settings.collection_path.parent)
    from ankiweb.adapters.anki.backup import create_backup
    archive = create_backup(tmp_path / "legacy", tmp_path / "backups")
    owner = bootstrap_owner(
        settings,
        username="local",
        display_name="Local owner",
        password="safe-password",
    )
    offline, evidence = prepare_legacy_migration(
        settings, user_id=owner.id, backup_archive=archive,
    )
    assert offline.is_file()
    assert evidence.is_file()

    result = migrate_legacy_collection(settings, user_id=owner.id)
    assert result.paths.collection.is_file()
    with pytest.raises(ValueError, match="does not exist"):
        migrate_legacy_collection(settings, user_id="00000000-0000-0000-0000-000000000000")
