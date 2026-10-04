from __future__ import annotations

import asyncio
import html
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse

from ankiweb.anki_rpc import build_router as build_rpc_router
from ankiweb.api.v1 import PREFIX as API_PREFIX, build_api_router
from ankiweb.assets import (
    build_media_router,
    build_router as build_assets_router,
    build_sveltekit_router,
)
from ankiweb.auth import LoginLimiter, SessionStore, login_client
from ankiweb.config import Settings, host_allowed
from ankiweb.identity import IdentityDatabase, IdentityRepository, IdentityService
from ankiweb.identity.http import (
    IDENTITY_COOKIE,
    IdentityPrincipal,
    build_identity_http,
)
from ankiweb.screens.routes import build_screen_router
from ankiweb.security import origin_ok, security_headers
from ankiweb.tenancy import (
    ResourceKey,
    RuntimeCapacityError,
    RuntimeRegistry,
    StorageLayout,
    TenantCollectionRuntime,
    TenantContext,
)
from ankiweb.tenancy.request import (
    RequestTenantRuntime,
    bind_request_runtime,
    get_hub,
    get_notifier,
    get_service,
    reset_request_runtime,
)
from ankiweb.tenancy.ws import build_tenant_ws_router


def _login_html(error: bool = False) -> str:
    message = (
        "<p class='error' role='alert'>Invalid username or password</p>"
        if error else ""
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>ankiweb</title><style>"
        ":root{color-scheme:light dark;--bg:#f4f6fa;--card:#fff;--text:#17202a;"
        "--muted:#667085;--border:#d0d5dd;--accent:#2563eb;--error:#b42318}"
        "@media(prefers-color-scheme:dark){:root{--bg:#12151a;--card:#1c2027;"
        "--text:#f3f5f7;--muted:#aab2bf;--border:#3d4551;--accent:#7aa7ff;--error:#ff8a80}}"
        "*{box-sizing:border-box}body{font-family:Inter,system-ui,sans-serif;margin:0;"
        "min-height:100vh;display:grid;place-items:center;padding:24px;background:var(--bg);"
        "color:var(--text)}form{width:min(100%,390px);background:var(--card);padding:32px;"
        "border:1px solid var(--border);border-radius:18px;box-shadow:0 20px 60px rgba(0,0,0,.12)}"
        ".mark{width:48px;height:48px;display:grid;place-items:center;margin-bottom:22px;"
        "border-radius:14px;background:var(--accent);color:#fff;font-weight:800;font-size:22px}"
        "h1{font-size:24px;line-height:1.2;margin:0 0 7px}.intro{color:var(--muted);"
        "margin:0 0 24px;font-size:15px}.field{display:block;margin:14px 0 0;font-size:14px;"
        "font-weight:650}.field span{display:block;margin-bottom:7px}input{font-size:16px;"
        "padding:11px 12px;width:100%;min-height:46px;border:1px solid var(--border);"
        "border-radius:10px;background:var(--card);color:var(--text)}input:focus{outline:3px solid "
        "color-mix(in srgb,var(--accent),transparent 65%);border-color:var(--accent)}"
        "button{font-size:16px;font-weight:700;padding:11px 18px;width:100%;min-height:48px;"
        "margin-top:22px;cursor:pointer;border:0;border-radius:10px;background:var(--accent);"
        "color:#fff}.error{color:var(--error);background:color-mix(in srgb,var(--error),"
        "transparent 90%);padding:10px 12px;border-radius:9px;margin:0 0 16px;font-size:14px}"
        "</style></head><body>"
        f"<form method='post' action='/login'><div class='mark' aria-hidden='true'>A</div>"
        f"<h1>Welcome back</h1><p class='intro'>Sign in to your Anki library.</p>{message}"
        "<label class='field'><span>Username</span><input name='username' "
        "autocomplete='username' autofocus required maxlength='64'></label>"
        "<label class='field'><span>Password</span>"
        "<input type='password' name='password' autocomplete='current-password' "
        "required maxlength='4096'></label><button type='submit'>Sign in</button>"
        "</form></body></html>"
    )


def _account_html(user, sessions, *, csrf_cookie_name: str) -> str:
    can_invite = user.global_role.value in {"owner", "admin"}
    role_options = "<option value='user'>User</option>"
    if user.global_role.value == "owner":
        role_options += "<option value='admin'>Administrator</option>"
    invite = ""
    if can_invite:
        invite = (
            "<section><h2>Invite an account</h2>"
            "<p class='muted'>Create a one-time code. Share it privately; it is shown once.</p>"
            "<form id='invite-form'><label>Username<input name='username' required "
            "maxlength='64' autocomplete='off'></label><label>Role<select name='role'>"
            f"{role_options}</select></label><button type='submit'>Create invitation</button></form>"
            "<div id='invite-result' class='token' hidden><strong>One-time token</strong>"
            "<code></code><button type='button' id='copy-token'>Copy token</button></div></section>"
        )
    rows = "".join(
        "<li><div><strong>Browser session</strong><span>Last used "
        f"{html.escape(item.last_seen_at.isoformat(timespec='minutes'))}</span></div>"
        f"<span class='pill'>{'Current' if index == 0 else 'Active'}</span></li>"
        for index, item in enumerate(sessions)
    ) or "<li>No active sessions</li>"
    cookie = json.dumps(csrf_cookie_name)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>"
        "<title>Account · ankiweb</title><link rel='stylesheet' href='/shell/static/mobile.css'>"
        "<style>.account-page{width:min(100% - 28px,760px);margin:28px auto 70px}"
        ".account-head{display:flex;align-items:center;gap:14px;margin-bottom:20px}"
        ".account-head .account-avatar{width:52px;height:52px;font-size:1.15rem}"
        ".account-head h1{font-size:1.45rem;margin:0}.muted,.account-head p{color:var(--muted);margin:4px 0 0}"
        "section{background:var(--surface);border:1px solid var(--border);border-radius:16px;"
        "padding:20px;margin:14px 0}h2{font-size:1.05rem;margin:0 0 12px}"
        "label{display:block;font-weight:650;font-size:.88rem;margin:12px 0}"
        "input,select{display:block;width:100%;min-height:46px;margin-top:6px;padding:9px 11px;"
        "border:1px solid var(--border);border-radius:9px;background:var(--surface);color:var(--text)}"
        "#invite-form{display:grid;grid-template-columns:1fr 160px auto;gap:10px;align-items:end}"
        "#invite-form label{margin:0}ul{list-style:none;padding:0;margin:0}li{display:flex;"
        "align-items:center;justify-content:space-between;gap:12px;padding:12px 0;border-bottom:1px solid var(--border)}"
        "li:last-child{border:0}li span{display:block;color:var(--muted);font-size:.82rem}"
        ".pill{padding:4px 8px;border-radius:999px;background:var(--accent-soft);color:var(--accent)!important}"
        ".token{margin-top:14px;padding:14px;background:var(--surface-soft);border-radius:10px}"
        ".token code{display:block;overflow-wrap:anywhere;margin:9px 0;font-size:.82rem;user-select:all}"
        ".top-actions{margin-left:auto;display:flex;gap:8px}.top-actions a{min-height:44px;display:inline-flex;"
        "align-items:center;padding:8px 12px;text-decoration:none}.danger{color:var(--danger)}"
        "@media(max-width:639px){.account-page{margin-top:16px}#invite-form{grid-template-columns:1fr}"
        ".account-name{display:block}.top-actions{flex-direction:column;align-items:stretch}}"
        "</style></head><body><main class='account-page'>"
        "<header class='account-head'><span class='account-avatar' aria-hidden='true'>"
        f"{html.escape((user.display_name or user.username)[0].upper())}</span><div><h1>"
        f"{html.escape(user.display_name or user.username)}</h1><p>@{html.escape(user.username)} · "
        f"{html.escape(user.global_role.value.title())}</p></div><div class='top-actions'>"
        "<a href='/deckbrowser'>Back to decks</a><button id='logout' class='danger'>Sign out</button>"
        f"</div></header>{invite}<section><h2>Active sessions</h2><ul>{rows}</ul></section>"
        "</main><script>"
        f"const csrfName={cookie};"
        "function csrf(){const p=document.cookie.split('; ').find(x=>x.startsWith(csrfName+'='));"
        "return p?decodeURIComponent(p.split('=').slice(1).join('=')):''}"
        "document.getElementById('logout').onclick=async()=>{await fetch('/api/v1/auth/logout',"
        "{method:'POST',headers:{'x-csrf-token':csrf()}});location.href='/login'};"
        "const form=document.getElementById('invite-form');if(form)form.onsubmit=async(e)=>{"
        "e.preventDefault();const d=new FormData(form);const r=await fetch('/api/v1/admin/account-invites',"
        "{method:'POST',headers:{'content-type':'application/json','x-csrf-token':csrf()},"
        "body:JSON.stringify({intended_username:d.get('username'),global_role:d.get('role'),"
        "expires_in_seconds:86400})});if(!r.ok){alert('Invitation failed');return}const v=await r.json();"
        "const box=document.getElementById('invite-result');box.hidden=false;box.querySelector('code').textContent=v.token;"
        "document.getElementById('copy-token').onclick=()=>navigator.clipboard.writeText(v.token)};"
        "</script></body></html>"
    )
def create_multi_user_app(settings: Settings) -> FastAPI:
    if not settings.multi_user:
        raise ValueError("multi-user settings required")
    storage = StorageLayout(settings.effective_data_root)
    identity = IdentityService(
        IdentityRepository(IdentityDatabase(storage.app_db)),
        provision=storage.provision_empty_user,
        rollback_provision=storage.discard_provisioned_user,
    )
    registry: RuntimeRegistry[TenantCollectionRuntime] = RuntimeRegistry(
        lambda key: TenantCollectionRuntime(key, storage=storage, base_settings=settings),
        max_active=settings.max_active_runtimes,
        idle_seconds=settings.runtime_idle_seconds,
        wait_seconds=settings.runtime_wait_seconds,
    )
    limiter = LoginLimiter()
    identity_http = build_identity_http(
        identity,
        secure_cookie=settings.secure_cookie,
        attempt_gate=lambda request: limiter.allow(
            login_client(request, settings.trusted_proxy_cidrs)
        ),
        login_succeeded=lambda request: limiter.reset(
            login_client(request, settings.trusted_proxy_cidrs)
        ),
        trusted_proxy_cidrs=settings.trusted_proxy_cidrs,
    )
    ready = False
    upload_locks: dict[str, asyncio.Lock] = {}
    upload_paths = {"/upload_media", "/image-occlusion/upload", "/import/upload"}

    async def maintenance() -> None:
        elapsed = 0
        try:
            while True:
                await asyncio.sleep(2)
                elapsed += 2
                for runtime in await registry.runtime_values():
                    for session_id in runtime.session_ids:
                        active = await asyncio.to_thread(identity.session_active, session_id)
                        if not active:
                            await runtime.close_session_hub(session_id)
                if elapsed >= min(settings.runtime_idle_seconds or 1, 60):
                    elapsed = 0
                    await registry.evict_idle()
        except asyncio.CancelledError:
            pass

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal ready
        storage.prepare()
        await asyncio.to_thread(identity.initialize)
        if not await asyncio.to_thread(identity.repository.has_active_owner):
            raise RuntimeError(
                "multi-user identity is not bootstrapped; run `python -m ankiweb user bootstrap`"
            )
        ready = True
        app.state.identity = identity
        app.state.runtime_registry = registry
        task = asyncio.create_task(maintenance())
        try:
            yield
        finally:
            ready = False
            task.cancel()
            await task
            await registry.drain(wait_seconds=settings.runtime_wait_seconds)

    app = FastAPI(title="ankiweb multi-user", lifespan=lifespan)
    extra_headers = security_headers(hsts=settings.secure_cookie)
    public_paths = {
        "/healthz",
        f"{API_PREFIX}/health/live",
        f"{API_PREFIX}/health/ready",
        f"{API_PREFIX}/auth/login",
        f"{API_PREFIX}/account-invites/accept",
        "/login",
    }

    def is_control_path(path: str) -> bool:
        return path == "/account" or path.startswith((
            f"{API_PREFIX}/auth/",
            f"{API_PREFIX}/admin/",
            f"{API_PREFIX}/account-invites/",
            f"{API_PREFIX}/health/",
        ))

    @app.middleware("http")
    async def tenant_guard(request: Request, call_next):
        host = request.headers.get("host", "")
        path = request.url.path
        token = request.cookies.get(IDENTITY_COOKIE)
        session = await asyncio.to_thread(identity.authenticate, token)
        session_ok = session is not None
        if not host_allowed(host, settings.allowed_hosts):
            response = PlainTextResponse("forbidden host", status_code=403)
        elif not origin_ok(
            request.method, request.headers, host, settings.allowed_hosts,
            has_session=session_ok,
        ):
            response = PlainTextResponse("cross-origin request blocked", status_code=403)
        elif path not in public_paths and not session_ok:
            if path.startswith(API_PREFIX + "/"):
                response = JSONResponse({"detail": "unauthenticated"}, status_code=401)
            else:
                response = RedirectResponse("/login", status_code=303)
        elif session is None or is_control_path(path):
            response = await call_next(request)
        else:
            user = await asyncio.to_thread(identity.repository.get_user, session.user_id)
            if user is None:
                response = JSONResponse({"detail": "unauthenticated"}, status_code=401)
            else:
                try:
                    lease = await registry.acquire(ResourceKey.user(user.id))
                except RuntimeCapacityError:
                    response = JSONResponse(
                        {"code": "runtime_capacity"}, status_code=503,
                    )
                except Exception:
                    response = JSONResponse(
                        {"code": "tenant_unavailable"}, status_code=503,
                    )
                else:
                    runtime = lease.runtime
                    identity_principal = IdentityPrincipal(user=user, session=session)
                    tenant = TenantContext.private_collection(
                        user.id, session_id=session.id,
                    )
                    binding = bind_request_runtime(RequestTenantRuntime(
                        principal=identity_principal,
                        tenant=tenant,
                        runtime=runtime,
                        hub=runtime.hub_for(session.id),
                    ))
                    try:
                        request.state.principal = identity_principal
                        request.state.tenant = tenant
                        if path in upload_paths:
                            lock = upload_locks.setdefault(user.id, asyncio.Lock())
                            async with lock:
                                quota = await asyncio.to_thread(
                                    identity.repository.get_quota, user.id
                                )
                                used = await asyncio.to_thread(
                                    storage.user_usage_bytes, user.id
                                )
                                request.state.upload_limit_bytes = max(
                                    0, min(quota.import_bytes, quota.storage_bytes - used)
                                )
                                content_length = request.headers.get("content-length")
                                declared = (
                                    int(content_length)
                                    if content_length and content_length.isdigit()
                                    else None
                                )
                                if (
                                    request.state.upload_limit_bytes <= 0
                                    or declared is not None
                                    and declared > request.state.upload_limit_bytes + 1024**2
                                ):
                                    response = JSONResponse(
                                        {
                                            "detail": {
                                                "code": "quota_exceeded",
                                                "message": "upload quota exceeded",
                                            }
                                        },
                                        status_code=507,
                                    )
                                else:
                                    response = await call_next(request)
                        else:
                            response = await call_next(request)
                    finally:
                        reset_request_runtime(binding)
                        await lease.release()
        for name, value in extra_headers.items():
            response.headers.setdefault(name, value)
        return response

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get(f"{API_PREFIX}/health/live")
    def live():
        return {"status": "ok"}

    @app.get(f"{API_PREFIX}/health/ready")
    def readiness():
        if not ready:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready"}

    @app.get("/login", response_class=HTMLResponse)
    def login_form():
        return HTMLResponse(_login_html())

    @app.post("/login")
    async def login_submit(request: Request):
        client = login_client(request, settings.trusted_proxy_cidrs)
        if not limiter.allow(client):
            return HTMLResponse(_login_html(error=True), status_code=429)
        form = await request.form()
        grant = await asyncio.to_thread(
            identity.login,
            username=str(form.get("username", "")),
            password=str(form.get("password", "")),
        )
        if grant is None:
            return HTMLResponse(_login_html(error=True), status_code=401)
        limiter.reset(client)
        response = RedirectResponse("/", status_code=303)
        identity_http.set_login_cookies(response, grant.token, grant.csrf_token)
        return response

    @app.get("/account", response_class=HTMLResponse)
    async def account(request: Request):
        session = await asyncio.to_thread(
            identity.authenticate, request.cookies.get(IDENTITY_COOKIE)
        )
        if session is None:
            return RedirectResponse("/login", status_code=303)
        user = await asyncio.to_thread(identity.repository.get_user, session.user_id)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        sessions = await asyncio.to_thread(identity.list_sessions, user.id)
        sessions.sort(key=lambda item: item.id != session.id)
        return HTMLResponse(_account_html(
            user, sessions, csrf_cookie_name=identity_http.csrf_cookie_name,
        ))

    app.include_router(identity_http.router)
    app.include_router(build_api_router(
        get_service,
        SessionStore(),
        limiter,
        settings,
        auth_enabled=True,
        include_platform_routes=False,
    ))

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
    app.include_router(build_assets_router(settings.assets_dir))
    app.include_router(build_rpc_router(get_service, get_hub))
    app.include_router(build_tenant_ws_router(identity, registry, settings.allowed_hosts))
    app.include_router(build_screen_router(get_service, get_notifier))
    app.include_router(build_sveltekit_router(settings.assets_dir))
    app.include_router(build_media_router(get_service))
    return app
