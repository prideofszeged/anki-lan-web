"""``/api/v1``: health, session, JSON auth, decks. Transport only; logic lives in ``application``."""
from __future__ import annotations
import asyncio
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ankiweb.adapters.anki.decks import AnkiDeckCatalog
from ankiweb.application.decks import DeckService
from ankiweb.auth import COOKIE, LoginLimiter, SessionStore, login_client, password_ok
from ankiweb.config import Settings
from ankiweb.domain.models import DeckCounts, DeckNode

PREFIX = "/api/v1"
# Reachable without a session (everything else under PREFIX gets a JSON 401).
PUBLIC_PATHS = frozenset({
    f"{PREFIX}/health/live", f"{PREFIX}/health/ready",
    f"{PREFIX}/auth/login", f"{PREFIX}/auth/logout",
})
CAPABILITIES = ["decks.read"]
READY_TIMEOUT_SECONDS = 2.0


class Status(BaseModel):
    status: str


class User(BaseModel):
    id: str


class SessionInfo(BaseModel):
    user: User
    authenticated: bool
    authRequired: bool
    capabilities: list[str]


class LoginBody(BaseModel):
    password: str


class Counts(BaseModel):
    new: int
    learning: int
    review: int

    @classmethod
    def of(cls, c: DeckCounts) -> "Counts":
        return cls(new=c.new, learning=c.learning, review=c.review)


class Deck(BaseModel):
    id: int
    name: str
    path: str
    filtered: bool
    counts: Counts
    children: list["Deck"]

    @classmethod
    def of(cls, n: DeckNode) -> "Deck":
        return cls(id=n.id, name=n.name, path=n.path, filtered=n.filtered,
                   counts=Counts.of(n.counts), children=[cls.of(c) for c in n.children])


class DeckList(BaseModel):
    decks: list[Deck]
    counts: Counts


class DeckDetailBody(BaseModel):
    deck: Deck
    counts: Counts
    description: str


def build_api_router(get_service: Callable, sessions: SessionStore, limiter: LoginLimiter,
                     settings: Settings, auth_enabled: bool,
                     include_platform_routes: bool = True) -> APIRouter:
    router = APIRouter(prefix=PREFIX)

    def decks() -> DeckService:
        return DeckService(AnkiDeckCatalog(get_service()))

    if include_platform_routes:
        @router.get("/health/live", response_model=Status, tags=["health"])
        def live() -> Status:
            return Status(status="ok")

        @router.get("/health/ready", response_model=Status, tags=["health"],
                    responses={503: {"model": Status}})
        async def ready():
            try:
                await asyncio.wait_for(get_service().run(lambda col: col.db.scalar("select 1")),
                                       READY_TIMEOUT_SECONDS)
            except Exception:
                return JSONResponse(Status(status="unavailable").model_dump(), status_code=503)
            return Status(status="ready")

        @router.get("/session", response_model=SessionInfo, tags=["session"])
        def session() -> SessionInfo:
            # Reaching here means the guard accepted the request (valid session, or auth disabled).
            return SessionInfo(user=User(id="local"), authenticated=True,
                               authRequired=auth_enabled, capabilities=CAPABILITIES)

        @router.post("/auth/login", status_code=204, tags=["session"])
        async def login(body: LoginBody, request: Request) -> Response:
            client = login_client(request, settings.trusted_proxy_cidrs)
            if auth_enabled and not limiter.allow(client):
                raise HTTPException(429, "too many attempts")
            if auth_enabled:
                accepted = await asyncio.to_thread(
                    password_ok, body.password, settings.password, settings.password_hash)
                if not accepted:
                    raise HTTPException(401, "invalid credentials")
                limiter.reset(client)
            resp = Response(status_code=204)
            if auth_enabled:
                resp.set_cookie(COOKIE, sessions.create(), httponly=True, samesite="strict",
                                secure=settings.secure_cookie, max_age=sessions.max_age)
            return resp

        @router.post("/auth/logout", status_code=204, tags=["session"])
        def logout(request: Request) -> Response:
            sessions.revoke(request.cookies.get(COOKIE))
            resp = Response(status_code=204)
            resp.delete_cookie(COOKIE)
            return resp

    @router.get("/decks", response_model=DeckList, tags=["decks"])
    async def list_decks() -> DeckList:
        listing = await decks().list()
        return DeckList(decks=[Deck.of(d) for d in listing.decks], counts=Counts.of(listing.counts))

    @router.get("/decks/{deck_id}", response_model=DeckDetailBody, tags=["decks"],
                responses={404: {"description": "deck not found"}})
    async def get_deck(deck_id: int) -> DeckDetailBody:
        detail = await decks().get(deck_id)
        if detail is None:
            raise HTTPException(404, "deck not found")
        return DeckDetailBody(deck=Deck.of(detail.deck), counts=Counts.of(detail.counts),
                              description=detail.description)

    return router
