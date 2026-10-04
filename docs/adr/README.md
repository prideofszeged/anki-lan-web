# Architecture Decision Records

One short file per decision: context, decision, consequences. Numbered, never rewritten;
supersede with a new ADR. SPEC §14.3 requires an ADR for any backend/frontend replacement.

| # | Decision | Status |
|---|---|---|
| [0001](0001-wrap-existing-screens.md) | Wrap Anki's screens in mobile chrome instead of a Svelte rewrite | accepted |
| [0002](0002-caddy-tls-proxy.md) | Caddy as the in-repo TLS proxy | accepted |
| [0003](0003-auth-csrf-fail-closed.md) | Opaque sessions, Origin-based CSRF, fail-closed startup | accepted |
| [0004](0004-pylib-import-ratchet.md) | Enforce V6 with an import ratchet, migrate incrementally | accepted |
| [0005](0005-multi-user-runtime-boundary.md) | Isolate identity, storage, runtimes, sessions, and bridge state before account rollout | accepted |
