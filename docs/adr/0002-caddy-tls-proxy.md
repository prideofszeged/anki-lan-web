# 0002 - Caddy as the in-repo TLS proxy (SPEC D2)

Date: 2026-10-03 · Status: accepted

## Context
SPEC §13 wants LAN HTTPS on `192.168.1.7:18443` with a pinned proxy in front of `web:8000`.
The legacy Webtop proxy lives outside this repo (`anki-library`).

## Decision
Ship a pinned Caddy service in this repo's Compose with an internal-CA (`tls internal`) site
block. `Host` is preserved (the Origin check in ADR 0003 compares against it) and untrusted
`X-Forwarded-*` are not trusted by the app.

## Consequences
- Config only until cutover: nothing binds `:18443` and UFW is untouched without an explicit go.
- Phones must trust the Caddy root CA (SPEC D5, tracked separately).
