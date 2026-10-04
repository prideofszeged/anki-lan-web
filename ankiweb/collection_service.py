from __future__ import annotations
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar
import anki.lang
from anki.collection import Collection
from ankiweb.config import Settings
from google.protobuf.descriptor import FieldDescriptor

T = TypeVar("T")


def op_changes_to_flags(changes) -> dict:
    """Convert an OpChanges proto into a {field_name: bool} dict (only its bool fields)."""
    return {
        f.name: getattr(changes, f.name)
        for f in changes.DESCRIPTOR.fields
        if f.type == FieldDescriptor.TYPE_BOOL
    }


class CollectionService:
    """Owns the single Collection. All access is serialized: pylib objects are
    not thread-safe, and the Rust backend serializes internally anyway."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="anki")
        # Auxiliary pool for thread-safe Rust backend calls that must run CONCURRENTLY
        # with the main worker (FSRS compute/simulate + latest_progress polling +
        # set_wants_abort) so progress is observable while a long compute runs.
        self._aux_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="anki-aux")
        self._lock = asyncio.Lock()
        self._aux_condition = asyncio.Condition()
        self._exclusive_pending = False
        self._active_aux = 0
        self._col: Collection | None = None
        self._subscribers: list = []
        self._closed = False

    @property
    def settings(self):
        return self._settings

    async def open(self) -> None:
        if self._closed:
            raise RuntimeError("collection service is closed")
        path = self._settings.collection_path
        if not path.is_file() and not self._settings.init_collection:
            raise FileNotFoundError(
                f"collection does not exist at {path}; restore it or set "
                "ANKIWEB_INIT_COLLECTION=1 for an intentional new collection"
            )
        path.parent.mkdir(parents=True, exist_ok=True)

        def _open() -> Collection:
            anki.lang.set_lang(self._settings.lang or "en")
            return Collection(str(path), server=False)

        loop = asyncio.get_running_loop()
        self._col = await loop.run_in_executor(self._executor, _open)

    async def reopen(self) -> None:
        """Re-open the collection on the worker WITHOUT shutting it down — for ops
        that close it (export_collection_package). Unlike close(), keeps the executor."""
        path = self._settings.collection_path

        old = self._col

        def _reopen() -> Collection:
            # Re-apply the language for self-consistency with open(): a fresh Collection
            # otherwise inherits the process-global, which a second service with a different
            # lang could have changed. Defensive — the single-service topology makes the
            # global sufficient today.
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            anki.lang.set_lang(self._settings.lang or "en")
            return Collection(str(path), server=False)

        loop = asyncio.get_event_loop()
        async with self._lock:
            self._col = await loop.run_in_executor(self._executor, _reopen)

    async def close(self) -> None:
        if self._closed:
            return
        col, self._col = self._col, None
        loop = asyncio.get_running_loop()
        if col is not None:
            await loop.run_in_executor(self._executor, lambda: col.close())
        await loop.run_in_executor(None, self._executor.shutdown)
        await loop.run_in_executor(None, self._aux_executor.shutdown)
        self._closed = True

    async def run(self, fn: Callable[[Collection], T]) -> T:
        async with self._lock:
            col = self._col
            if col is None:
                raise RuntimeError("collection not open")
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, lambda: fn(col))

    async def export_collection_package(
        self, out_path: str, include_media: bool, legacy: bool
    ) -> None:
        """Export and replace the collection while holding one lifecycle boundary.

        pylib closes the Collection during a full export. Holding ``_lock`` prevents
        ordinary requests queued during the export from seeing that closed object. The
        auxiliary-reader gate also drains/pauses concurrent Rust backend calls while the
        backend is replaced, without sacrificing their normal FSRS progress concurrency.
        """
        async with self._lock:
            await self._begin_exclusive()
            try:
                col = self._col
                if col is None:
                    raise RuntimeError("collection not open")

                def _export_and_reopen() -> None:
                    try:
                        col.export_collection_package(out_path, include_media, legacy)
                    finally:
                        try:
                            col.close()
                        except Exception:
                            pass
                        self._col = None
                        anki.lang.set_lang(self._settings.lang or "en")
                        self._col = Collection(str(self._settings.collection_path), server=False)

                loop = asyncio.get_running_loop()
                await loop.run_in_executor(self._executor, _export_and_reopen)
            finally:
                await self._end_exclusive()

    async def run_op(self, fn: Callable[[Collection], T], initiator: str | None = None) -> T:
        """Run a mutating op (fn returns OpChanges or an OpChanges* wrapper), then
        broadcast the change flags on the bus. Returns the op result unchanged."""
        result = await self.run(fn)
        changes = getattr(result, "changes", result)
        flags = op_changes_to_flags(changes)
        if any(flags.values()):  # skip no-op broadcasts (e.g. set_current returns all-False)
            await self.emit(flags, initiator)
        return result

    async def backend_raw(self, method: str, data: bytes) -> bytes:
        def fn(col):
            return getattr(col._backend, f"{method}_raw")(data)
        return await self.run(fn)

    async def backend_raw_concurrent(self, method: str, data: bytes) -> bytes:
        """Call `col._backend.<method>_raw` OFF the serialized main worker, on the aux
        pool, so it runs CONCURRENTLY with the main worker and with other aux calls.
        ONLY for thread-safe Rust backend calls that don't mutate Python-side collection
        state: FSRS compute/simulate (long, read-only), `latest_progress` (polled while
        they run), and `set_wants_abort` (cancels them). The Rust backend serializes its
        own collection access internally; `latest_progress` uses a separate lock, so it
        returns live progress while a compute holds the collection lock."""
        await self._begin_aux()
        try:
            col = self._col
            if col is None:
                raise RuntimeError("collection not open")
            fn = getattr(col._backend, f"{method}_raw")
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._aux_executor, lambda: fn(data))
        finally:
            await self._end_aux()

    async def _begin_aux(self) -> None:
        async with self._aux_condition:
            await self._aux_condition.wait_for(lambda: not self._exclusive_pending)
            self._active_aux += 1

    async def _end_aux(self) -> None:
        async with self._aux_condition:
            self._active_aux -= 1
            if self._active_aux == 0:
                self._aux_condition.notify_all()

    async def _begin_exclusive(self) -> None:
        async with self._aux_condition:
            self._exclusive_pending = True
            await self._aux_condition.wait_for(lambda: self._active_aux == 0)

    async def _end_exclusive(self) -> None:
        async with self._aux_condition:
            self._exclusive_pending = False
            self._aux_condition.notify_all()

    def subscribe(self, cb) -> None:
        """cb(changes, initiator) — called after a mutating op broadcasts changes."""
        self._subscribers.append(cb)

    async def emit(self, changes, initiator) -> None:
        for cb in list(self._subscribers):
            res = cb(changes, initiator)
            if asyncio.iscoroutine(res):
                await res
