# Architecture

anki-lan-web is a single-user, browser-first Anki server. It deliberately keeps Anki's
collection engine and compiled web UI while replacing the desktop/Qt host with a small
FastAPI process.

## Boundaries

```text
Browser / installed PWA
        │ HTTP + WebSocket
        ▼
Presentation: screens, RPC routes, bridge
        │
        ▼
Application: use-case orchestration and collection serialization
        │ CollectionGateway
        ▼
Adapter: official anki Python package ── SQLite collection + media directory
```

- `ankiweb/screens`, `ankiweb/anki_rpc`, and `ankiweb/bridge` are presentation adapters.
- `ankiweb/application` is the home for new use cases that span screens or transports.
- `ankiweb/domain/ports.py` defines the collection boundary. New storage or sync adapters
  depend on this contract instead of leaking into HTTP handlers.
- `ankiweb/adapters/anki` exposes the current official-Anki implementation.
- `ankiweb/ankiconnect` is an independent compatibility surface on its own port.

The collection is single-writer. `CollectionService` serializes all access through one
worker, which avoids SQLite and Anki object races even when several browser tabs are open.

## Extension strategy

The first release does not execute arbitrary desktop add-ons: Qt add-ons cannot safely run
inside a headless web process. Future extensions should be capability-based and register
one of a small set of explicit contracts (commands, read-only panels, importers, or event
subscribers). An extension must not receive the raw collection object by default.

Recommended evolution:

1. Move new behavior into application services behind `domain` protocols.
2. Add a versioned `/api/v1` resource for that use case.
3. Add a manifest declaring the exact capabilities required.
4. Run untrusted extensions out of process and communicate over the versioned API.
5. Add multi-user support only after collection ownership, per-user sessions, and separate
   scheduling histories have explicit domain models.

## Data compatibility

The container pins the `anki` package and matching frontend assets to the same release.
Upgrades must update both versions together and pass unit, browser, collection-integrity,
and backup/restore tests before they reach a real collection.
