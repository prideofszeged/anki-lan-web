# 0005 - Multi-user runtime boundary (SPEC §11)

Date: 2026-10-03 · Status: accepted

## Context

Single-user app owns one global collection, bridge, reviewer session, UI state, media root,
notifier, backup root, and AnkiConnect key. Route-only user checks cannot isolate these objects.
Anki locale also remains process-global.

## Decision

- durable `app.db` owns identity/control metadata; Anki collections own card/note/review content
- private resource key always derived from authenticated session; request-supplied user UUID ⊥
- UUID-only `UserStorage`/`ShareStorage`; symlink traversal ⊥; directory/file modes `0700`/`0600`
- `RuntimeRegistry` owns ≤ configured collection runtimes; ref-counted operation leases; OS lock
- one collection worker/resource; one bridge + reviewer/UI state/authenticated browser session
- bridge callbacks owned by exact connection; resource/session partitioned broadcast
- idle WS ⊥ pin runtime; each command acquires lease; active reviewer flow may pin
- locale server-global through MVP-A/B
- multi-user AnkiConnect disabled; future API key binds one private user; share access ⊥
- imports/restores/follow updates use staged same-filesystem copy + journaled atomic swap
- multi-user flag fail-closed until identity bootstrap, legacy migration, and transport switch pass

## Consequences

- legacy single-user path stays operational during staged rollout
- second account unavailable until all transport paths resolve `TenantContext`
- runtime cap controls memory without capping idle logged-in browsers
- one corrupt/locked tenant resource degrades that tenant, not global readiness
- host operator must remove legacy plaintext password config; container cannot edit host `.env`
- ADR 0003 remains authoritative for legacy mode; durable session + CSRF rules supersede its
  in-memory/session-token consequences only in multi-user mode
