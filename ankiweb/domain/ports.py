from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar

from ankiweb.domain.models import DeckNode

T = TypeVar("T")


class CollectionGateway(Protocol):
    """Only supported path from application modules to Anki collection state."""

    async def run(self, fn: Callable[[Any], T]) -> T: ...

    async def run_op(self, fn: Callable[[Any], T], initiator: str | None = None) -> T: ...

    async def backend_raw(self, method: str, data: bytes) -> bytes: ...

    def subscribe(self, cb: Callable[..., Awaitable[None] | None]) -> None: ...


class DeckCatalog(Protocol):
    """Read-only view of the deck hierarchy with today's due counts."""

    async def tree(self) -> list[DeckNode]: ...

    async def description(self, deck_id: int) -> str | None: ...
