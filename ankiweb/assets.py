from __future__ import annotations
from pathlib import Path
from typing import Callable
from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from ankiweb.screens.page import spa_navigation_html

# Injected into the served SvelteKit shell so the SPA's bridgeCommand("browserSearch:<q>")
# (e.g. graphs count-links) opens ankiweb's browser instead of being a no-op. The SPA has no
# pycmd host otherwise; this defines a minimal one before the app modules load. Other bridge
# commands are intentionally ignored (same as before).
_SPA_BRIDGE = (
    "<script>window.pycmd=window.bridgeCommand=function(c){try{"
    "if(typeof c==='string'&&c.indexOf('browserSearch:')===0){"
    "location.href='/browse?q='+encodeURIComponent(c.slice(14));}}catch(e){}};</script>"
)

_SPA_NAV_TAGS = (
    '<link rel="stylesheet" href="/shell/static/spa-nav.css?v=4">'
    '<script src="/shell/static/spa-nav.js?v=4" defer></script>'
)

# Navigation markup is server-injected into a separately-built SvelteKit document.  Keep its
# geometry safe in the HTML itself: an older service worker may briefly have neither matching
# CSS nor JS while the new version activates.  The versioned stylesheet supplies all visual
# polish; this only bounds the shell and prevents viewBox-only SVGs from filling the viewport.
_SPA_NAV_CRITICAL = (
    "<style id='ankiweb-spa-nav-critical'>"
    "body[data-context=graphs]{margin:0;max-width:100%;overflow-x:hidden;"
    "padding-top:calc(52px + env(safe-area-inset-top,0px))}"
    "#ankiweb-toolbar{position:fixed;inset:0 0 auto;z-index:10000;"
    "height:calc(52px + env(safe-area-inset-top,0px));display:flex;align-items:flex-end;gap:8px;"
    "padding:env(safe-area-inset-top,0px) 12px 0;overflow:hidden;background:#f4f6f8;"
    "border-bottom:1px solid #d0d5dd}"
    "#ankiweb-toolbar>a,#ankiweb-toolbar .lbl,#ankiweb-toolbar .nm{display:inline-flex;"
    "align-items:center;min-height:44px;padding:0 10px;white-space:nowrap}"
    "#ankiweb-toolbar .menu .sub,#ankiweb-bottomnav,#ankiweb-more-sheet{display:none}"
    "#ankiweb-bottomnav svg{display:block;width:20px;height:20px;max-width:20px;max-height:20px;"
    "fill:none;stroke:currentColor}"
    "@media(max-width:639px){body[data-context=graphs]{padding-top:0;"
    "padding-bottom:calc(56px + env(safe-area-inset-bottom,0px))}"
    "#ankiweb-toolbar{display:none}#ankiweb-bottomnav{position:fixed;inset:auto 0 0;"
    "z-index:10000;min-height:calc(56px + env(safe-area-inset-bottom,0px));display:flex;"
    "padding-bottom:env(safe-area-inset-bottom,0px);background:#fff;border-top:1px solid #d0d5dd}"
    "#ankiweb-bottomnav .tab-item{flex:1 1 0;min-width:44px;min-height:44px;display:flex;"
    "flex-direction:column;align-items:center;justify-content:center;padding:4px 2px;"
    "border:0;background:transparent;color:#667085;text-decoration:none;font:12px sans-serif}"
    "#ankiweb-bottomnav .active{color:#2563eb}}"
    "</style>"
)

# subset of mediasrv _mime_for_path (mediasrv.py:171-210)
MIME = {
    ".css": "text/css", ".js": "application/javascript", ".mjs": "application/javascript",
    ".html": "text/html", ".svg": "image/svg+xml", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".ico": "image/x-icon", ".json": "application/json", ".woff": "font/woff",
    ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf", ".map": "application/json",
    ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".opus": "audio/opus", ".wav": "audio/wav", ".flac": "audio/flac",
    ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
}
SVELTEKIT_PAGES = {"graphs", "congrats", "card-info", "change-notetype", "deck-options",
                   "import-anki-package", "import-csv", "import-page", "image-occlusion"}


# vendored binary assets that are content-stable across the pinned anki version: cache hard.
# (fonts are the big one — MathJax CHTML lazy-loads ~dozens of woff glyph files per render.)
_STATIC_ASSET_EXTS = {"woff", "woff2", "ttf", "otf", "eot",
                      "svg", "png", "jpg", "jpeg", "gif", "webp", "ico"}


def _mime(path: str) -> str:
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return MIME.get(ext, "application/octet-stream")


def _resolve(rel: str) -> str:
    """Replicate mediasrv _extract_internal_request rewrites for the _anki/ namespace."""
    first = rel.split("/", 1)[0]
    if first in SVELTEKIT_PAGES:
        return f"sveltekit/{rel}"
    if rel.startswith("_app/"):
        return f"sveltekit/{rel}"
    if "/" not in rel:  # bare file at /_anki/<file>
        if rel.endswith(".css"):
            return f"css/{rel}"
        if rel.endswith(".js"):
            stem = rel[:-3].removesuffix(".min")  # jquery.min -> jquery
            if stem in ("jquery", "jquery-ui", "plot"):
                return f"js/vendor/{rel}"
            return f"js/{rel}"
    return rel


def build_router(assets_dir: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/_anki/{path:path}")
    def serve(path: str, request: Request) -> Response:
        rel = _resolve(path)
        target = (assets_dir / rel).resolve()
        try:
            target.relative_to(assets_dir.resolve())
        except ValueError:
            return PlainTextResponse("forbidden", status_code=403)

        if not target.is_file():
            # SvelteKit SPA fallback for non-immutable sveltekit paths
            if rel.startswith("sveltekit/") and "immutable" not in rel:
                fallback = assets_dir / "sveltekit" / "index.html"
                if fallback.is_file():
                    return FileResponse(fallback, media_type="text/html")
            return PlainTextResponse("not found", status_code=404)

        headers = {}
        ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
        if "immutable" in rel:
            headers["Cache-Control"] = "max-age=31536000"
        elif ext in _STATIC_ASSET_EXTS:
            # Vendored, version-pinned, content-stable binaries (esp. MathJax's lazily-loaded
            # CHTML glyph fonts). Without a cache header the browser re-downloads them FULLY on
            # every card render -> slow card switches. They never change at runtime.
            headers["Cache-Control"] = "max-age=31536000"
        elif rel.endswith((".css", ".js")):
            # Vendored frontend bundles (editor.js is 3.5 MB) are version-pinned. Caching them
            # for a day means the Browser's per-card editor iframe (and the reviewer) reuse the
            # cache instead of re-downloading megabytes on every card switch. (max-age=0 forced a
            # revalidation that came back as a full 200 here, defeating the cache.) A re-vendor is
            # picked up within a day, or immediately via a hard refresh.
            headers["Cache-Control"] = "max-age=86400"
        return FileResponse(target, media_type=_mime(rel), headers=headers)

    return router


def build_sveltekit_router(assets_dir: Path) -> APIRouter:
    """Serve the vendored SvelteKit SPA at ROOT paths (its index.html imports /_app/...
    and client-routes by location.pathname). E2/E3 add more page routes here."""
    router = APIRouter()
    index = assets_dir / "sveltekit" / "index.html"

    def _render_shell(context: str, include_bridge: bool = False) -> str:
        html = index.read_text(encoding="utf-8")
        injections = []
        if include_bridge:
            injections.append(_SPA_BRIDGE)
        if context == "graphs":
            injections.append(_SPA_NAV_CRITICAL)
        injections.append(_SPA_NAV_TAGS)
        html = html.replace("<head>", "<head>" + "".join(injections), 1)
        if context == "graphs":
            body = '<body data-sveltekit-preload-data="hover">'
            shell_body = (
                f'<body data-context="{context}" data-sveltekit-preload-data="hover">'
                + spa_navigation_html(context)
            )
            html = html.replace(body, shell_body, 1)
        return html

    @router.get("/graphs")
    def graphs_page() -> Response:
        # served with the browserSearch bridge so the stats count-links open /browse
        return HTMLResponse(_render_shell("graphs", include_bridge=True))

    @router.get("/deck-options/{deck_id}")
    def deck_options_page(deck_id: str) -> Response:
        return HTMLResponse(_render_shell("deck-options"))

    @router.get("/change-notetype/{ids:path}")
    def change_notetype_page(ids: str) -> Response:
        return HTMLResponse(_render_shell("change-notetype"))

    @router.get("/card-info/{ids:path}")
    def card_info_page(ids: str) -> Response:
        # SvelteKit route nodes: /card-info/[cardId] and /card-info/[cardId]/[previousId].
        # Bundle is vendored; card_stats / get_review_logs are already PASSTHROUGH RPCs.
        return HTMLResponse(_render_shell("card-info"))

    @router.get("/import-csv/{path:path}")
    def import_csv_page(path: str) -> Response:
        return HTMLResponse(_render_shell("import-csv"))

    @router.get("/import-anki-package/{path:path}")
    def import_anki_package_page(path: str) -> Response:
        return HTMLResponse(_render_shell("import-anki-package"))

    @router.get("/image-occlusion/{path:path}")
    def image_occlusion_page(path: str) -> Response:
        return HTMLResponse(_render_shell("image-occlusion"))

    @router.get("/_app/{path:path}")
    def app_asset(path: str) -> Response:
        rel = _resolve("_app/" + path)
        target = (assets_dir / rel).resolve()
        try:
            target.relative_to(assets_dir.resolve())
        except ValueError:
            return PlainTextResponse("forbidden", status_code=403)
        if not target.is_file():
            return PlainTextResponse("not found", status_code=404)
        headers = {"Cache-Control": "max-age=31536000"} if "immutable" in rel else {}
        return FileResponse(target, media_type=_mime(rel), headers=headers)

    @router.get("/favicon.ico")
    def favicon() -> Response:
        f = assets_dir / "imgs" / "favicon.ico"
        if f.is_file():
            return FileResponse(f, media_type="image/x-icon")
        return Response(status_code=204)

    return router


def build_media_router(get_service: Callable) -> APIRouter:
    router = APIRouter()

    @router.get("/{path:path}")
    async def serve_media(path: str) -> Response:
        service = get_service()  # lazy: service is created in lifespan, not import time
        media_dir = Path(await service.run(lambda col: col.media.dir())).resolve()
        target = (media_dir / path).resolve()
        try:
            target.relative_to(media_dir)
        except ValueError:
            return PlainTextResponse("forbidden", status_code=403)
        if not target.is_file():
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(target, media_type=_mime(path))

    return router
