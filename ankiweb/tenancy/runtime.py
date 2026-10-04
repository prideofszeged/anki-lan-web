from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import Generic, Protocol, TypeVar

from .context import ResourceKey


class CollectionRuntime(Protocol):
    async def open(self) -> None: ...

    async def close(self) -> None: ...


RuntimeT = TypeVar("RuntimeT", bound=CollectionRuntime)
RuntimeFactory = Callable[[ResourceKey], RuntimeT | Awaitable[RuntimeT]]


class RuntimeCapacityError(RuntimeError):
    code = "runtime_capacity"


@dataclass(slots=True)
class _Entry(Generic[RuntimeT]):
    runtime: RuntimeT
    open_task: asyncio.Task[None]
    leases: int
    last_used: float
    evict_when_idle: bool = False
    close_task: asyncio.Task[None] | None = None


class RuntimeLease(Generic[RuntimeT]):
    def __init__(
        self,
        registry: RuntimeRegistry[RuntimeT],
        key: ResourceKey,
        runtime: RuntimeT,
    ) -> None:
        self._registry = registry
        self.key = key
        self.runtime = runtime
        self._released = False

    async def __aenter__(self) -> RuntimeT:
        return self.runtime

    async def __aexit__(self, *_exc: object) -> None:
        await self.release()

    async def release(self) -> None:
        if not self._released:
            self._released = True
            await self._registry._release(self.key)


class RuntimeRegistry(Generic[RuntimeT]):
    """Bounded, ref-counted owner of tenant collection runtimes."""

    def __init__(
        self,
        factory: RuntimeFactory[RuntimeT],
        *,
        max_active: int = 4,
        idle_seconds: float = 900,
        wait_seconds: float = 30,
    ) -> None:
        if max_active < 1:
            raise ValueError("max_active must be at least 1")
        if idle_seconds < 0 or wait_seconds < 0:
            raise ValueError("runtime timeouts cannot be negative")
        self._factory = factory
        self._max_active = max_active
        self._idle_seconds = idle_seconds
        self._wait_seconds = wait_seconds
        self._entries: dict[ResourceKey, _Entry[RuntimeT]] = {}
        self._reservations: set[ResourceKey] = set()
        self._maintenance: set[ResourceKey] = set()
        self._condition = asyncio.Condition()
        self._accepting = True

    @property
    def active_count(self) -> int:
        return len(self._entries) + len(self._reservations)

    async def runtime_values(self) -> tuple[RuntimeT, ...]:
        async with self._condition:
            return tuple(entry.runtime for entry in self._entries.values())

    async def acquire(self, key: ResourceKey) -> RuntimeLease[RuntimeT]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._wait_seconds
        reserved = False

        while True:
            async with self._condition:
                if not self._accepting:
                    raise RuntimeError("runtime registry is draining")
                if key in self._reservations or key in self._maintenance:
                    await self._wait_for_change(deadline)
                    continue
                entry = self._entries.get(key)
                if entry is not None:
                    if entry.evict_when_idle:
                        await self._wait_for_change(deadline)
                        continue
                    entry.leases += 1
                    open_task = entry.open_task
                    runtime = entry.runtime
                    break
                if self.active_count < self._max_active:
                    self._reservations.add(key)
                    reserved = True
                    break
                await self._wait_for_change(deadline)

        if reserved:
            try:
                runtime = await self._create_runtime(key)
            except BaseException:
                async with self._condition:
                    self._reservations.discard(key)
                    self._condition.notify_all()
                raise
            open_task = asyncio.create_task(runtime.open())
            rejected = False
            async with self._condition:
                if not self._accepting:
                    rejected = True
                else:
                    self._reservations.discard(key)
                    self._entries[key] = _Entry(
                        runtime=runtime,
                        open_task=open_task,
                        leases=1,
                        last_used=monotonic(),
                    )
                    self._condition.notify_all()
            if rejected:
                open_task.cancel()
                try:
                    await asyncio.shield(open_task)
                except BaseException:
                    pass
                try:
                    await runtime.close()
                finally:
                    async with self._condition:
                        self._reservations.discard(key)
                        self._condition.notify_all()
                raise RuntimeError("runtime registry is draining")

        try:
            await asyncio.shield(open_task)
        except asyncio.CancelledError:
            failed = open_task.cancelled()
            if open_task.done() and not failed:
                failed = open_task.exception() is not None
            if failed:
                await self._release_failed_acquire(key, open_task)
            else:
                await self._cancel_acquire(key, open_task)
            raise
        except BaseException:
            await self._release_failed_acquire(key, open_task)
            raise
        return RuntimeLease(self, key, runtime)

    @asynccontextmanager
    async def maintenance(
        self, key: ResourceKey, *, wait_seconds: float | None = None,
    ):
        """Hold exclusive access to a resource after closing its live runtime."""
        loop = asyncio.get_running_loop()
        timeout = self._wait_seconds if wait_seconds is None else wait_seconds
        deadline = loop.time() + timeout
        claimed = False
        entry: _Entry[RuntimeT] | None = None
        close_task: asyncio.Task[None] | None = None
        try:
            async with self._condition:
                while key in self._maintenance or key in self._reservations:
                    await self._wait_for_change(deadline)
                if not self._accepting:
                    raise RuntimeError("runtime registry is draining")
                self._maintenance.add(key)
                claimed = True
                entry = self._entries.get(key)
                if entry is not None:
                    entry.evict_when_idle = True
                while entry is not None and entry.leases:
                    await self._wait_for_change(deadline)
                if entry is not None:
                    close_task = self._ensure_close_locked(key, entry)
            if close_task is not None:
                try:
                    await asyncio.shield(close_task)
                except asyncio.CancelledError:
                    await asyncio.shield(close_task)
                    raise
            yield
        finally:
            async with self._condition:
                if claimed:
                    self._maintenance.discard(key)
                current = self._entries.get(key)
                if current is entry and current is not None and close_task is None:
                    current.evict_when_idle = False
                self._condition.notify_all()

    async def evict(
        self,
        key: ResourceKey,
        *,
        reason: str = "requested",
        wait_seconds: float | None = None,
    ) -> bool:
        del reason  # reserved for audit/metrics integration
        loop = asyncio.get_running_loop()
        timeout = self._wait_seconds if wait_seconds is None else wait_seconds
        deadline = loop.time() + timeout
        owns_eviction = False

        try:
            while True:
                async with self._condition:
                    entry = self._entries.get(key)
                    if entry is None:
                        return owns_eviction
                    if entry.evict_when_idle and not owns_eviction:
                        await self._wait_for_change(deadline)
                        continue
                    if not owns_eviction:
                        entry.evict_when_idle = True
                        owns_eviction = True
                    if entry.leases == 0:
                        close_task = self._ensure_close_locked(key, entry)
                    else:
                        await self._wait_for_change(deadline)
                        continue
                await asyncio.shield(close_task)
                return True
        except BaseException:
            async with self._condition:
                current = self._entries.get(key)
                if (
                    current is not None
                    and owns_eviction
                    and current.close_task is None
                ):
                    current.evict_when_idle = False
                    self._condition.notify_all()
            raise

    async def evict_idle(self) -> int:
        cutoff = monotonic() - self._idle_seconds
        close_tasks: list[asyncio.Task[None]] = []
        async with self._condition:
            for key, entry in self._entries.items():
                if (
                    not entry.evict_when_idle
                    and entry.leases == 0
                    and entry.last_used <= cutoff
                ):
                    entry.evict_when_idle = True
                    close_tasks.append(self._ensure_close_locked(key, entry))
        await asyncio.gather(*(asyncio.shield(task) for task in close_tasks))
        return len(close_tasks)

    async def drain(self, *, wait_seconds: float | None = None) -> None:
        loop = asyncio.get_running_loop()
        timeout = self._wait_seconds if wait_seconds is None else wait_seconds
        deadline = loop.time() + timeout
        async with self._condition:
            self._accepting = False
            for entry in self._entries.values():
                entry.evict_when_idle = True
            while self._reservations or self._maintenance or any(
                entry.leases for entry in self._entries.values()
            ):
                await self._wait_for_change(deadline)
            close_tasks = [
                self._ensure_close_locked(key, entry)
                for key, entry in self._entries.items()
            ]
        await asyncio.gather(*(asyncio.shield(task) for task in close_tasks))

    async def _release(self, key: ResourceKey) -> None:
        async with self._condition:
            entry = self._entries.get(key)
            if entry is None or entry.leases < 1:
                raise RuntimeError("unknown or already released runtime lease")
            entry.leases -= 1
            entry.last_used = monotonic()
            if entry.leases == 0 and entry.evict_when_idle:
                self._ensure_close_locked(key, entry)
            self._condition.notify_all()

    async def _create_runtime(self, key: ResourceKey) -> RuntimeT:
        runtime = self._factory(key)
        if inspect.isawaitable(runtime):
            runtime = await runtime
        return runtime

    async def _release_failed_acquire(
        self, key: ResourceKey, open_task: asyncio.Task[None]
    ) -> None:
        async with self._condition:
            entry = self._entries.get(key)
            if entry is None or entry.open_task is not open_task:
                return
            if entry.leases > 0:
                entry.leases -= 1
            entry.evict_when_idle = True
            if entry.leases == 0:
                self._ensure_close_locked(key, entry)
            self._condition.notify_all()

    async def _cancel_acquire(
        self, key: ResourceKey, open_task: asyncio.Task[None]
    ) -> None:
        """Undo this caller's reservation without cancelling a shared open task."""
        async with self._condition:
            entry = self._entries.get(key)
            if entry is not None and entry.open_task is open_task and entry.leases > 0:
                entry.leases -= 1
                entry.last_used = monotonic()
                if entry.leases == 0 and entry.evict_when_idle:
                    self._ensure_close_locked(key, entry)
                self._condition.notify_all()

    async def _close_entry(self, entry: _Entry[RuntimeT]) -> None:
        try:
            await entry.open_task
        except BaseException:
            pass
        await entry.runtime.close()

    def _ensure_close_locked(
        self, key: ResourceKey, entry: _Entry[RuntimeT]
    ) -> asyncio.Task[None]:
        if entry.close_task is None:
            entry.close_task = asyncio.create_task(self._close_and_remove(key, entry))
        return entry.close_task

    async def _close_and_remove(
        self, key: ResourceKey, entry: _Entry[RuntimeT]
    ) -> None:
        try:
            await self._close_entry(entry)
        finally:
            async with self._condition:
                if self._entries.get(key) is entry:
                    self._entries.pop(key)
                self._condition.notify_all()

    async def _wait_for_change(self, deadline: float) -> None:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise RuntimeCapacityError("timed out waiting for a runtime slot")
        try:
            await asyncio.wait_for(self._condition.wait(), timeout=remaining)
        except TimeoutError as exc:
            raise RuntimeCapacityError("timed out waiting for a runtime slot") from exc
