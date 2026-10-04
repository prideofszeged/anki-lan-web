from __future__ import annotations

import errno
import fcntl
import os
import stat
from dataclasses import replace
from pathlib import Path

from ankiweb.collection_service import CollectionService
from ankiweb.config import Settings
from ankiweb.bridge.hub import BridgeHub
from ankiweb.screens.routes import register_screen_handlers
from ankiweb.notifier import NotifierState

from .context import ResourceKey, ResourceKind
from .storage import StorageLayout


class ResourceLockedError(RuntimeError):
    """Another runtime/process already owns the collection."""


class CollectionProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ValueError(f"symlink not allowed for runtime lock: {self.path}") from exc
            raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError(f"runtime lock must be a regular file: {self.path}")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise ResourceLockedError(f"collection runtime already active: {self.path}") from exc
            raise
        except BaseException:
            os.close(fd)
            raise
        os.fchmod(fd, 0o600)
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class TenantCollectionRuntime:
    """One collection service plus an OS lock for a canonical tenant resource."""

    def __init__(
        self,
        key: ResourceKey,
        *,
        storage: StorageLayout,
        base_settings: Settings,
    ) -> None:
        self.key = key
        self.storage = storage
        if key.kind is ResourceKind.USER:
            paths = storage.prepare_user(key.resource_id)
        else:
            paths = storage.prepare_share(key.resource_id)
        self.paths = paths
        self.settings = replace(
            base_settings,
            collection_path=paths.collection,
            import_tmp_dir=paths.root / "tmp",
        )
        self.service = CollectionService(self.settings)
        self.notifier = NotifierState(paths.app / "notify.json")
        self._lock = CollectionProcessLock(paths.collection.parent / ".runtime.lock")
        self._open = False
        self._session_hubs: dict[str, BridgeHub] = {}

    async def open(self) -> None:
        if self._open:
            return
        if self.paths.collection.is_symlink():
            raise ValueError(f"symlink not allowed for collection: {self.paths.collection}")
        self._lock.acquire()
        try:
            await self.service.open()
            self.service.subscribe(self._broadcast_changes)
            os.chmod(self.paths.collection, 0o600)
            self._open = True
        except BaseException:
            await self.service.close()
            self._lock.release()
            raise

    async def close(self) -> None:
        try:
            await self.close_session_hubs()
            await self.service.close()
        finally:
            self._open = False
            self._lock.release()

    def hub_for(self, session_id: str) -> BridgeHub:
        """Return isolated browser/reviewer/UI state for one authenticated session."""
        hub = self._session_hubs.get(session_id)
        if hub is None:
            hub = BridgeHub()
            register_screen_handlers(self.service, hub)
            self._session_hubs[session_id] = hub
        return hub

    @property
    def session_ids(self) -> tuple[str, ...]:
        return tuple(self._session_hubs)

    @property
    def socket_count(self) -> int:
        return sum(hub.connection_count for hub in self._session_hubs.values())

    async def close_session_hub(self, session_id: str) -> None:
        hub = self._session_hubs.pop(session_id, None)
        if hub is not None:
            await hub.close_all()

    async def close_session_hubs(self) -> None:
        hubs, self._session_hubs = list(self._session_hubs.values()), {}
        for hub in hubs:
            await hub.close_all()

    async def _broadcast_changes(self, flags: dict, initiator) -> None:
        for hub in list(self._session_hubs.values()):
            await hub.broadcast_opchanges(flags, initiator)
