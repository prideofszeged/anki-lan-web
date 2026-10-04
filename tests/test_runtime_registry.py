import asyncio
from uuid import uuid4

import pytest

from ankiweb.tenancy import ResourceKey, RuntimeCapacityError, RuntimeRegistry


class FakeRuntime:
    def __init__(self) -> None:
        self.opens = 0
        self.closes = 0

    async def open(self) -> None:
        self.opens += 1

    async def close(self) -> None:
        self.closes += 1


@pytest.mark.asyncio
async def test_same_key_reuses_one_runtime_until_evicted() -> None:
    created: list[FakeRuntime] = []

    def factory(_key: ResourceKey) -> FakeRuntime:
        created.append(FakeRuntime())
        return created[-1]

    registry = RuntimeRegistry(factory, idle_seconds=0)
    first = await registry.acquire(ResourceKey.user(uuid4()))
    second = await registry.acquire(first.key)
    assert first.runtime is second.runtime
    assert created[0].opens == 1

    await first.release()
    await second.release()
    assert await registry.evict_idle() == 1
    assert created[0].closes == 1


@pytest.mark.asyncio
async def test_capacity_waits_then_admits_next_tenant() -> None:
    registry = RuntimeRegistry(
        lambda _key: FakeRuntime(), max_active=1, idle_seconds=0, wait_seconds=1
    )
    first = await registry.acquire(ResourceKey.user(uuid4()))
    waiting = asyncio.create_task(registry.acquire(ResourceKey.user(uuid4())))
    await asyncio.sleep(0)
    assert not waiting.done()

    await first.release()
    assert await registry.evict_idle() == 1
    second = await waiting
    await second.release()
    await registry.drain()


@pytest.mark.asyncio
async def test_capacity_timeout_has_stable_error_code() -> None:
    registry = RuntimeRegistry(lambda _key: FakeRuntime(), max_active=1, wait_seconds=0.01)
    lease = await registry.acquire(ResourceKey.user(uuid4()))
    with pytest.raises(RuntimeCapacityError) as error:
        await registry.acquire(ResourceKey.user(uuid4()))
    assert error.value.code == "runtime_capacity"
    await lease.release()
    await registry.drain()


@pytest.mark.asyncio
async def test_evict_waits_for_active_lease_then_closes() -> None:
    runtime = FakeRuntime()
    registry = RuntimeRegistry(lambda _key: runtime, wait_seconds=1)
    lease = await registry.acquire(ResourceKey.user(uuid4()))
    eviction = asyncio.create_task(registry.evict(lease.key))
    await asyncio.sleep(0)
    assert not eviction.done()

    await lease.release()
    assert await eviction is True
    assert runtime.closes == 1


@pytest.mark.asyncio
async def test_drain_stops_new_admission_and_closes_runtime() -> None:
    runtime = FakeRuntime()
    registry = RuntimeRegistry(lambda _key: runtime)
    lease = await registry.acquire(ResourceKey.share(uuid4()))
    await lease.release()
    await registry.drain()
    assert runtime.closes == 1
    with pytest.raises(RuntimeError, match="draining"):
        await registry.acquire(ResourceKey.user(uuid4()))


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_runtime_open() -> None:
    gate = asyncio.Event()

    class SlowRuntime(FakeRuntime):
        async def open(self) -> None:
            self.opens += 1
            await gate.wait()

    runtime = SlowRuntime()
    key = ResourceKey.user(uuid4())
    registry = RuntimeRegistry(lambda _key: runtime)
    cancelled = asyncio.create_task(registry.acquire(key))
    survivor = asyncio.create_task(registry.acquire(key))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    gate.set()
    lease = await survivor
    assert runtime.opens == 1
    await lease.release()
    await registry.drain()


@pytest.mark.asyncio
async def test_successful_open_racing_cancellation_keeps_shared_runtime() -> None:
    gate = asyncio.Event()

    class SlowRuntime(FakeRuntime):
        async def open(self) -> None:
            self.opens += 1
            await gate.wait()

    runtime = SlowRuntime()
    key = ResourceKey.user(uuid4())
    registry = RuntimeRegistry(lambda _key: runtime)
    cancelled = asyncio.create_task(registry.acquire(key))

    while key not in registry._entries:
        await asyncio.sleep(0)
    open_task = registry._entries[key].open_task
    survivor = asyncio.create_task(registry.acquire(key))
    open_task.add_done_callback(lambda _task: cancelled.cancel())
    gate.set()

    with pytest.raises(asyncio.CancelledError):
        await cancelled
    lease = await survivor
    assert registry.active_count == 1
    assert runtime.opens == 1
    assert runtime.closes == 0
    await lease.release()
    await registry.drain()


@pytest.mark.asyncio
async def test_replacement_waits_until_previous_runtime_is_fully_closed() -> None:
    close_gate = asyncio.Event()
    created: list[FakeRuntime] = []

    class SlowCloseRuntime(FakeRuntime):
        async def close(self) -> None:
            await close_gate.wait()
            await super().close()

    def factory(_key: ResourceKey) -> FakeRuntime:
        runtime = SlowCloseRuntime() if not created else FakeRuntime()
        created.append(runtime)
        return runtime

    key = ResourceKey.user(uuid4())
    registry = RuntimeRegistry(factory, wait_seconds=1)
    first = await registry.acquire(key)
    await first.release()
    eviction = asyncio.create_task(registry.evict(key))
    await asyncio.sleep(0)
    replacement = asyncio.create_task(registry.acquire(key))
    await asyncio.sleep(0)
    assert not replacement.done()
    assert len(created) == 1

    close_gate.set()
    assert await eviction is True
    second = await replacement
    assert len(created) == 2
    await second.release()
    await registry.drain()


@pytest.mark.asyncio
async def test_concurrent_evict_has_one_close_owner() -> None:
    runtime = FakeRuntime()
    key = ResourceKey.user(uuid4())
    registry = RuntimeRegistry(lambda _key: runtime)
    lease = await registry.acquire(key)
    await lease.release()
    first, second = await asyncio.gather(registry.evict(key), registry.evict(key))
    assert sorted((first, second)) == [False, True]
    assert runtime.closes == 1


@pytest.mark.asyncio
async def test_failed_open_cleanup_finishes_before_replacement() -> None:
    cleanup_gate = asyncio.Event()
    cleanup_started = asyncio.Event()
    created: list[FakeRuntime] = []

    class BrokenRuntime(FakeRuntime):
        async def open(self) -> None:
            raise RuntimeError("broken")

        async def close(self) -> None:
            cleanup_started.set()
            await cleanup_gate.wait()
            await super().close()

    def factory(_key: ResourceKey) -> FakeRuntime:
        runtime = BrokenRuntime() if not created else FakeRuntime()
        created.append(runtime)
        return runtime

    key = ResourceKey.user(uuid4())
    registry = RuntimeRegistry(factory, wait_seconds=1)
    broken = asyncio.create_task(registry.acquire(key))
    await cleanup_started.wait()
    replacement = asyncio.create_task(registry.acquire(key))
    await asyncio.sleep(0)
    assert len(created) == 1
    cleanup_gate.set()
    with pytest.raises(RuntimeError, match="broken"):
        await broken
    lease = await replacement
    assert len(created) == 2
    await lease.release()
    await registry.drain()


@pytest.mark.asyncio
async def test_drain_completes_when_in_flight_open_fails() -> None:
    open_gate = asyncio.Event()

    class BrokenRuntime(FakeRuntime):
        async def open(self) -> None:
            self.opens += 1
            await open_gate.wait()
            raise RuntimeError("broken")

    runtime = BrokenRuntime()
    key = ResourceKey.user(uuid4())
    registry = RuntimeRegistry(lambda _key: runtime, wait_seconds=1)
    pending = asyncio.create_task(registry.acquire(key))

    while key not in registry._entries:
        await asyncio.sleep(0)
    draining = asyncio.create_task(registry.drain(wait_seconds=1))
    await asyncio.sleep(0)
    open_gate.set()

    with pytest.raises(RuntimeError, match="broken"):
        await pending
    await asyncio.wait_for(draining, timeout=1)
    assert runtime.closes == 1
    assert registry.active_count == 0


@pytest.mark.asyncio
async def test_async_factory_does_not_block_release_or_eviction() -> None:
    factory_gate = asyncio.Event()
    first_key, second_key = ResourceKey.user(uuid4()), ResourceKey.user(uuid4())

    async def factory(key: ResourceKey) -> FakeRuntime:
        if key == second_key:
            await factory_gate.wait()
        return FakeRuntime()

    registry = RuntimeRegistry(factory, max_active=2, wait_seconds=1)
    first = await registry.acquire(first_key)
    pending = asyncio.create_task(registry.acquire(second_key))
    await asyncio.sleep(0)
    await asyncio.wait_for(first.release(), timeout=0.1)
    await asyncio.wait_for(registry.evict(first_key), timeout=0.1)
    factory_gate.set()
    second = await pending
    await second.release()
    await registry.drain()
