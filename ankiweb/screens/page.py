from __future__ import annotations
import json
from typing import Sequence

from ankiweb.i18n import tr

# Base dark theme for the server-rendered screens. bootstrap.js adds the
# `night-mode` class to <html> when the persisted preference (or #night hash) is set.
_NIGHT_CSS = (
    "<style>"
    "html.night-mode body{background:#2b2b2b;color:#e0e0e0;}"
    "html.night-mode a{color:#6cb6ff;}"
    "html.night-mode button,html.night-mode input,html.night-mode select,"
    "html.night-mode textarea{background:#3a3a3a;color:#e0e0e0;border-color:#555;}"
    "html.night-mode table,html.night-mode th,html.night-mode td{border-color:#555;}"
    "html.night-mode .zero-count{color:#888;}"
    "html.night-mode #ankiweb-bottomnav{background:#1e1e1e;border-color:#444;}"
    "html.night-mode #ankiweb-bottomnav .tab-item{color:#aaa;}"
    "html.night-mode #ankiweb-bottomnav .tab-item.active{color:#6cb6ff;}"
    "html.night-mode #ankiweb-more-sheet .panel{background:#1e1e1e;border-color:#444;color:#e0e0e0;}"
    "html.night-mode #ankiweb-more-sheet .hdr{border-color:#444;}"
    "html.night-mode #ankiweb-more-sheet .items a,"
    "html.night-mode #ankiweb-more-sheet .items button{color:#e0e0e0;}"
    "</style>"
)

# Always-present top toolbar (Anki's main-window Decks/Add/Browse/Stats, minus Sync).
# Fixed at the top of every server-rendered screen; the body gets matching padding.
_TOOLBAR_CSS = (
    "<style>"
    "#ankiweb-toolbar{position:fixed;top:0;left:0;right:0;height:34px;display:flex;"
    "gap:16px;align-items:center;padding:0 12px;background:#f0f0f0;"
    "border-bottom:1px solid #ccc;z-index:2000;font-size:14px;}"
    "#ankiweb-toolbar a{text-decoration:none;color:#333;}"
    "#ankiweb-toolbar a:hover{text-decoration:underline;}"
    "#ankiweb-toolbar .nm{margin-left:auto;border:0;background:transparent;"
    "cursor:pointer;font-size:16px;}"
    # "Extras" CSS-only dropdown (ankiweb-original features, separate from the Anki port).
    "#ankiweb-toolbar .menu{position:relative;}"
    "#ankiweb-toolbar .menu>.lbl{cursor:default;color:#333;}"
    "#ankiweb-toolbar .menu .sub{display:none;position:absolute;top:100%;left:0;"
    "background:#f0f0f0;border:1px solid #ccc;min-width:170px;flex-direction:column;"
    "padding:4px 0;box-shadow:0 2px 6px rgba(0,0,0,.15);}"
    "#ankiweb-toolbar .menu:hover .sub{display:flex;}"
    "#ankiweb-toolbar .menu .sub a{padding:5px 14px;white-space:nowrap;}"
    "html.night-mode #ankiweb-toolbar{background:#1e1e1e;border-color:#444;}"
    "html.night-mode #ankiweb-toolbar a{color:#ccc;}"
    "html.night-mode #ankiweb-toolbar .menu>.lbl{color:#ccc;}"
    "html.night-mode #ankiweb-toolbar .menu .sub{background:#1e1e1e;border-color:#444;}"
    "body{padding-top:42px;}"
    "</style>"
)

# Choice for Reviewer on compact viewport (SPEC T8a):
# Hide the bottom tab bar on /reviewer so the answer buttons (Show Answer,
# Again/Hard/Good/Easy) sit cleanly at the bottom edge with env(safe-area-inset-bottom)
# padding without overlap. Retain a compact top toolbar on /reviewer with a link back to Decks.
_BOTTOMNAV_CSS = (
    "<style>"
    "#ankiweb-bottomnav{display:none;}"
    "@media(max-width:639px){"
    "#ankiweb-toolbar{display:none;}"
    "#ankiweb-bottomnav{position:fixed;bottom:0;left:0;right:0;height:auto;min-height:48px;"
    "display:flex;align-items:stretch;justify-content:space-around;background:#f0f0f0;"
    "border-top:1px solid #ccc;z-index:2000;padding-bottom:env(safe-area-inset-bottom);}"
    "#ankiweb-bottomnav .tab-item{flex:1 1 0;min-width:44px;min-height:44px;display:flex;"
    "flex-direction:column;align-items:center;justify-content:center;gap:2px;padding:4px 2px;"
    "text-decoration:none;color:#555;font-size:11px;background:transparent;border:0;cursor:pointer;"
    "box-sizing:border-box;font-family:inherit;}"
    "#ankiweb-bottomnav .tab-item svg{width:20px;height:20px;flex-shrink:0;stroke:currentColor;fill:none;}"
    "#ankiweb-bottomnav .tab-item.active{color:#2563eb;font-weight:600;}"
    "#ankiweb-more-sheet[hidden]{display:none;}"
    "#ankiweb-more-sheet{position:fixed;inset:0;z-index:2500;display:flex;flex-direction:column;justify-content:flex-end;}"
    "#ankiweb-more-sheet .backdrop{position:absolute;inset:0;background:rgba(0,0,0,.45);}"
    "#ankiweb-more-sheet .panel{position:relative;background:#f0f0f0;border-top:1px solid #ccc;"
    "border-radius:14px 14px 0 0;padding:12px 16px calc(16px + env(safe-area-inset-bottom));max-height:80vh;overflow-y:auto;}"
    "#ankiweb-more-sheet .hdr{display:flex;align-items:center;justify-content:space-between;padding-bottom:8px;border-bottom:1px solid #ddd;font-weight:600;}"
    "#ankiweb-more-sheet .close{background:transparent;border:0;font-size:18px;min-width:44px;min-height:44px;cursor:pointer;}"
    "#ankiweb-more-sheet .items{display:flex;flex-direction:column;gap:4px;margin-top:8px;}"
    "#ankiweb-more-sheet .items a,#ankiweb-more-sheet .items button{display:flex;align-items:center;gap:10px;min-height:44px;padding:8px 12px;color:#333;text-decoration:none;border-radius:6px;background:transparent;border:0;font-size:14px;width:100%;text-align:left;box-sizing:border-box;cursor:pointer;font-family:inherit;}"
    "body[data-context='reviewer'] #ankiweb-bottomnav,"
    "body[data-context='reviewer'] #ankiweb-more-sheet{display:none !important;}"
    "body[data-context='reviewer'] #ankiweb-toolbar{display:flex !important;position:fixed;top:0;left:0;right:0;min-height:44px !important;height:auto !important;padding:0 max(12px,env(safe-area-inset-left)) !important;align-items:center;justify-content:space-between;z-index:2000;}"
    "body[data-context='reviewer'] #ankiweb-toolbar a:not([href='/deckbrowser']){display:none !important;}"
    "body[data-context='reviewer'] #ankiweb-toolbar .menu{display:none !important;}"
    "body[data-context='reviewer'] #ankiweb-toolbar a[href='/deckbrowser']{display:inline-flex !important;align-items:center;min-height:44px;font-weight:600;}"
    "body[data-context='reviewer'] #ankiweb-toolbar .nm{display:inline-flex !important;}"
    "}"
    "</style>"
)

def _toolbar_html() -> str:
    """Built per request so the labels reflect the active language (a module-level
    constant would freeze to the import-time locale). "Source" + the title= tooltips
    + the 🌙 emoji are ankiweb-specific (keyless) and stay English."""
    return (
        "<div id='ankiweb-toolbar'>"
        f"<a href='/deckbrowser'>{tr.actions_decks()}</a>"
        f"<a href='/add'>{tr.actions_add()}</a>"
        f"<a href='/browse'>{tr.qt_misc_browse()}</a>"
        f"<a href='/graphs'>{tr.qt_misc_stats()}</a>"
        f"<a href='/preferences'>{tr.preferences_preferences()}</a>"
        # qt_accel_tools is "&Tools" (menu accelerator); strip the & for a clean label.
        f"<a href='/tools'>{tr.qt_accel_tools().replace('&', '')}</a>"
        "<a href='/about' title='Source code (AGPL)'>Source</a>"
        # ankiweb-original features (beyond the Anki/AnkiConnect port) live under "Extras".
        "<div class='menu'><span class='lbl' title='ankiweb extras'>Extras ▾</span>"
        "<div class='sub'><a href='/notify'>Push notifications</a></div></div>"
        "<button class='nm' onclick='ankiwebToggleNight()' title='Toggle night mode'>\U0001F319</button>"
        "</div>"
    )


def _bottomnav_html(context: str) -> str:
    """Server-rendered compact bottom tab bar (<nav id='ankiweb-bottomnav'>) and bottom
    sheet for 'More'. 5 tabs: Decks, Study, Add, Browse, More. Active tab indicated by context."""
    decks_active = context == "deckbrowser"
    study_active = context in ("overview", "reviewer", "customstudy", "filtereddeck")
    add_active = context == "add"
    browse_active = context in ("browser", "editor")
    more_active = context in ("graphs", "preferences", "tools", "about", "notify")

    def tab(href: str, label: str, svg: str, active: bool, is_btn: bool = False) -> str:
        cls = "tab-item active" if active else "tab-item"
        if is_btn:
            return (
                f"<button type='button' id='ankiweb-more-btn' class='{cls}' "
                f"aria-haspopup='dialog' aria-expanded='false' aria-controls='ankiweb-more-sheet' "
                f"onclick='ankiwebToggleMore()'>{svg}<span class='tab-label'>{label}</span></button>"
            )
        cur = " aria-current='page'" if active else ""
        return f"<a href='{href}' class='{cls}'{cur}>{svg}<span class='tab-label'>{label}</span></a>"

    decks_svg = (
        "<svg viewBox='0 0 24 24' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
        "<rect x='2' y='7' width='20' height='14' rx='2' ry='2'></rect>"
        "<path d='M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16'></path></svg>"
    )
    study_svg = (
        "<svg viewBox='0 0 24 24' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
        "<polygon points='5 3 19 12 5 21 5 3'></polygon></svg>"
    )
    add_svg = (
        "<svg viewBox='0 0 24 24' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
        "<line x1='12' y1='5' x2='12' y2='19'></line><line x1='5' y1='12' x2='19' y2='12'></line></svg>"
    )
    browse_svg = (
        "<svg viewBox='0 0 24 24' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
        "<circle cx='11' cy='11' r='8'></circle><line x1='21' y1='21' x2='16.65' y2='16.65'></line></svg>"
    )
    more_svg = (
        "<svg viewBox='0 0 24 24' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
        "<circle cx='12' cy='12' r='1'></circle><circle cx='19' cy='12' r='1'></circle>"
        "<circle cx='5' cy='12' r='1'></circle></svg>"
    )

    return (
        "<nav id='ankiweb-bottomnav' aria-label='Navigation'>"
        f"{tab('/deckbrowser', tr.actions_decks(), decks_svg, decks_active)}"
        f"{tab('/overview', tr.decks_study(), study_svg, study_active)}"
        f"{tab('/add', tr.actions_add(), add_svg, add_active)}"
        f"{tab('/browse', tr.qt_misc_browse(), browse_svg, browse_active)}"
        f"{tab('#', tr.studying_more(), more_svg, more_active, is_btn=True)}"
        "</nav>"
        "<div id='ankiweb-more-sheet' class='more-sheet' hidden>"
        "<div class='backdrop' onclick='ankiwebToggleMore(false)'></div>"
        "<div class='panel' role='dialog' aria-modal='true' aria-label='More options'>"
        f"<div class='hdr'><span>{tr.studying_more()}</span>"
        "<button type='button' class='close' onclick='ankiwebToggleMore(false)' aria-label='Close'>✕</button></div>"
        "<div class='items'>"
        f"<a href='/graphs'>{tr.qt_misc_stats()}</a>"
        f"<a href='/preferences'>{tr.preferences_preferences()}</a>"
        f"<a href='/tools'>{tr.qt_accel_tools().replace('&', '')}</a>"
        "<a href='/notify'>Push notifications</a>"
        "<a href='/about' title='Source code (AGPL)'>Source</a>"
        "<button type='button' onclick='ankiwebToggleNight()' title='Toggle night mode'>"
        "\U0001F319 Toggle night mode</button>"
        "</div></div></div>"
    )


def render_page(
    context: str,
    body: str,
    css_files: Sequence[str] = (),
    js_files: Sequence[str] = (),
    toolbar: bool = True,
) -> str:
    """Wrap a server-rendered fragment in a full shell HTML document.

    Sets window.__ankiwebContext BEFORE any script so the Bridge connects to
    /ws?context=<context>. Vendored js_files (served from /_anki/) load BEFORE
    the shell bootstrap.js, so globals they define (e.g. reviewer.js's
    window._showQuestion) exist when the page body's inline script runs.

    `toolbar` adds the always-present top toolbar (Decks/Add/Browse/Stats) and
    the compact bottom navigation bar; pass False for embedded fragments like
    the editor iframe inside the Browser.
    """
    links = "".join(f'<link rel="stylesheet" href="/_anki/{c}">' for c in css_files)
    scripts = "".join(f'<script src="/_anki/{j}"></script>' for j in js_files)
    bar_css = (_TOOLBAR_CSS + _BOTTOMNAV_CSS) if toolbar else ""
    bar_html = (_toolbar_html() + _bottomnav_html(context)) if toolbar else ""
    return (
        "<!doctype html>\n"
        '<html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="theme-color" content="#2563eb">'
        '<link rel="manifest" href="/shell/static/manifest.webmanifest">'
        '<link rel="icon" href="/shell/static/icon.svg" type="image/svg+xml">'
        '<link rel="stylesheet" href="/shell/static/mobile.css">'
        f"<script>window.__ankiwebContext={json.dumps(context)}</script>"
        f"{_NIGHT_CSS}"
        f"{bar_css}"
        f"{links}"
        f"{scripts}"
        '<script src="/shell/static/bootstrap.js"></script>'
        '<script>if("serviceWorker" in navigator){navigator.serviceWorker.register("/shell/static/sw.js")}</script>'
        "</head>"
        f"<body data-context={json.dumps(context)}>{bar_html}{body}</body></html>"
    )
