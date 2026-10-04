from __future__ import annotations

import asyncio
from collections import defaultdict

from fastapi import WebSocket


class ShareSocketRegistry:
    """Active member-scoped workspace event sockets.

    Membership removal calls :meth:`revoke` after the database commit. Closing is awaited,
    so the canonical removal endpoint does not report success while the removed member still
    has a live workspace channel.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sockets: dict[tuple[str, str], set[WebSocket]] = defaultdict(set)

    async def register(self, share_id: str, user_id: str, socket: WebSocket) -> None:
        async with self._lock:
            self._sockets[(share_id, user_id)].add(socket)

    async def unregister(self, share_id: str, user_id: str, socket: WebSocket) -> None:
        async with self._lock:
            group = self._sockets.get((share_id, user_id))
            if group is None:
                return
            group.discard(socket)
            if not group:
                self._sockets.pop((share_id, user_id), None)

    async def revoke(self, share_id: str, user_id: str) -> int:
        async with self._lock:
            sockets = tuple(self._sockets.pop((share_id, user_id), ()))
        if sockets:
            await asyncio.gather(*(
                socket.close(code=1008, reason="share membership revoked")
                for socket in sockets
            ), return_exceptions=True)
        return len(sockets)

    async def count(self, share_id: str, user_id: str) -> int:
        async with self._lock:
            return len(self._sockets.get((share_id, user_id), ()))
