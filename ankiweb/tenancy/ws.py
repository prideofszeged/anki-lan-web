from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ankiweb.config import host_allowed
from ankiweb.identity.http import IDENTITY_COOKIE
from ankiweb.identity.service import IdentityService
from ankiweb.security import origin_ok

from .collection import TenantCollectionRuntime
from .context import ResourceKey
from .runtime import RuntimeCapacityError, RuntimeRegistry

log = logging.getLogger(__name__)


def build_tenant_ws_router(
    identity: IdentityService,
    registry: RuntimeRegistry[TenantCollectionRuntime],
    allowed_hosts=(),
) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket, context: str = "default"):
        host = websocket.headers.get("host", "")
        token = websocket.cookies.get(IDENTITY_COOKIE)
        session = await asyncio.to_thread(identity.authenticate, token, refresh=False)
        if not host_allowed(host, allowed_hosts) or session is None:
            await websocket.close(code=1008)
            return
        if not origin_ok("WS", websocket.headers, host, allowed_hosts, has_session=True):
            await websocket.close(code=1008)
            return

        key = ResourceKey.user(session.user_id)
        try:
            initial = await registry.acquire(key)
        except RuntimeCapacityError:
            await websocket.close(code=1013)
            return
        runtime = initial.runtime
        hub = runtime.hub_for(session.id)
        quota = await asyncio.to_thread(identity.repository.get_quota, session.user_id)
        if runtime.socket_count >= quota.review_sockets:
            await initial.release()
            await websocket.close(code=1008, reason="review socket quota exceeded")
            return
        connection_id = hub.register(context, websocket)
        await initial.release()

        try:
            await websocket.accept()
            hub.ui_state.current_screen = context
            while True:
                try:
                    msg = await websocket.receive_json()
                except (ValueError, KeyError, TypeError):
                    continue
                live = await asyncio.to_thread(identity.authenticate, token, refresh=False)
                if live is None or live.id != session.id:
                    await websocket.close(code=1008)
                    return
                if not isinstance(msg, dict):
                    continue
                try:
                    lease = await registry.acquire(key)
                except RuntimeCapacityError:
                    if msg.get("id") is not None:
                        await websocket.send_json({
                            "type": "result", "id": msg["id"], "value": None,
                            "error": "runtime_capacity",
                        })
                    continue
                try:
                    if lease.runtime is not runtime:
                        await websocket.close(code=1012)
                        return
                    mtype = msg.get("type")
                    if mtype == "cmd":
                        try:
                            result = await hub.dispatch_cmd(context, msg.get("arg", ""))
                        except Exception:
                            log.exception("tenant bridge command failed in context %s", context)
                            result = None
                        if msg.get("id") is not None:
                            await websocket.send_json({
                                "type": "result", "id": msg["id"], "value": result,
                            })
                    elif mtype == "result" and msg.get("id") is not None:
                        hub.resolve(msg["id"], msg.get("value"), connection_id)
                finally:
                    await lease.release()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.unregister(context, websocket, connection_id)

    return router
