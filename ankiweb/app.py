from __future__ import annotations
import asyncio
import html
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from ankiweb.config import Settings, host_allowed
from ankiweb.auth import COOKIE, LoginLimiter, SessionStore, login_client, password_ok
from ankiweb.security import origin_ok, security_headers
from ankiweb.api.v1 import PREFIX as API_PREFIX, PUBLIC_PATHS as API_PUBLIC, build_api_router


def _login_html(error: bool = False) -> str:
    """Self-contained login page (no /_anki assets, so it works before authentication)."""
    err = "<p style='color:#c0392b;margin:0 0 12px'>密码错误 / Wrong password</p>" if error else ""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>ankiweb</title><style>"
        "body{font-family:system-ui,sans-serif;margin:0;min-height:100vh;display:flex;"
        "align-items:center;justify-content:center;background:#f0f0f0}"
        "form{background:#fff;padding:28px 34px;border-radius:10px;text-align:center;"
        "box-shadow:0 2px 10px rgba(0,0,0,.12)}h1{font-size:18px;margin:0 0 18px}"
        "input{font-size:16px;padding:9px 10px;width:220px;box-sizing:border-box}"
        "button{font-size:16px;padding:9px 22px;margin-top:14px;cursor:pointer;"
        "border:0;border-radius:6px;background:#2d7dd2;color:#fff}</style></head><body>"
        "<form method='post' action='/login'><h1>ankiweb</h1>"
        f"{err}"
        "<input type='password' name='password' autofocus placeholder='密码 / Password'><br>"
        "<button type='submit'>进入 / Enter</button></form></body></html>"
    )
from ankiweb.collection_service import CollectionService
from ankiweb.bridge.hub import BridgeHub
from ankiweb.assets import build_router as build_assets_router, build_media_router, build_sveltekit_router
from ankiweb.anki_rpc import build_router as build_rpc_router
from ankiweb.bridge.ws import build_router as build_ws_router
from ankiweb.screens.routes import build_screen_router, register_screen_handlers
from ankiweb.notifier import NotifierState


def create_app(settings: Settings | None = None, service: CollectionService | None = None,
               hub: BridgeHub | None = None, notifier=None) -> FastAPI:
    settings = settings or Settings.from_env()
    owns = service is None
    sessions = SessionStore()
    login_limiter = LoginLimiter()
    auth_enabled = bool(settings.password or settings.password_hash)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = service
        if owns:
            svc = CollectionService(settings)
            await svc.open()
        h = hub if hub is not None else BridgeHub()
        svc.subscribe(lambda flags, initiator: h.broadcast_opchanges(flags, initiator))
        app.state.settings = settings
        app.state.service = svc
        app.state.hub = h
        app.state.notifier = notifier if notifier is not None else NotifierState(
            settings.collection_path.parent / "notify.json")
        register_screen_handlers(svc, h)
        try:
            yield
        finally:
            if owns:
                await svc.close()

    app = FastAPI(title="ankiweb", lifespan=lifespan)

    extra_headers = security_headers(hsts=settings.secure_cookie)

    def _gate(path: str):
        """Response for an unauthenticated request, or None when the path is public:
        JSON 401 for the API, a redirect to the login form for pages."""
        if path.startswith(API_PREFIX + "/"):
            return None if path in API_PUBLIC else JSONResponse(
                {"detail": "unauthenticated"}, status_code=401)
        return None if path in ("/login", "/logout", "/healthz") else RedirectResponse(
            "/login", status_code=303)

    async def guard(request, call_next):
        """One ordered gate so the baseline headers land on every response, including the
        403/303 short-circuits: host (DNS-rebinding) -> origin (CSRF, V8) -> session."""
        host = request.headers.get("host", "")
        session_ok = auth_enabled and sessions.valid(request.cookies.get(COOKIE))
        if not host_allowed(host, settings.allowed_hosts):
            resp = PlainTextResponse("forbidden host", status_code=403)
        elif not origin_ok(request.method, request.headers, host, settings.allowed_hosts,
                           has_session=session_ok):
            resp = PlainTextResponse("cross-origin request blocked", status_code=403)
        # Only gates when a password is configured (startup refuses to run without one unless
        # ANKIWEB_AUTH_DISABLED). /login, /logout, /healthz stay reachable for the login form.
        elif auth_enabled and not session_ok and (denied := _gate(request.url.path)):
            resp = denied
        else:
            resp = await call_next(request)
        for name, value in extra_headers.items():
            resp.headers.setdefault(name, value)
        return resp

    app.add_middleware(BaseHTTPMiddleware, dispatch=guard)

    # --- specific routes FIRST, media catch-all LAST (Starlette matches in order) ---
    @app.get("/healthz")
    def healthz():
        # Docker/Caddy liveness must remain responsive while the serialized collection
        # worker is busy. Collection readiness lives at /api/v1/health/ready.
        return {"ok": True}

    @app.get("/login", response_class=HTMLResponse)
    def login_form():
        # already authenticated (or no gate) -> straight to the app
        return HTMLResponse(_login_html())

    @app.post("/login")
    async def login_submit(request: Request):
        client = login_client(request)
        if not login_limiter.allow(client):
            return HTMLResponse(_login_html(error=True), status_code=429)
        form = await request.form()
        accepted = auth_enabled and await asyncio.to_thread(
            password_ok, form.get("password", ""), settings.password, settings.password_hash)
        if accepted:
            login_limiter.reset(client)
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE, sessions.create(), httponly=True, samesite="strict",
                            secure=settings.secure_cookie, max_age=sessions.max_age)
            return resp
        return HTMLResponse(_login_html(error=True), status_code=401)

    @app.get("/logout")
    def logout(request: Request):
        sessions.revoke(request.cookies.get(COOKIE))
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    app.include_router(build_api_router(lambda: app.state.service, sessions, login_limiter,
                                        settings, auth_enabled))   # /api/v1/*

    static_dir = settings.shell_dir / "static"
    static_dir.mkdir(parents=True, exist_ok=True)

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        return FileResponse(
            static_dir / "sw.js",
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    app.mount("/shell/static", StaticFiles(directory=str(static_dir), check_dir=False), name="shell")

    app.include_router(build_assets_router(settings.assets_dir))       # GET  /_anki/{path}
    app.include_router(build_rpc_router(lambda: app.state.service, lambda: app.state.hub))    # POST /_anki/{method}
    app.include_router(build_ws_router(
        lambda: app.state.hub,
        settings.allowed_hosts,
        (lambda token: not auth_enabled or sessions.valid(token)),
        auth_required=auth_enabled,
    ))  # WS /ws
    app.include_router(build_screen_router(lambda: app.state.service, lambda: app.state.notifier))  # GET / + /notify
    app.include_router(build_sveltekit_router(settings.assets_dir))     # GET  /graphs, /_app/{path}, /favicon.ico
    app.include_router(build_media_router(lambda: app.state.service))  # GET  /{path} — LAST

    return app
