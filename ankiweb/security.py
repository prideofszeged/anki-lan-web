"""Request-level security helpers: CSRF/origin validation and baseline response headers.

Defense in depth on top of the ``SameSite=Strict`` session cookie (SPEC V8). No client-side
token plumbing is needed: browsers always attach ``Origin`` (or ``Sec-Fetch-Site``) to
state-changing requests, and a cross-site page cannot forge or strip them.
"""
from __future__ import annotations
from collections.abc import Mapping
from urllib.parse import urlsplit

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_SAME_ORIGIN_FETCH_SITES = frozenset({"same-origin", "none"})

# frame-ancestors only: Anki card HTML and the reused frontend rely on inline script/style,
# so a script-src policy would break the reviewer. Clickjacking is the part we can enforce.
_CSP = "frame-ancestors 'self'"
_HSTS = "max-age=31536000"


def _same_origin(netloc: str, host: str) -> bool:
    netloc = netloc.lower()
    return bool(netloc) and netloc == host.lower()


def origin_ok(method: str, headers: Mapping[str, str], host: str, extra: tuple[str, ...] = (),
              *, has_session: bool = False) -> bool:
    """False when a state-changing request looks cross-origin.

    An explicit ``Origin``/``Referer`` must match the ``Host`` the request was sent to (or an
    explicitly listed allowed host; localhost is *not* implicitly trusted as an origin).
    A request carrying a live session but no browser signals at all is rejected, since every
    real browser sends one; session-less script clients (curl, health checks) pass.
    """
    if method.upper() in SAFE_METHODS:
        return True
    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site.lower() not in _SAME_ORIGIN_FETCH_SITES:
        return False
    origin = headers.get("origin")
    if origin is not None:
        return origin != "null" and _same_origin(urlsplit(origin).netloc, host)
    referer = headers.get("referer")
    if referer:
        return _same_origin(urlsplit(referer).netloc, host)
    return not has_session


def security_headers(*, hsts: bool = False) -> dict[str, str]:
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
        "X-Frame-Options": "SAMEORIGIN",
        "Content-Security-Policy": _CSP,
        "Permissions-Policy": "camera=(), geolocation=(), payment=()",
    }
    if hsts:
        headers["Strict-Transport-Security"] = _HSTS
    return headers
