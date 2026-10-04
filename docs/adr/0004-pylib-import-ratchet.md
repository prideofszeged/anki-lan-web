# 0004 - Enforce V6 with an import ratchet

Date: 2026-10-03 · Status: accepted

## Context
SPEC V6/V7 say only the Anki adapter imports `anki`. Fifteen inherited modules still do. A
big-bang move would be risky against 533 tests and a live collection.

## Decision
`tests/test_architecture.py` fails on any new module that imports `anki` outside
`ankiweb/adapters/anki/`, and keeps `domain/` and `application/` free of FastAPI, Starlette
and pylib. The legacy list is an allowlist that may only shrink; stale entries also fail.
New use cases are written against `domain.ports.CollectionGateway`.

## Consequences
+ The violation count is visible and monotonic.
- Migration of each legacy module is separate work, done opportunistically with its tests.
