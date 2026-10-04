# 0003 - Opaque sessions, Origin-based CSRF, fail-closed startup (SPEC V8)

Date: 2026-10-03 · Status: accepted

## Context
Auth was already Argon2id + opaque server-side session digests + `SameSite=Strict`. Gaps:
state-changing requests were not origin-checked, response hardening headers were absent,
and the server ran fully open when no password was configured.

## Decision
- `ankiweb/security.py`: unsafe methods must be same-origin by `Sec-Fetch-Site`, else
  `Origin`, else `Referer`, compared to the request `Host` (or an explicit allowed host;
  localhost is not implicitly trusted as an origin). A live session with no browser signal is
  rejected; session-less script clients pass. No per-request token, so the reused Anki
  frontend needs no client changes.
- Every response carries nosniff, `Referrer-Policy: same-origin`, `X-Frame-Options`, a
  `frame-ancestors 'self'` CSP, a minimal Permissions-Policy, and HSTS when secure cookies are on.
  A script-src CSP is deliberately omitted: card HTML and the reused frontend need inline script.
- `python -m ankiweb` refuses to start without `ANKIWEB_PASSWORD(_HASH)`, or with the
  `.env.example` placeholder, unless `ANKIWEB_AUTH_DISABLED=1`. `create_app()` itself stays
  permissive so tests can build unauthenticated apps.

## Consequences
- Sessions remain in memory: a restart logs everyone out. Persisting them to `app.db`
  (SPEC §10) is deferred; acceptable for single-user v1.
- curl with a session cookie must send an `Origin` header.
- `/api/v1` deviates from SPEC section 7 on purpose: `GET /session` returns no `csrfToken`
  (the Origin check needs none) and instead reports `authRequired`; unauthenticated API calls get
  a JSON 401 rather than the login redirect that pages get.
