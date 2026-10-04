from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass

from ankiweb.bridge.hub import BridgeHub
from ankiweb.collection_service import CollectionService
from ankiweb.identity.http import IdentityPrincipal
from ankiweb.notifier import NotifierState

from .collection import TenantCollectionRuntime
from .context import TenantContext


@dataclass(frozen=True, slots=True)
class RequestTenantRuntime:
    principal: IdentityPrincipal
    tenant: TenantContext
    runtime: TenantCollectionRuntime
    hub: BridgeHub


_current: ContextVar[RequestTenantRuntime | None] = ContextVar(
    "ankiweb_tenant_runtime", default=None
)


def bind_request_runtime(value: RequestTenantRuntime) -> Token:
    return _current.set(value)


def reset_request_runtime(token: Token) -> None:
    _current.reset(token)


def current_request_runtime() -> RequestTenantRuntime:
    value = _current.get()
    if value is None:
        raise RuntimeError("tenant runtime is unavailable outside an authenticated request")
    return value


def get_service() -> CollectionService:
    return current_request_runtime().runtime.service


def get_hub() -> BridgeHub:
    return current_request_runtime().hub


def get_notifier() -> NotifierState:
    return current_request_runtime().runtime.notifier
