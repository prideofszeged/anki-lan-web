from __future__ import annotations
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from ankiweb.config import host_allowed
from ankiweb.auth import COOKIE
from ankiweb.security import origin_ok

log = logging.getLogger(__name__)


def build_router(get_hub, allowed_hosts=(), cookie_valid=lambda _token: True,
                 auth_required: bool = False) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket, context: str = "default"):
        # BaseHTTPMiddleware (host + auth guards) does NOT cover WS upgrades — check here too.
        host = websocket.headers.get("host", "")
        token = websocket.cookies.get(COOKIE)
        if not host_allowed(host, allowed_hosts):
            await websocket.close(code=1008)
            return
        if not cookie_valid(token):
            await websocket.close(code=1008)
            return
        if not origin_ok("WS", websocket.headers, host, allowed_hosts,
                         has_session=auth_required):
            await websocket.close(code=1008)
            return
        hub = get_hub()
        await websocket.accept()
        connection_id = hub.register(context, websocket)
        hub.ui_state.current_screen = context
        try:
            while True:
                # Malformed-frame hardening: a bad-JSON frame, a non-object payload, a
                # missing field, or a handler error must NOT drop the live socket. Only
                # *frame-content* errors are skipped: receive_json() on a socket that is already
                # gone raises RuntimeError (WebSocketDisconnected, which is NOT a
                # WebSocketDisconnect) without ever yielding, so swallowing it with a blanket
                # `continue` is a hot loop that starves the whole event loop (observed: container
                # pinned at 100% CPU, /healthz timing out). Anything else ends the session.
                try:
                    msg = await websocket.receive_json()
                except (ValueError, KeyError, TypeError):
                    continue  # malformed frame (bad JSON, binary/missing payload) — skip it
                if auth_required and not cookie_valid(token):
                    await websocket.close(code=1008)
                    return
                if not isinstance(msg, dict):
                    continue
                mtype = msg.get("type")
                if mtype == "cmd":
                    try:
                        result = await hub.dispatch_cmd(context, msg.get("arg", ""))
                    except Exception:
                        log.exception("bridge command failed in context %s", context)
                        result = None  # a handler error must not drop the session
                    if msg.get("id") is not None:
                        await websocket.send_json(
                            {"type": "result", "id": msg["id"], "value": result})
                elif mtype == "result":
                    mid = msg.get("id")
                    if mid is not None:
                        hub.resolve(mid, msg.get("value"), connection_id)
                elif mtype == "ready":
                    pass  # domDone handshake; per-screen logic handles buffering
        except (WebSocketDisconnect, RuntimeError):
            pass  # client left, or the socket was already torn down (e.g. failed broadcast send)
        finally:
            hub.unregister(context, websocket, connection_id)

    return router
