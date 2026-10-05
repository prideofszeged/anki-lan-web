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
from ankiweb.adapters.anki.collaboration import SubscriptionUpdater
from ankiweb.config import Settings, host_allowed
from ankiweb.identity import (
    IdentityDatabase, IdentityRepository, IdentityService, JobRepository,
)
from ankiweb.identity.http import (
    IDENTITY_COOKIE,
    IdentityPrincipal,
    build_identity_http,
)
from ankiweb.screens.routes import build_screen_router
from ankiweb.security import origin_ok, security_headers
from ankiweb.sharing import ShareRole, SharingRepository, SharingService
from ankiweb.sharing.http import build_sharing_router
from ankiweb.sharing.events import ShareSocketRegistry
from ankiweb.sharing.jobs import SharingJobRunner
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
        "<a href='/shares'>Shared decks</a>"
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


def _shares_html(details, names: dict[str, str], *, csrf_cookie_name: str) -> str:
    cards: list[str] = []
    for detail in details:
        share = detail.share
        members = "".join(
            "<li><span>" + html.escape(names.get(member.user_id, member.user_id)) +
            "</span><small>" + html.escape(member.role.value.title()) + "</small>" +
            ("<button class='remove-member' data-share='" + html.escape(share.id) +
             "' data-user='" + html.escape(member.user_id) + "'>Remove</button>"
             if detail.membership.role is ShareRole.OWNER and member.role is not ShareRole.OWNER
             else "") + "</li>"
            for member in detail.members
        )
        owner_tools = ""
        if detail.membership.role is ShareRole.OWNER:
            owner_tools = (
                "<form class='share-invite' data-share='" + html.escape(share.id) + "'>"
                "<select name='role'><option value='viewer'>Viewer</option>"
                "<option value='editor'>Editor</option></select>"
                "<button type='submit'>Create member invitation</button></form>"
                "<div class='invite-result' hidden><strong>One-time token</strong>"
                "<code></code><button class='copy-token'>Copy</button></div>"
                "<form class='job-form' data-op='provision' data-share='" + html.escape(share.id) + "'>"
                "<label>Source deck ID<input name='deck_id' inputmode='numeric' required></label>"
                "<button>Create workspace</button></form>"
                "<form class='job-form' data-op='publish' data-share='" + html.escape(share.id) + "'>"
                "<button>Publish next release</button></form>"
            )
        operations = (
            owner_tools + "<form class='job-form' data-op='install' data-share='" +
            html.escape(share.id) + "'><label>Release<input name='version' type='number' min='1' required></label>"
            "<label>Mode<select name='mode'><option value='follow'>Follow</option><option value='copy'>Copy</option>"
            "</select></label><button>Install</button></form>"
            "<form class='job-form update-form' data-op='update' data-share='" + html.escape(share.id) +
            "'><label>Installed subscription<select name='subscription_id' required>"
            "<option value=''>Choose a followed deck</option></select></label>"
            "<label>Target release<input name='target_version' type='number' min='1' required></label>"
            "<button type='button' class='mirror-preview'>Preview mirror changes</button>"
            "<button>Update</button></form><div class='job-status' role='status' aria-live='polite'></div>"
            "<div class='conflict-list'></div>"
        )
        cards.append(
            "<article class='share-card'><header><div><h2>" + html.escape(share.name) +
            "</h2><p>" + html.escape(share.state.value.title()) + " · " +
            html.escape(detail.membership.role.value.title()) + "</p></div>"
            "<span class='badge'>" + str(len(detail.members)) + " member" +
            ("s" if len(detail.members) != 1 else "") + "</span></header>"
            "<ul>" + members + "</ul><details><summary>Deck operations</summary>" +
            operations + "</details></article>"
        )
    content = "".join(cards) or (
        "<section class='empty'><h2>No shared decks yet</h2>"
        "<p>Create one to invite viewers or editors.</p></section>"
    )
    cookie = json.dumps(csrf_cookie_name)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>"
        "<title>Shared decks · ankiweb</title>"
        "<link rel='stylesheet' href='/shell/static/mobile.css'><style>"
        ".shares-page{width:min(100% - 28px,820px);margin:24px auto 80px}"
        ".shares-head{display:flex;gap:16px;justify-content:space-between;align-items:center}"
        ".shares-head h1{margin:0;font-size:1.5rem}.shares-head p,.share-card p,.empty p{color:var(--muted)}"
        ".shares-head a{text-decoration:none}.create-share,.share-card,.empty{background:var(--surface);"
        "border:1px solid var(--border);border-radius:16px;padding:18px;margin:14px 0}"
        ".create-share{display:grid;grid-template-columns:1fr auto;gap:10px}.create-share h2{grid-column:1/-1}"
        "input,select{min-height:44px;border:1px solid var(--border);border-radius:9px;"
        "background:var(--surface);color:var(--text);padding:9px 11px}.share-card header,.share-card li{"
        "display:flex;align-items:center;justify-content:space-between;gap:12px}.share-card h2{margin:0}"
        ".share-card p{margin:4px 0}.share-card ul{list-style:none;padding:0;margin:14px 0}"
        ".share-card li{padding:10px 0;border-bottom:1px solid var(--border)}"
        ".share-card li small{margin-left:auto;color:var(--muted)}.badge{padding:5px 9px;"
        "border-radius:999px;background:var(--accent-soft);color:var(--accent);font-size:.8rem}"
        ".share-invite{display:flex;gap:8px}.invite-result{padding:12px;margin-top:10px;"
        "background:var(--surface-soft);border-radius:9px}.invite-result code{display:block;"
        "overflow-wrap:anywhere;margin:8px 0}.remove-member{color:var(--danger)}"
        "details summary{cursor:pointer;min-height:44px;display:flex;align-items:center;font-weight:700}"
        ".job-form{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:12px 0;border-top:1px solid var(--border)}"
        ".job-form label{font-size:.82rem;color:var(--muted)}.job-form input,.job-form select{display:block;width:100%}"
        ".job-status{min-height:32px;padding:8px 0}.job-status.failed{color:var(--danger)}"
        ".conflict-item{border:1px solid var(--border);border-radius:9px;padding:12px;margin:8px 0}"
        ".conflict-item textarea{width:100%;min-height:80px}.conflict-item label{display:block;margin:8px 0}"
        "@media(max-width:600px){.create-share,.job-form{grid-template-columns:1fr}.share-invite{flex-direction:column}}"
        "</style></head><body><main class='shares-page'><header class='shares-head'><div>"
        "<h1>Shared decks</h1><p>Membership and collaboration access.</p></div>"
        "<a href='/account'>Account</a></header><form class='create-share' id='create-share'>"
        "<h2>Create a shared deck</h2><input name='name' maxlength='128' required "
        "placeholder='Shared deck name'><button type='submit'>Create</button></form>" + content +
        "</main><script>" + f"const csrfName={cookie};" +
        "const csrf=()=>decodeURIComponent((document.cookie.split('; ').find(x=>"
        "x.startsWith(csrfName+'='))||'=').split('=').slice(1).join('='));"
        "const headers=()=>({'content-type':'application/json','x-csrf-token':csrf()});"
        "document.getElementById('create-share').onsubmit=async e=>{e.preventDefault();"
        "const name=new FormData(e.currentTarget).get('name');const r=await fetch('/api/v1/shares',"
        "{method:'POST',headers:headers(),body:JSON.stringify({name})});if(r.ok)location.reload();else alert('Create failed')};"
        "document.querySelectorAll('.share-invite').forEach(form=>form.onsubmit=async e=>{"
        "e.preventDefault();const role=new FormData(form).get('role');const r=await fetch('/api/v1/shares/'"
        "+form.dataset.share+'/invitations',{method:'POST',headers:headers(),body:JSON.stringify({role})});"
        "if(!r.ok){alert('Invitation failed');return}const v=await r.json();const box=form.nextElementSibling;"
        "box.hidden=false;box.querySelector('code').textContent=v.token;box.querySelector('.copy-token').onclick=()"
        "=>navigator.clipboard.writeText(v.token)});document.querySelectorAll('.remove-member').forEach(button=>"
        "button.onclick=async()=>{if(!confirm('Remove this member?'))return;const r=await fetch('/api/v1/shares/'"
        "+button.dataset.share+'/members/'+button.dataset.user,{method:'DELETE',headers:headers()});"
        "if(r.ok)location.reload();else alert('Remove failed')});"
        "async function showConflicts(id,card){const host=card.querySelector('.conflict-list');if(host.dataset.job===id)return;"
        "const r=await fetch('/api/v1/jobs/'+id+'/conflicts');if(!r.ok)return;const rows=(await r.json()).conflicts;"
        "if(!rows.length)return;host.dataset.job=id;host.replaceChildren();const manual={};for(const c of rows){"
        "const f=document.createElement('fieldset');f.className='conflict-item';const legend=document.createElement('legend');"
        "legend.textContent='Conflict: '+c.entity_type+' · '+(c.field||c.source_id);f.append(legend);"
        "const label=document.createElement('label');label.textContent='Resolution';const select=document.createElement('select');"
        "for(const [v,t] of [['mine','Keep mine'],['upstream','Use upstream'],['manual','Enter manually']]){const o=document.createElement('option');o.value=v;o.textContent=t;select.append(o)}label.append(select);f.append(label);"
        "const ml=document.createElement('label');ml.textContent='Manual value';const area=document.createElement('textarea');area.disabled=true;ml.append(area);f.append(ml);"
        "select.onchange=()=>area.disabled=select.value!=='manual';const save=document.createElement('button');save.type='button';save.textContent='Save resolution';"
        "save.onclick=async()=>{const body={resolution:select.value,manual_value:select.value==='manual'?area.value:null};"
        "const x=await fetch('/api/v1/jobs/'+id+'/conflicts/'+c.id+'/resolve',{method:'POST',headers:headers(),body:JSON.stringify(body)});"
        "if(x.ok){if(select.value==='manual')manual[c.id]=area.value;else delete manual[c.id];save.textContent='Saved'}};f.append(save);host.append(f)}"
        "const apply=document.createElement('button');apply.type='button';apply.textContent='Apply all resolutions';apply.onclick=async()=>{"
        "const x=await fetch('/api/v1/jobs/'+id+'/continue',{method:'POST',headers:headers(),body:JSON.stringify({manual_values:manual})});"
        "if(x.ok){host.replaceChildren();host.dataset.job='';poll(id,card)}};host.append(apply)}"
        "async function poll(id,card){const box=card.querySelector('.job-status');for(let i=0;i<180;i++){"
        "const r=await fetch('/api/v1/jobs/'+id);if(!r.ok){box.textContent='Unable to read job';return}"
        "const j=await r.json(),p=j.progress||{};box.textContent=[j.state,p.version?'release '+p.version:'',"
        "p.mode||'',j.error_code?('error: '+j.error_code.replaceAll('_',' ')):''].filter(Boolean).join(' · ');"
        "box.classList.toggle('failed',j.state==='failed');if(['succeeded','failed','cancelled'].includes(j.state))return;"
        "if(j.state==='running'&&i>1)await showConflicts(id,card);"
        "await new Promise(x=>setTimeout(x,1000))}}"
        "document.querySelectorAll('.job-form').forEach(form=>form.onsubmit=async e=>{e.preventDefault();"
        "const d=new FormData(form),op=form.dataset.op,s=form.dataset.share;let url,body=null;"
        "if(op==='provision'){url='/api/v1/shares/'+s+'/workspace/provision';body={deck_id:Number(d.get('deck_id'))}}"
        "if(op==='publish')url='/api/v1/shares/'+s+'/releases';if(op==='install'){url='/api/v1/shares/'+s+'/installs';"
        "body={version:Number(d.get('version')),mode:d.get('mode')}}if(op==='update'){url='/api/v1/subscriptions/'"
        "+encodeURIComponent(d.get('subscription_id'))+'/updates';body={target_version:Number(d.get('target_version'))}}"
        "const h=headers();h['idempotency-key']=crypto.randomUUID();const r=await fetch(url,{method:'POST',headers:h,"
        "body:body?JSON.stringify(body):null}),box=form.closest('.share-card').querySelector('.job-status');"
        "if(!r.ok){box.textContent='Request failed';box.classList.add('failed');return}poll((await r.json()).id,form.closest('.share-card'))});"
        "fetch('/api/v1/subscriptions').then(r=>r.ok?r.json():{subscriptions:[]}).then(data=>{for(const form of document.querySelectorAll('.update-form')){"
        "const select=form.elements.subscription_id;for(const sub of data.subscriptions.filter(x=>x.share_id===form.dataset.share)){const o=document.createElement('option');"
        "o.value=sub.id;o.textContent='Release '+sub.installed_release+' · '+sub.mode;select.append(o)}}});"
        "document.querySelectorAll('.mirror-preview').forEach(button=>button.onclick=async()=>{const form=button.form,d=new FormData(form),box=form.closest('.share-card').querySelector('.job-status');"
        "if(!d.get('subscription_id')){box.textContent='Choose a subscription first';return}const r=await fetch('/api/v1/subscriptions/'"
        "+encodeURIComponent(d.get('subscription_id'))+'/mirror-preview?target_version='+encodeURIComponent(d.get('target_version')));"
        "if(!r.ok){box.textContent='Preview unavailable';return}const p=await r.json();box.textContent=p.tombstones.length?"
        "p.tombstones.length+' items will be retired':'No mirror removals'})"
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
    sharing = SharingService(SharingRepository(identity.repository.database))
    update_recovery = SubscriptionUpdater(storage, sharing.repository)
    share_sockets = ShareSocketRegistry()
    registry: RuntimeRegistry[TenantCollectionRuntime] = RuntimeRegistry(
        lambda key: TenantCollectionRuntime(key, storage=storage, base_settings=settings),
        max_active=settings.max_active_runtimes,
        idle_seconds=settings.runtime_idle_seconds,
        wait_seconds=settings.runtime_wait_seconds,
    )
    sharing_jobs = SharingJobRunner(
        jobs=JobRepository(identity.repository.database),
        sharing=sharing.repository,
        storage=storage,
        registry=registry,
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
        await asyncio.to_thread(update_recovery.recover_all)
        if not await asyncio.to_thread(identity.repository.has_active_owner):
            raise RuntimeError(
                "multi-user identity is not bootstrapped; run `python -m ankiweb user bootstrap`"
            )
        ready = True
        app.state.identity = identity
        app.state.runtime_registry = registry
        app.state.sharing_jobs = sharing_jobs
        await sharing_jobs.start()
        task = asyncio.create_task(maintenance())
        try:
            yield
        finally:
            ready = False
            task.cancel()
            await task
            await sharing_jobs.stop()
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
        return path in {"/account", "/shares", "/shares/invite"} or path.startswith((
            f"{API_PREFIX}/auth/",
            f"{API_PREFIX}/admin/",
            f"{API_PREFIX}/account-invites/",
            f"{API_PREFIX}/shares",
            f"{API_PREFIX}/jobs/",
            f"{API_PREFIX}/share-invitations/",
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

    @app.get("/shares", response_class=HTMLResponse)
    async def shares_page(request: Request):
        principal = await identity_http.optional_principal(request)
        if principal is None:
            return RedirectResponse("/login", status_code=303)
        shares = await asyncio.to_thread(sharing.list_shares, principal.user.id)
        details = [await asyncio.to_thread(
            sharing.get_share, actor_user_id=principal.user.id, share_id=share.id,
        ) for share in shares]
        user_ids = {member.user_id for detail in details for member in detail.members}
        users = await asyncio.gather(*(
            asyncio.to_thread(identity.repository.get_user, user_id) for user_id in user_ids
        ))
        names = {
            user.id: (user.display_name or user.username) for user in users if user is not None
        }
        return HTMLResponse(_shares_html(
            details, names, csrf_cookie_name=identity_http.csrf_cookie_name,
        ))

    app.include_router(identity_http.router)
    app.include_router(build_sharing_router(
        sharing, identity_http, connections=share_sockets,
        allowed_hosts=settings.allowed_hosts, job_runner=sharing_jobs,
    ))
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
