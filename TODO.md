# TODO

Source of truth for the roadmap is `SPEC.md` §18. This file tracks the working plan for the
current run. Scope agreed 2026-10-03: **T3–T11**, stop before the pilot (T12–T14).
Frontend approach (D4, ADR 0001): wrap existing Anki screens in mobile chrome, thin `/api/v1`.

Owners: **CC** = Claude Code (main checkout, `feat/modular-mobile-v1`),
**agy** = Antigravity CLI (worktree `../anki-lan-web-agy`, branch `agy/work`).
File ownership was disjoint per batch so the two branches merge cleanly.

Test counts: 533 at start of run -> **632** on main (CC work); agy branch 548 (533 + 15). Verified in a
throwaway combined tree (agy/work + CC uncommitted files, no overlapping paths): **647 passed**.

## Done (CC, main checkout, UNCOMMITTED)
- [x] T3 ADRs 0001-0004 (`docs/adr/`) + import-boundary ratchet test (`tests/test_architecture.py`)
- [x] T9a fail-closed startup, Origin/Sec-Fetch-Site CSRF check, baseline security headers
      (`ankiweb/security.py`); real-browser proof incl. mutation check (`tests/test_security_integration.py`)
- [x] T7 thin `/api/v1`: health live/ready, session, JSON login/logout, decks tree + detail;
      domain/application/adapter layering; OpenAPI snapshot gate (`packages/contracts/openapi-v1.json`)
- [x] T6 M1-M10 acceptance tool (`ankiweb/adapters/anki/acceptance.py`) + generated sanitized fixture;
      run on a production-scale copy: 4829 notes / 7353 cards / 12256 media, M1-M9 pass, M7 WARN
- [x] T9b Caddy front behind `--profile lan` (config only; nothing binds :18443 by default)
- [x] T11 backup bundle + manifest + sha256, 7/4/6 retention, restore drill (R1-R3 + M1-M10),
      `scripts/backup.sh`, `scripts/restore-drill.sh`; drill passes on the production-scale copy (8 s)
- [x] Bug fix: WebSocket receive loop hot-spun on a dead socket (`WebSocketDisconnected` is a
      `RuntimeError`); hub no longer lets one dead socket abort a broadcast
- [x] `faulthandler` SIGUSR1 stack dump for hung servers; `docs/OPERATIONS.md` rewritten

## Done (agy, branch agy/work, committed, NOT merged)
- [x] T8a bottom tab bar, reviewer bottom answer bar, safe areas, More sheet, dark mode
- [x] T8b Playwright viewport E2E (iPhone portrait/landscape, Pixel, iPad, desktop)
- [x] T10 Browse compact drill-down; editor stacked fields + sticky Save
- [x] Mobile chrome for SvelteKit pages (graphs, deck-options, ...); nav CSS de-duplicated
- [x] T7b generated TS client from the OpenAPI snapshot + drift test

## Needs Steven
- [ ] Commit CC work on `feat/modular-mobile-v1` and merge `agy/work` (commits are held back on purpose)
- [ ] LIVE `anki-lan-web` container is HUNG (100% CPU, unhealthy since 17:10Z, ~1h CPU burned).
      Root cause NOT proven (see below). Recovery: `./scripts/pilot.sh` (rebuilds with the fixes).
- [ ] Decide: add `2-seconds-of-silence.mp3` to media? ("Greek Multi" answer side references it; it is
      missing in the ORIGINAL Webtop library too, so this is pre-existing, M7 reports it as WARN)
- [ ] Decide: sessions in memory (restart = re-login) acceptable for v1, or persist to `app.db` (SPEC §10)?
- [ ] Pin the Caddy image digest and run `caddy validate` (not done: no image pulled)
- [ ] Manual checks automation can't do: M6 playback on iOS/Android, M9 import into desktop Anki GUI
- T12-T14 pilot, cutover (:18443), Webtop retirement: operational, out of scope for this run
- UFW / binding 192.168.1.7:18443: not touched

## Remaining engineering (after merge)
- [ ] CI: run `npm run typecheck`, `npm run gen:api` drift check, `tests/e2e`; add `docker compose config`
- [ ] Combined full-suite run on the merged tree; rebuild image; `scripts/verify.sh`
- [ ] Scheduled daily/weekly `backup.sh` (systemd timer or cron) - tool exists, schedule does not
- [ ] Real-device phone pass (emulation only so far)
- [ ] Migrate the 15 legacy `import anki` modules behind `CollectionGateway` (ratchet in test_architecture.py)
- [ ] Review/answer endpoints in `/api/v1` (only health/session/auth/decks exist)

## Hang investigation (open)
Last request before the hang: `GET /about` then `WS /ws?context=about` at 17:10:36Z, after ~12 s of
rapid Browse->Add->Decks->Tools->About clicks; no `connection closed` logged for any earlier socket.
Fixed a real spin bug in that code path, but a 120-navigation stress run against the ORIGINAL code did
not reproduce a hang, so the fix is not proven to be THE cause. If it recurs on the rebuilt image:
`docker kill -s USR1 anki-lan-web; docker logs --tail 80 anki-lan-web`.

## Later (T15-T17)
- [ ] T15 extension API v1  - [ ] T16 multi-user  - [ ] T17 sync/offline research
