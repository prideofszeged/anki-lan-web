# Anki LAN Web — Modular Headless Server Spec

status: draft 0.1
date: 2026-10-03
owner: steven
scope: single-user, LAN-first, browser-native Anki server
source base: `https://github.com/aitsc/ankiweb`
Anki target: `26.09.3`

## 1. Outcome

Replace streamed Anki desktop/Webtop with headless Docker web app.

Goals:

- real HTML/CSS/JS UI; ⊥ VNC, X11, Qt window, desktop environment
- Anki scheduler, collection format, media, templates, review history preserved
- phone-first study UX; desktop-capable management UX
- single-user v1; multi-user-ready isolation model
- modular backend, frontend, deployment, extensions
- safe upgrades, backup, rollback
- LAN HTTPS @ `192.168.1.7:18443`
- future remote access via VPN/reverse proxy; ⊥ direct public exposure

## 2. Current Baseline

Current:

- Compose root: `/home/steven/Applications/anki-library`
- Webtop image: `local/anki-webtop:26.09.3`
- image size: `7.11 GB`
- observed RAM: ~`1.08 GiB`
- local UI: `http://127.0.0.1:18081/`
- LAN UI: `https://192.168.1.7:18443/`
- UFW: `192.168.1.0/24` → `192.168.1.7:18443/tcp` on `enp42s0`
- collection: `4,829` notes, `7,353` cards
- required media: Greek audio + Language Transfer audio

Target budget:

- runtime image ≤ `1.5 GB`
- idle RAM ≤ `512 MiB`
- ⊥ GUI packages in runtime image
- cold ready ≤ `30 s` on current host
- review transition p95 ≤ `250 ms` on LAN

## 3. Scope

### 3.1 v1

- password login/logout
- deck tree + due counts
- review: question, answer, Again/Hard/Good/Easy
- audio playback + replay
- undo, bury, suspend, mark, flag
- add/edit note
- browse/search
- deck options + FSRS settings already supported by target Anki backend
- import `.apkg`/`.colpkg`
- export + download
- stats
- media upload/serve/check
- responsive phone/tablet/desktop UI
- PWA install shell; online use only
- AnkiConnect-compatible internal API
- backups, health checks, logs, upgrade/rollback

### 3.2 future

- controlled extension SDK
- per-user collections
- admin UI
- shared/delegated decks
- notification hooks
- native-client sync integration
- offline review queue + conflict resolution
- optional Tailscale/remote HTTPS

### 3.3 non-goals v1

- desktop Qt add-on compatibility
- arbitrary Python add-ons
- public SaaS
- anonymous users
- offline review
- simultaneous review on ≥2 devices
- write access from both Webtop + headless app
- direct SQL access from UI/plugins

## 4. Core Invariants

V1: ∀ collection mutations → `CollectionGateway` single serialized worker.

V2: one collection path → ≤1 writer process.

V3: Webtop running against collection ⇒ headless writer stopped; inverse true.

V4: upgrade/migration → verified backup before first write.

V5: Anki dependency + vendored frontend versions match exactly.

V6: domain/UI code ∉ direct `anki.Collection`, SQLite, protobuf access.

V7: ∀ backend calls → typed application service → `CollectionGateway`.

V8: ∀ browser state-changing requests → authenticated session + CSRF check.

V9: AnkiConnect API → loopback/container network by default; explicit opt-in for LAN.

V10: plugin → versioned capability API; ⊥ direct DB/filesystem/process access by default.

V11: user isolation → one process/worker + collection/media root per user.

V12: backup restore test ! pass before Webtop retirement.

V13: source modifications served via `/about/source` per AGPL obligations.

V14: secrets ∉ image, Git, logs, generated support bundles.

V15: collection downgrade ⊥ without disposable copy + compatibility proof.

## 5. Architecture

```text
phone / tablet / desktop browser
              |
          HTTPS :18443
              |
      reverse proxy / TLS
              |
        web app :8000
   +----------+-----------+
   | auth/session          |
   | web/PWA static UI     |
   | /api/v1 REST          |
   | /ws event bridge      |
   +----------+-----------+
              |
       application services
   +----------+-----------+
   | review | decks | notes|
   | media  | import/export|
   | stats  | extensions   |
   +----------+-----------+
              |
       CollectionGateway
        serialized worker
              |
      AnkiAdapter 26.09.3
       anki pylib + rslib
              |
   collection.anki2 + media/
```

### 5.1 Process model

- one app process v1
- one collection worker thread/process
- async HTTP/WebSocket edge
- long tasks → job service + progress events
- in-process event bus v1
- durable job/outbox adapter future
- ⊥ horizontal app replicas sharing one collection

### 5.2 Repository layout

```text
headless/
  apps/
    server/
      api/                 # HTTP/WS transport
      auth/                # sessions, CSRF, password
      application/         # use cases
      domain/              # transport/Anki-independent types
      adapters/
        anki/              # only Anki-specific imports
        storage/           # app metadata + settings
        events/            # in-process event bus
      modules/
        review/
        decks/
        notes/
        browser/
        media/
        import_export/
        stats/
        extensions/
    web/
      src/
        app/               # shell, routes, nav
        features/          # module UI
        components/        # shared UI primitives
        contracts/         # generated API types
        legacy/            # temporary upstream UI bridge
      public/
  packages/
    contracts/             # OpenAPI/protobuf schemas
    extension-sdk/
  deploy/
    Dockerfile
    compose.yaml
    proxy/
  migrations/
  tests/
    fixtures/
    contract/
    integration/
    e2e/
  docs/
    adr/
    operations/
```

Rule: dependency direction `transport → application → domain`; adapters implement domain ports.

## 6. Backend Modules

### 6.1 `CollectionGateway`

Owns:

- open/close collection
- serialized call queue
- transaction boundary
- mutation timestamp
- backup coordination
- long-operation progress
- graceful shutdown

Interface:

```python
class CollectionGateway(Protocol):
    async def query(self, op: CollectionQuery[T]) -> T: ...
    async def mutate(self, op: CollectionMutation[T]) -> T: ...
    async def backup(self, reason: str) -> BackupRef: ...
```

### 6.2 `AnkiAdapter`

Only module allowed to import `anki`, call rslib/protobuf, inspect Anki schema.

Responsibilities:

- map domain DTO ↔ Anki objects
- reviewer state lifecycle
- scheduler answer operations
- deck/note/card queries
- media manager operations
- import/export
- stats/FSRS jobs
- compatibility probe

Version boundary:

```text
AnkiAdapterV260903
  ├─ scheduler.py
  ├─ collection.py
  ├─ media.py
  ├─ importer.py
  ├─ exporter.py
  └─ frontend_bridge.py
```

Future Anki upgrade → new adapter version or isolated compatibility patch; domain/API stable.

### 6.3 application services

- `ReviewService`
- `DeckService`
- `NoteService`
- `BrowserService`
- `MediaService`
- `ImportExportService`
- `StatsService`
- `SettingsService`
- `BackupService`
- `ExtensionService`

Each service:

- typed input/output
- auth policy hook
- no framework request/response objects
- no direct filesystem except declared port
- emits domain events after successful mutation

## 7. API Contracts

Base: `/api/v1`

```text
api: POST /auth/login → 204 + session cookie
api: POST /auth/logout → 204
api: GET /session → 200 {user,csrfToken,capabilities}

api: GET /decks → 200 {decks[],counts}
api: GET /decks/{id} → 200 {deck,counts,description}

api: POST /review/sessions → 201 {sessionId,card}
api: POST /review/sessions/{id}/reveal → 200 {card,buttons[]}
api: POST /review/sessions/{id}/answer → 200 {nextCard,progress}
api: POST /review/sessions/{id}/undo → 200 {card,state}
api: POST /review/sessions/{id}/actions → 200 {state}

api: GET /notes?query=... → 200 {items[],cursor}
api: POST /notes → 201 {noteId,cardIds[]}
api: PATCH /notes/{id} → 200 {note}
api: DELETE /notes/{id} → 204

api: POST /imports → 202 {jobId}
api: POST /exports → 202 {jobId}
api: GET /jobs/{id} → 200 {state,progress,result}

api: GET /media/{name} → 200 binary
api: POST /media → 201 {name,url}
api: GET /stats → 200 {...}
api: GET /health/live → 200
api: GET /health/ready → 200 | 503

ws: /ws → authenticated events
event: review.updated
event: collection.changed
event: job.progress
event: job.completed
event: session.revoked
```

Contract rules:

- OpenAPI generated from server types
- TypeScript clients generated; ⊥ hand-copied DTOs
- additive changes allowed within `/v1`
- breaking change → `/v2` or explicit compatibility shim
- mutation endpoints accept idempotency key where retry risk exists

## 8. Frontend

Stack:

- Svelte + TypeScript + Vite
- feature modules mirror backend modules
- PWA manifest + service worker for shell/assets
- WebSocket event client
- responsive design tokens + accessible primitives
- Anki card HTML rendered in sandboxed reviewer surface
- vendored Anki reviewer/editor assets behind `legacy/` adapter

### 8.1 mobile UX

Breakpoints:

- compact: `< 640 px`
- medium: `640–1023 px`
- wide: `≥ 1024 px`

Compact review:

- card uses available viewport minus nav/actions
- answer buttons fixed bottom, ≥ `44×44 px`
- safe-area padding via `env(safe-area-inset-*)`
- swipe optional; buttons canonical
- tap card/audio ⊥ accidental answer
- portrait + landscape
- text zoom to `200%` without action loss
- dark mode

Compact navigation:

- bottom tabs: Decks, Study, Add, Browse, More
- destructive actions behind overflow + confirmation
- browser: list → detail drill-down, not fixed split pane
- editor: stacked fields, sticky Save

### 8.2 PWA limits

- installable home-screen shell
- static assets cached
- collection operations require server
- offline page shows connectivity state
- ⊥ queue review answers offline v1

## 9. Authentication + Security

Auth v1:

- single local user
- password hash: Argon2id
- session: random opaque token; server-side hashed record
- cookie: `HttpOnly`, `Secure`, `SameSite=Strict`, path `/`
- session expiry + logout-all
- login rate limit
- recovery via local CLI, not email

Required controls:

- CSRF token on mutations
- origin + host validation
- WebSocket session validation
- CSP; no unrestricted remote scripts
- upload type/size/path validation
- normalized media filenames
- no shell interpolation from requests
- structured audit records for login, import, export, delete, plugin change
- AnkiConnect key independent from UI password
- proxy strips untrusted forwarding headers
- UFW exact LAN source/destination/port rule retained

TLS:

- v1: LAN HTTPS, local/self-signed CA accepted on owned devices
- future: trusted domain or Tailscale certificate
- HTTP listener loopback-only or redirect-only

## 10. Data Model + Storage

Persistent paths:

```text
/data/anki/collection.anki2
/data/anki/media/
/data/app/app.db
/data/backups/
/data/import-tmp/
```

`collection.anki2` + `media/`: owned by Anki backend.
`app.db`: auth sessions, app settings, jobs, extension registry, audit.

Rules:

- app metadata ∉ Anki DB custom tables
- temp imports same filesystem only when atomic move needed
- media served by logical name after traversal check
- no live raw file backup while collection mutation active
- backup includes collection, media manifest, app DB, versions, checksums

## 11. Multi-user-Ready Design

v1 user count: `1`.

Future isolation:

```text
/data/users/{user_id}/anki/collection.anki2
/data/users/{user_id}/anki/media/
/data/users/{user_id}/app/
```

Runtime:

- `TenantRouter` maps authenticated user → `CollectionRuntime`
- one serialized worker per active user
- idle runtime eviction after safe close
- resource quota per user
- ⊥ shared collection writes across users
- shared deck feature uses import/copy or explicit service; ⊥ shared SQLite file

v1 code requirements:

- session exposes `user_id`
- application context carries `user_id`
- storage paths resolved by `UserStorage` interface
- default user seeded as `local`
- ⊥ hard-coded global collection singleton outside runtime registry

## 12. Extension Architecture

Desktop add-ons ≠ supported plugins.

Manifest:

```json
{
  "id": "example.plugin",
  "version": "1.0.0",
  "api": "1",
  "backend": "example_plugin:create_plugin",
  "frontend": "/plugins/example.plugin/index.js",
  "capabilities": ["review.read", "ui.reviewer_action"]
}
```

Extension points:

- `review.card.presented`
- `review.card.answered`
- `collection.changed`
- `deck.menu`
- `reviewer.action`
- `note.editor.field_tool`
- `settings.page`
- `notification.sink`

Rules:

- plugin API versioned independently
- explicit capability allowlist
- disabled by default after incompatible core upgrade
- frontend loaded only from installed local package
- backend hooks timeout + error isolation
- mutation via application services only
- plugin schema migrations namespaced
- uninstall preserves optional data export
- future third-party plugin sandbox required before untrusted plugins

v1: internal extensions use same contracts; public install UI deferred.

## 13. Deployment

Compose target:

```text
services:
  web:
    image: local/anki-lan-web:<version>
    internal port: 8000
    volumes: data, backups
    user: non-root
    read_only: true
    tmpfs: /tmp

  proxy:
    image: pinned Caddy or nginx
    bind: 192.168.1.7:18443
    upstream: web:8000
    volumes: TLS config/certs read-only

  library:
    existing deck catalog
    bind: 127.0.0.1:18080
```

Runtime restrictions:

- pinned image digests
- `no-new-privileges:true`
- dropped Linux capabilities
- ⊥ Docker socket mount
- health checks
- graceful stop ≥ collection close timeout
- restart `unless-stopped`
- log rotation

## 14. Upgrade Strategy

### 14.1 dependency classes

1. app patch: same API + same Anki adapter
2. frontend patch: same backend contract
3. Anki upgrade: new pinned `anki` + matching web assets
4. schema migration: app DB and/or Anki collection
5. extension API change

### 14.2 Anki upgrade gate

∀ target Anki version:

1. build isolated adapter branch
2. vendor exact matching frontend assets
3. copy latest production backup → disposable test volume
4. run compatibility probe
5. verify counts, schemas, media, queue, templates, review actions
6. run full contract/integration/E2E suite
7. export test `.colpkg`; import into disposable official Anki target
8. publish migration notes + rollback boundary
9. create pre-upgrade backup
10. deploy; smoke test; retain prior image + data backup

Rollback:

- app-only failure + no collection migration → prior image
- collection write/migration occurred → stop app, restore pre-upgrade backup, then prior image
- ⊥ run older Anki binary on upgraded live collection

### 14.3 upstream tracking

- record upstream commit in `UPSTREAM.md`
- keep upstream bridge patches isolated
- monthly check; ⊥ auto-deploy dependency upgrades
- CI compatibility matrix: current production + next candidate
- ADR required for backend/frontend replacement

## 15. Backup + Recovery

Schedules:

- before import/upgrade: required
- daily: collection + app DB + media delta/manifest
- weekly: full bundle
- retention: `7` daily, `4` weekly, `6` monthly

Backup manifest:

```json
{
  "created_at": "ISO-8601",
  "app_version": "...",
  "anki_version": "26.09.3",
  "schema_versions": {},
  "note_count": 4829,
  "card_count": 7353,
  "media_count": 0,
  "sha256": {}
}
```

Recovery test:

- restore into disposable volume
- start app read-only/probe mode
- verify manifest + DB integrity + sample media decode
- run quarterly and before Webtop removal

## 16. Observability

Logs:

- JSON to stdout
- request ID, session ID hash, user ID, module, latency, status
- ⊥ password, session token, card field content, media bytes

Health:

- live: process/event loop
- ready: collection open + worker responsive + migrations current
- deep diagnostic: authenticated local CLI only

Metrics future:

- request latency/error rate
- collection queue depth
- review answer latency
- job duration/failure
- active WebSockets
- backup age

## 17. Testing

### 17.1 unit

- domain/application services with fake ports
- auth/session/CSRF
- path/media validation
- extension permissions

### 17.2 contract

- OpenAPI snapshot
- TypeScript client compile
- `AnkiAdapter` behavior fixtures
- upstream frontend bridge messages

### 17.3 integration

- real disposable collection
- import/export round trip
- media + audio
- backup/restore
- upgrade probe
- collection single-writer enforcement

### 17.4 E2E

Playwright viewports:

- iPhone compact portrait/landscape
- Android compact portrait
- tablet
- desktop

Critical flows:

- login
- select deck
- complete review card with audio
- undo
- add/edit/search note
- import deck
- export deck
- logout/session expiry

### 17.5 migration acceptance

M1: notes = `4,829`.

M2: cards = `7,353`.

M3: deck tree + scheduling counts match source snapshot.

M4: due/new/learning queue sample matches Anki `26.09.3`.

M5: Greek Unicode fields round-trip unchanged.

M6: Language Transfer audio decodes + plays on iOS/Android browser.

M7: card templates/CSS render without missing local media.

M8: review answer persisted after restart.

M9: `.colpkg` export imports into official Anki `26.09.3` disposable profile.

M10: backup restore reproduces M1–M9.

## 18. Delivery Plan

Status: `x` done, `~` active, `.` todo.

|id|status|task|exit gate|
|---|---|---|---|
|T0|x|preserve current Webtop + backups|current service healthy|
|T1|x|record baseline counts/media/LAN/firewall|§2 complete|
|T2|.|create Git repo + import upstream source with attribution|clean reproducible checkout|
|T3|.|add architecture skeleton + ADRs|module boundaries compile|
|T4|.|multi-stage Docker build + Compose|headless image healthy on loopback|
|T5|.|upgrade upstream pin `25.9.4` → `26.09.3`|adapter/asset versions match; tests pass|
|T6|.|create sanitized compatibility fixture + private production-copy test|M1–M10 pass on copy|
|T7|.|implement `/api/v1` + generated TS contracts|contract suite pass|
|T8|.|build mobile shell + deck/review flows|phone E2E review pass|
|T9|.|auth/security/proxy hardening|security checklist pass|
|T10|.|notes/browser/editor/import/export/stats responsive flows|feature E2E pass|
|T11|.|backup/restore/upgrade automation|recovery drill pass|
|T12|.|parallel LAN pilot on alternate port|≥7 days stable; no source writes|
|T13|.|cutover `18443`; Webtop read-only fallback|acceptance + user signoff|
|T14|.|retire Webtop runtime; retain recovery bundle|30-day stable window|
|T15|.|publish extension API v1|capability + compatibility tests|
|T16|.|multi-user implementation|per-user isolation/load tests|
|T17|.|optional sync/offline research|ADR + conflict model|

## 19. Phase Detail

### P0 — foundation

Tasks: T2–T4.
Output: reproducible headless container; no production collection write.

### P1 — compatibility

Tasks: T5–T7.
Output: Anki `26.09.3` adapter + stable API contracts + fixture suite.

Stop condition: any unsupported collection downgrade/migration risk → ⊥ production pilot.

### P2 — study MVP

Tasks: T8–T9.
Output: secure phone-first daily review, audio, undo, deck selection.

### P3 — full personal server

Tasks: T10–T11.
Output: common desktop management flows + tested recovery.

### P4 — migration

Tasks: T12–T14.
Output: headless server owns production collection; Webtop removed from runtime.

### P5 — platform

Tasks: T15–T17.
Output: extensions, users, optional sync/offline based on demand.

## 20. Cutover Runbook

1. notify/close browser sessions
2. stop Webtop
3. create checksum backup
4. verify Webtop Anki process stopped
5. copy collection + media to new production volume
6. run migration probe + M1–M10
7. start headless stack on loopback
8. smoke test auth/review/audio/export
9. switch proxy upstream while retaining `192.168.1.7:18443`
10. verify UFW unchanged
11. monitor logs/backup for `24 h`
12. keep Webtop stopped, data immutable fallback

Rollback trigger:

- collection integrity failure
- scheduler mismatch
- missing/corrupt media
- repeated review mutation error
- backup failure

Rollback action: stop headless writer → restore pre-cutover backup → start prior Webtop → verify counts.

## 21. Risks

|id|risk|control|
|---|---|---|
|R1|upstream browser port pinned to `25.9.4`|T5 adapter upgrade + exact assets|
|R2|collection corruption via dual writers|V2,V3 + lock/probe|
|R3|mobile UI inherits desktop assumptions|new shell + feature modules; legacy bridge isolated|
|R4|Anki internal APIs change|`AnkiAdapter` boundary + contract fixtures|
|R5|plugin compromises data|capabilities; no direct DB; trusted-only v1|
|R6|self-signed TLS friction|managed local CA; future trusted VPN/domain|
|R7|single-process bottleneck|acceptable single-user; per-user worker future|
|R8|AGPL/trademark obligations|source page; retain notices; distinct product name before distribution|
|R9|offline expectation mismatch|online-only UI explicit; offline separate ADR|
|R10|phone + desktop answer same card|review session lease; reject stale answer version|

## 22. Definition of Done — v1 Cutover

- T2–T13 complete
- M1–M10 pass
- no Qt/X11/VNC/desktop packages
- phone E2E critical flows pass
- image/RAM/start/latency budgets pass
- auth + CSRF + TLS + host/origin checks pass
- AnkiConnect external bind disabled
- backup + restore drill pass
- exact LAN firewall restriction retained
- rollback tested
- `/about` shows versions, licenses, source
- operations docs cover start/stop/update/backup/restore/recovery

## 23. Decision Queue

Decide before T4:

- D1: internal project name; avoid `AnkiWeb` official-service confusion
- D2: Caddy vs existing nginx TLS proxy
- D3: upstream fork strategy: Git fork vs vendored subtree

Decide before T8:

- D4: full custom reviewer shell vs Anki reviewer wrapped by mobile chrome
- D5: local CA trust workflow for phones

Decide before T15:

- D6: plugin process isolation level
- D7: public extension signing/distribution

Decide before T16:

- D8: user provisioning model
- D9: per-user process vs worker pool

## 24. References

- unofficial browser port: `https://github.com/aitsc/ankiweb`
- official Anki source: `https://github.com/ankitects/anki`
- official sync server docs: `https://docs.ankiweb.net/sync-server.html`
- AnkiConnect: `https://github.com/FooSoft/anki-connect`
