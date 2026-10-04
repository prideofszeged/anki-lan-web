from __future__ import annotations
import asyncio
from typing import Any, Awaitable, Callable
from ankiweb.bridge.ui_state import UiState


class BridgeHub:
    """Tracks WebSocket connections per UI context and pushes messages to them."""

    def __init__(self) -> None:
        self._conns: dict[str, dict[int, Any]] = {}
        self._next_id = 0
        self._next_connection_id = 0
        self._pending: dict[int, tuple[int, asyncio.Future]] = {}
        # ctx -> async handler(arg:str) -> json-serializable result
        self._handlers: dict[str, Callable[[str], Awaitable[Any]]] = {}
        self.ui_state = UiState()

    def register(self, ctx: str, ws) -> int:
        self._next_connection_id += 1
        connection_id = self._next_connection_id
        self._conns.setdefault(ctx, {})[connection_id] = ws
        return connection_id

    @property
    def connection_count(self) -> int:
        return sum(len(connections) for connections in self._conns.values())

    def unregister(self, ctx: str, ws, connection_id: int | None = None) -> None:
        conns = self._conns.get(ctx, {})
        if connection_id is None:
            connection_id = next((ident for ident, conn in conns.items() if conn is ws), None)
        if connection_id is None or conns.get(connection_id) is not ws:
            return
        conns.pop(connection_id, None)
        for message_id, (owner, future) in list(self._pending.items()):
            if owner == connection_id:
                self._pending.pop(message_id, None)
                if not future.done():
                    future.cancel()

    def set_handler(self, ctx: str, handler: Callable[[str], Awaitable[Any]]) -> None:
        self._handlers[ctx] = handler

    async def _send_all(self, ctx: str, msg: dict) -> None:
        for connection_id, ws in list(self._conns.get(ctx, {}).items()):
            try:
                await ws.send_json(msg)
            except Exception:
                # A closed tab (navigation, sleep, network drop) must not abort delivery to the
                # other sockets or bubble into the collection's change callback. Drop it so it is
                # not retried on every later broadcast; its receive loop ends on its own.
                self.unregister(ctx, ws, connection_id)

    async def push_call(self, ctx: str, fn: str, args: list) -> None:
        await self._send_all(ctx, {"type": "call", "id": None, "fn": fn, "args": args})

    async def push_eval(self, ctx: str, js: str) -> None:
        await self._send_all(ctx, {"type": "eval", "id": None, "js": js})

    async def broadcast_opchanges(self, flags: dict, initiator) -> None:
        msg = {"type": "opchanges", "flags": flags, "initiator": initiator}
        for ctx in list(self._conns):
            await self._send_all(ctx, msg)

    async def close_all(self, code: int = 1008) -> None:
        """Close and forget every socket/pending callback owned by this session hub."""
        sockets = [
            (ctx, connection_id, ws)
            for ctx, conns in self._conns.items()
            for connection_id, ws in conns.items()
        ]
        for ctx, connection_id, ws in sockets:
            try:
                await ws.close(code=code)
            except Exception:
                pass
            self.unregister(ctx, ws, connection_id)

    # --- request/response (evalWithCallback / cmd callback) ---
    def _alloc(self, connection_id: int) -> tuple[int, asyncio.Future]:
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[self._next_id] = (connection_id, fut)
        return self._next_id, fut

    def resolve(self, msg_id: int, value: Any, connection_id: int | None = None) -> None:
        pending = self._pending.get(msg_id)
        if pending is None:
            return
        owner, fut = pending
        if connection_id is not None and owner != connection_id:
            return
        self._pending.pop(msg_id, None)
        if not fut.done():
            fut.set_result(value)

    async def eval_with_callback(
        self, ctx: str, js: str, connection_id: int | None = None
    ) -> Any:
        conns = self._conns.get(ctx, {})
        if connection_id is None:
            connection_id = next(iter(conns), None)
        ws = conns.get(connection_id) if connection_id is not None else None
        if ws is None:
            raise RuntimeError(f"no live websocket for context {ctx!r}")
        msg_id, fut = self._alloc(connection_id)
        try:
            await ws.send_json({"type": "eval", "id": msg_id, "js": js})
        except Exception:
            self._pending.pop(msg_id, None)
            self.unregister(ctx, ws, connection_id)
            raise
        return await fut

    async def dispatch_cmd(self, ctx: str, arg: str) -> Any:
        self.ui_state.current_screen = ctx
        handler = self._handlers.get(ctx)
        if handler is None:
            return None
        return await handler(arg)
