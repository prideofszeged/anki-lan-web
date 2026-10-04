from pathlib import Path
from uuid import uuid4

import pytest

from ankiweb.tenancy import StorageLayout


def test_user_storage_matches_canonical_layout(tmp_path: Path) -> None:
    ident = uuid4()
    layout = StorageLayout(tmp_path / "data")
    paths = layout.prepare_user(ident)

    assert paths.collection == tmp_path / "data/users" / str(ident) / "anki/collection.anki2"
    assert paths.media.name == "collection.media"
    assert paths.temporary.is_dir()
    assert paths.app.is_dir()
    assert paths.backups.is_dir()
    assert paths.root.stat().st_mode & 0o777 == 0o700


def test_share_release_storage_is_separate_from_users(tmp_path: Path) -> None:
    ident = uuid4()
    layout = StorageLayout(tmp_path / "data")
    paths = layout.prepare_share(ident)

    assert paths.collection == tmp_path / "data/shares" / str(ident) / "anki/collection.anki2"
    assert paths.releases.is_dir()
    assert paths.backups == tmp_path / "data/backups/shares" / str(ident)


def test_symlink_inside_managed_path_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "users").mkdir(parents=True)
    ident = uuid4()
    (root / "users" / str(ident)).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        StorageLayout(root).prepare_user(ident)


def test_user_usage_counts_private_tree_and_backups(tmp_path: Path) -> None:
    layout = StorageLayout(tmp_path / "data")
    paths = layout.prepare_user(uuid4())
    (paths.app / "state").write_bytes(b"1234")
    (paths.media / "audio.mp3").write_bytes(b"123456")
    (paths.backups / "backup").write_bytes(b"123")
    assert layout.user_usage_bytes(paths.root.name) == 13
