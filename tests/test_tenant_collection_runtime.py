from pathlib import Path
from uuid import uuid4

import pytest

from ankiweb.config import Settings
from ankiweb.tenancy import (
    CollectionProcessLock,
    ResourceKey,
    ResourceLockedError,
    StorageLayout,
    TenantCollectionRuntime,
)


def test_process_lock_rejects_second_owner(tmp_path: Path) -> None:
    first = CollectionProcessLock(tmp_path / ".runtime.lock")
    second = CollectionProcessLock(tmp_path / ".runtime.lock")
    first.acquire()
    try:
        with pytest.raises(ResourceLockedError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_process_lock_rejects_symlink_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("keep")
    target.chmod(0o644)
    link = tmp_path / ".runtime.lock"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        CollectionProcessLock(link).acquire()
    assert target.stat().st_mode & 0o777 == 0o644


@pytest.mark.asyncio
async def test_runtime_uses_canonical_tenant_collection_and_releases_lock(
    tmp_path: Path,
) -> None:
    key = ResourceKey.user(uuid4())
    storage = StorageLayout(tmp_path / "data")
    settings = Settings(collection_path=tmp_path / "legacy.anki2")
    runtime = TenantCollectionRuntime(key, storage=storage, base_settings=settings)

    await runtime.open()
    assert runtime.settings.collection_path == storage.user_paths(key.resource_id).collection
    assert runtime.settings.collection_path.is_file()
    assert runtime.settings.collection_path.stat().st_mode & 0o777 == 0o600
    await runtime.close()

    replacement = TenantCollectionRuntime(key, storage=storage, base_settings=settings)
    await replacement.open()
    await replacement.close()


@pytest.mark.asyncio
async def test_failed_open_releases_process_lock(tmp_path: Path) -> None:
    key = ResourceKey.user(uuid4())
    storage = StorageLayout(tmp_path / "data")
    settings = Settings(collection_path=tmp_path / "legacy.anki2", init_collection=False)
    runtime = TenantCollectionRuntime(key, storage=storage, base_settings=settings)

    with pytest.raises(FileNotFoundError):
        await runtime.open()
    replacement_lock = CollectionProcessLock(runtime.paths.collection.parent / ".runtime.lock")
    replacement_lock.acquire()
    replacement_lock.release()


@pytest.mark.asyncio
async def test_runtime_rejects_collection_symlink(tmp_path: Path) -> None:
    key = ResourceKey.user(uuid4())
    storage = StorageLayout(tmp_path / "data")
    paths = storage.prepare_user(key.resource_id)
    target = tmp_path / "outside.anki2"
    target.write_bytes(b"not a collection")
    paths.collection.symlink_to(target)
    runtime = TenantCollectionRuntime(
        key,
        storage=storage,
        base_settings=Settings(collection_path=tmp_path / "legacy.anki2"),
    )
    with pytest.raises(ValueError, match="symlink"):
        await runtime.open()


@pytest.mark.asyncio
async def test_browser_session_hubs_do_not_share_reviewer_or_ui_state(tmp_path: Path) -> None:
    runtime = TenantCollectionRuntime(
        ResourceKey.user(uuid4()),
        storage=StorageLayout(tmp_path / "data"),
        base_settings=Settings(collection_path=tmp_path / "legacy.anki2"),
    )
    await runtime.open()
    first = runtime.hub_for("session-a")
    second = runtime.hub_for("session-b")
    assert first is not second
    first.ui_state.current_screen = "reviewer"
    assert second.ui_state.current_screen != "reviewer"

    await runtime.close_session_hub("session-a")
    assert runtime.hub_for("session-a") is not first
    await runtime.close()
