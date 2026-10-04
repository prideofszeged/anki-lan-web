# Anki LAN Web — Modular Headless Server Spec

status: draft 0.2
date: 2026-10-03
owner: steven
scope: LAN-first browser-native Anki server; single-user v1; multi-user platform planned
source base: `https://github.com/aitsc/ankiweb`
Anki target: `26.09.3`

## 1. Outcome

Replace streamed Anki desktop/Webtop with headless Docker web app.

Goals:

- real HTML/CSS/JS UI; ⊥ VNC, X11, Qt window, desktop environment
- Anki scheduler, collection format, media, templates, review history preserved
- phone-first study UX; desktop-capable management UX
- single-user v1; concurrent isolated users + controlled deck collaboration next
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

V11: user isolation → one runtime/serialized worker + collection/media root per user.

V12: backup restore test ! pass before Webtop retirement.

V13: source modifications served via `/about/source` per AGPL obligations.

V14: secrets ∉ image, Git, logs, generated support bundles.

V15: collection downgrade ⊥ without disposable copy + compatibility proof.

V16: ∀ HTTP/WS/job → authenticated `actor_user_id` + explicit `resource_owner_id`.

V17: user collection, media, scheduler, review log → private; ⊥ implicit cross-user access.

V18: one collection path → one `CollectionRuntime` + one serialized worker + one process lock.

V19: session token stored hashed; session row bound to one user, expiry, revocation epoch.

V20: quota preflight + postflight required for import, upload, release install, workspace publish.

V21: shared deck release immutable; manifest + bundle SHA-256 required.

V22: collaboration workspace uses separate collection root; ⊥ review/scheduling in workspace.

V23: shared deck install/update preserves recipient scheduling + review history.

V24: invitation token single-use, hashed at rest, scoped, expiring.

V25: admin UI exposes account/usage/audit metadata; ⊥ card/note content impersonation.

V26: account deletion two-phase; immediate suspend + delayed recoverable purge.

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
api: POST /auth/login {username,password} → 204 + session cookie
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

## 11. Multi-user + Deck Collaboration

### 11.1 outcome + boundary

MVP-A: concurrent private accounts; isolated decks, media, scheduler, sessions, jobs, backups, quotas.

MVP-B: administration, invitations, versioned deck sharing, shared authoring workspaces.

Non-goals:

- ⊥ public signup, email delivery, billing, public marketplace
- ⊥ shared live review queue or shared scheduling history
- ⊥ two users writing same private collection
- ⊥ automatic destructive overwrite of subscriber local edits
- ⊥ admin impersonation
- ⊥ native Anki sync protocol in T16–T19

### 11.2 identity + provisioning

Global roles: `owner | admin | user`.

Account states: `invited | active | suspended | purge_pending | purged`.

Provisioning:

1. migration seeds existing account as `local`, role `owner`
2. owner/admin creates account invitation
3. invitation URL/code shown once; ⊥ email dependency
4. invitee selects username + password
5. Argon2id credential stored; token consumed atomically
6. private collection initialized empty or from uploaded package

Rules:

- username normalized + unique; display name non-authoritative
- bootstrap owner creation → local CLI only
- password reset → admin-created one-time reset token or local CLI
- suspend → revoke sessions + reject jobs + close idle runtime
- suspend during mutation → mutation completes/rolls back, then runtime closes
- purge default delay `30 days`; restore allowed before deadline
- last active owner ⊥ suspend/purge/demote

### 11.3 app DB schema

`/data/app/app.db` owns identity/control metadata; ⊥ card/note bodies.

```text
users(id UUID PK, username_norm UNIQUE, display_name, global_role, state,
      created_at, suspended_at, purge_after, auth_epoch)
credentials(user_id PK, password_hash, changed_at)
sessions(id UUID PK, user_id, token_hash UNIQUE, csrf_hash, created_at,
         last_seen_at, expires_at, auth_epoch, user_agent_hash, ip_prefix)
account_invites(id UUID PK, token_hash UNIQUE, created_by, intended_username,
                global_role, expires_at, consumed_at)
password_resets(id UUID PK, token_hash UNIQUE, user_id, expires_at, consumed_at)
user_quotas(user_id PK, storage_bytes, import_bytes, active_jobs,
            active_sessions, review_sockets)
audit_events(id, occurred_at, actor_user_id, target_user_id, action,
             resource_type, resource_id, request_id, outcome, metadata_json)

deck_shares(id UUID PK, owner_user_id, workspace_id, name, state,
            current_release, created_at)
share_members(share_id, user_id, role, state, joined_at, UNIQUE(share_id,user_id))
share_invites(id UUID PK, share_id, token_hash UNIQUE, role, created_by,
              intended_user_id, expires_at, consumed_at)
share_releases(id UUID PK, share_id, version, manifest_path, bundle_path,
               bundle_sha256, created_by, created_at, UNIQUE(share_id,version))
share_subscriptions(id UUID PK, share_id, user_id, mode, installed_release,
                    target_deck_id, conflict_policy, created_at)
workspace_revisions(workspace_id, entity_type, entity_id, revision,
                    changed_by, changed_at)
workspace_comments(id UUID PK, workspace_id, entity_type, entity_id,
                   author_user_id, body, resolved_at, created_at)
```

Migration system: ordered app-schema versions; one transaction/version; startup blocks readiness on failure.

### 11.4 storage layout

```text
/data/app/app.db
/data/users/{user_uuid}/anki/collection.anki2
/data/users/{user_uuid}/anki/collection.media/
/data/users/{user_uuid}/tmp/
/data/users/{user_uuid}/app/
/data/shares/{share_uuid}/anki/collection.anki2
/data/shares/{share_uuid}/anki/collection.media/
/data/shares/{share_uuid}/releases/{version}/manifest.json
/data/shares/{share_uuid}/releases/{version}/deck.apkg
/data/backups/system/
/data/backups/users/{user_uuid}/
/data/backups/shares/{share_uuid}/
```

`UserStorage` + `ShareStorage` resolve UUID → paths. Request data never forms filesystem path.

Rules:

- UUID directory names only; username rename ≠ path move
- owner UID/GID fixed; directories `0700`; files `0600`
- symlink traversal ⊥
- temp + backup roots same user boundary
- cross-user hardlinks/symlinks ⊥
- share release bundle immutable after publish
- quota counts collection, media, temp, private backups over retention floor
- share workspace/releases charged to share owner; member installs charged to member

### 11.5 runtime registry

```python
ResourceKey = UserCollection(user_id) | ShareWorkspace(share_id)

class RuntimeRegistry(Protocol):
    async def acquire(self, key: ResourceKey) -> RuntimeLease: ...
    async def evict(self, key: ResourceKey, reason: str) -> None: ...
    async def drain(self) -> None: ...
```

`RuntimeLease` owns `CollectionRuntime`; ref-counted by HTTP, WS, job.

Lifecycle:

1. resolve `TenantContext`
2. authorize actor → resource
3. acquire runtime slot
4. open collection + process lock if cold
5. serialized operation
6. release lease
7. idle `15 min` + zero lease/job/socket → flush, close, evict

Defaults:

- `ANKIWEB_MAX_ACTIVE_RUNTIMES=4`
- `ANKIWEB_RUNTIME_IDLE_SECONDS=900`
- `ANKIWEB_RUNTIME_WAIT_SECONDS=30`
- capacity timeout → `503 {code:"runtime_capacity"}`
- global import/export/backup semaphore = `2`

Requirements:

- per-runtime reviewer sessions keyed by authenticated browser session
- no global current deck/card/reviewer state
- WS registry partitioned by resource key + session
- app shutdown → stop admission, drain jobs, close ∀ runtimes
- runtime open failure affects tenant readiness, not unrelated tenants
- process lock + registry uniqueness defend double open

### 11.6 request + job context

```text
TenantContext {actor_user_id, resource_owner_id, resource_key,
               global_role, resource_role, session_id, request_id}
JobContext    {job_id, actor_user_id, resource_key, capability,
               idempotency_key, created_at}
```

Rules:

- context created after auth; immutable through request
- repository/service methods require context or `ResourceKey`; ⊥ ambient global user
- queued job re-authorizes at start; revoked/suspended actor → cancel
- result/download checks actor + resource membership
- audit actor and target separately
- session revoke → close matching WS ≤ `5 s`

### 11.7 sessions

- durable SQLite sessions; restart ≠ logout
- opaque random token ≥`256 bits`; only SHA-256 digest stored
- cookie: `HttpOnly`, `Secure`, `SameSite=Strict`, path `/`
- idle expiry `30 days`; absolute expiry `90 days`
- user `auth_epoch` change revokes ∀ sessions without row scan
- max active sessions default `10`; oldest idle session revoked on new login
- `GET /api/v1/auth/sessions` lists device label, created, last seen, approximate IP
- user can revoke one/all sessions
- admin suspend revokes all; admin cannot mint user session

### 11.8 quotas + capacity

Defaults/user:

```text
storage              5 GiB
single import/upload 2 GiB
active jobs          2
active sessions      10
review WebSockets    4
reserved review DB   128 MiB
```

Policy:

- warning @ `80%`; hard limit @ `100%`
- upload/import → content-length preflight + streaming byte counter + staged-file cleanup
- import/install → estimate expanded media + collection delta before commit
- postflight actual usage; overrun → rollback import/install
- quota-sensitive import/install runs on disposable same-user collection copy; validate → atomic root swap
- storage-growing management ops blocked @ hard limit with `507 quota_exceeded`
- review answers use reserved space; ⊥ acknowledge answer before durable commit
- host free space `<5%` → block all growing jobs; health exposes degraded reason
- quota override audited; owner/admin only
- usage recompute daily + after large job; discrepancy >`1%` → repair counter

### 11.9 private backup + restore

- per-user backup quiesces only target runtime
- user backup includes collection, media, user app metadata, manifest/checksum
- system backup includes global `app.db`, invitations, memberships, audit, release index
- share backup includes workspace + immutable releases + comments
- archive encryption future; filesystem permissions `0600` required now
- restore-user flow: suspend → backup current → restore scratch → M1–M10 → atomic root swap → revoke sessions → reopen
- restore-as-new flow assigns new user UUID; rewrites control metadata only
- full disaster restore order: `app.db` → users → shares → release checksum validation
- purge waits for retention deadline + final backup policy
- backup jobs serialized against same runtime mutations

Targets:

- RPO ≤`24 h`
- single-user restore RTO ≤`30 min` at `5 GiB`
- quarterly random user + share restore drill

### 11.10 administration

Owner/admin capabilities:

- create/revoke account invite
- list users, state, last login, usage, backup age
- set quota; suspend/reactivate; initiate purge; cancel pending purge
- create password-reset token
- view security/operation audit metadata
- run user backup/restore health check

Restrictions:

- admin list/search ⊥ note text, card HTML, media bytes, review answers
- admin ⊥ impersonation or session minting
- content support requires user-exported diagnostic bundle
- owner-only: promote/demote admin, transfer system ownership, destructive purge
- every admin mutation → re-auth if credential age >`15 min` + audit event

### 11.11 invitation model

Account invite:

- token ≥`256 bits`; DB stores digest only
- token ∉ HTTP path/query/log; invite URL uses fragment; acceptance sends token in POST body
- default expiry `24 h`; one use
- role + intended username fixed at creation
- accept endpoint rate-limited; password set + consume in one transaction

Share invite:

- role `viewer | editor`; owner role ⊥ invite
- token ∉ HTTP path/query/log; invite URL uses fragment; acceptance sends token in POST body
- optional `intended_user_id`; if set, other user denied
- default expiry `7 days`; one use
- revocation immediate before acceptance
- acceptance creates membership once; replay idempotent for same user

### 11.12 deck sharing model

Share states: `draft | active | archived`.

Membership roles:

|role|read workspace|comment|edit content|publish|members|delete share|
|---|---:|---:|---:|---:|---:|---:|
|viewer|x|x|.|.|.|.|
|editor|x|x|x|.|.|.|
|owner|x|x|x|x|x|x|

Global admin ≠ automatic share member.

Workspace:

- separate Anki collection; one shared deck + required notetypes/media
- create by copying owner deck; source private deck unchanged
- scheduling/review actions disabled
- edits allowed: notes, fields, tags, templates, CSS, deck description, media
- deck hierarchy within workspace supported; external private deck links ⊥
- note GUID stable across releases
- workspace runtime uses same serialized worker model

Release:

```json
{
  "share_id": "uuid",
  "version": 3,
  "parent_version": 2,
  "anki_version": "26.09.3",
  "created_at": "ISO-8601",
  "note_guids_sha256": "...",
  "media_sha256": {},
  "tombstones": [],
  "bundle_sha256": "..."
}
```

- owner publishes explicit immutable release
- publish validates DB, templates, referenced media, package re-import
- failed validation → no release/version increment
- semantic version label optional; monotonic integer canonical
- archived share allows existing release download; blocks edits/publish/invites

### 11.13 install + subscription

Modes:

- `copy`: one-time release import; no relationship retained
- `follow`: subscription tracks installed release + source GUID/base hashes

Install/update job:

1. authorize membership + release
2. stage bundle under recipient root
3. checksum + quota validation
4. snapshot affected recipient deck metadata
5. import content via recipient runtime
6. preserve scheduling/review logs
7. store source GUID/base hashes + installed release
8. emit summary + conflicts

Update policy:

- upstream new note/media → add
- upstream changed content + recipient unchanged from base → update
- recipient field/template changed from base → conflict; ⊥ silent overwrite
- upstream tombstone → tag recipient note `ankiweb::retired`; ⊥ delete default
- optional `mirror` deletion policy requires per-subscription explicit enable + preview
- deck options/scheduling config local by default; upstream change shown for opt-in apply
- unsubscribe retains installed deck/content + local history
- failed update rolls back content transaction and leaves installed release unchanged

Conflict object:

```text
{id, entityType, entityId, field, baseHash, localValue, upstreamValue,
 release, resolution: mine|upstream|manual|null}
```

Conflict values visible only to recipient. Resolution audit stores hashes, not field bodies.

### 11.14 collaboration

- optimistic revision per note/template/deck config
- mutation requires `expectedRevision`; mismatch → `409 edit_conflict`
- no CRDT/character-level co-editing
- editor sees latest entity + conflict diff, reapplies edit manually
- comments attached to note/template/deck entity; Markdown subset; plain local links only
- comment edit window `15 min`; afterward append reply or resolve
- member removal/revocation → WS close ≤`5 s`; pending mutation re-authorizes before commit
- owner transfer atomic; old owner becomes editor unless removed
- deleting share → archive first; hard purge after `30 days` + no active restore hold
- workspace events visible only to active members

### 11.15 API + events

Auth/user:

```text
api: POST /api/v1/auth/login {username,password} → 204 + durable session
api: GET /api/v1/auth/sessions → 200 {sessions[]}
api: DELETE /api/v1/auth/sessions/{id} → 204
api: DELETE /api/v1/auth/sessions → 204
api: POST /api/v1/account-invitations/accept {token,username,password} → 201 {user}
api: GET /api/v1/me/usage → 200 {quota,used,warnings[]}
```

Admin:

```text
api: GET /api/v1/admin/users → 200 {users[],cursor}
api: POST /api/v1/admin/account-invitations → 201 {inviteUrl,expiresAt}
api: PATCH /api/v1/admin/users/{id} → 200 {user}
api: POST /api/v1/admin/users/{id}/password-reset → 201 {resetUrl,expiresAt}
api: POST /api/v1/admin/users/{id}/backup → 202 {jobId}
api: GET /api/v1/admin/audit → 200 {events[],cursor}
```

Sharing:

```text
api: GET /api/v1/shares → 200 {shares[],cursor}
api: POST /api/v1/shares → 202 {jobId}
api: GET /api/v1/shares/{id} → 200 {share,membership,releases[]}
api: POST /api/v1/shares/{id}/invitations → 201 {inviteUrl,expiresAt}
api: POST /api/v1/share-invitations/accept {token} → 200 {membership}
api: DELETE /api/v1/shares/{id}/members/{userId} → 204
api: POST /api/v1/shares/{id}/releases → 202 {jobId}
api: POST /api/v1/shares/{id}/installs → 202 {jobId}
api: POST /api/v1/subscriptions/{id}/updates → 202 {jobId}
api: GET /api/v1/jobs/{id}/conflicts → 200 {conflicts[]}
api: POST /api/v1/jobs/{id}/conflicts/{conflictId}/resolve → 200 {conflict}
api: PATCH /api/v1/shares/{id}/workspace/notes/{guid} → 200 {note,revision}
api: POST /api/v1/shares/{id}/workspace/comments → 201 {comment}
```

Events:

```text
event: user.session.revoked
event: user.quota.warning
event: user.state.changed
event: share.workspace.changed
event: share.member.changed
event: share.release.published
event: share.subscription.update_available
event: share.update.conflict
```

∀ endpoints: generated OpenAPI + TS client; cursor pagination; idempotency key on POST jobs/invites.

### 11.16 existing-user migration

Preconditions: verified current backup + app stopped + free space ≥ current user root ×`2`.

Steps:

1. migrate app DB schema
2. create `local` owner UUID; hash current plaintext credential if needed; remove plaintext env config
3. create target user root
4. copy collection/media; fsync; preserve source immutable
5. run M1–M10 against target
6. atomically activate `UserStorage(local)` mapping
7. start multi-user build loopback-only
8. login, review, audio, import/export, backup smoke
9. retain old root ≥`30 days`

Rollback before first target write → old build/root. After target write → stop, restore migration backup, old build/root.

Existing in-memory sessions intentionally revoked once; subsequent sessions durable.

### 11.17 acceptance gates

Multi-user:

```text
MU1  two users may use same deck/note IDs; reads/writes remain isolated
MU2  answer by user A changes only A scheduler/review log
MU3  media traversal/crafted UUID cannot cross roots
MU4  session survives restart; revoke/suspend closes HTTP + WS
MU5  four active runtimes + queued fifth respect capacity timeout
MU6  idle eviction/reopen preserves counts + scheduler state
MU7  quota blocks staged growth; no partial import/media residue
MU8  per-user backup/restore leaves other active user unaffected
MU9  migrated local user passes M1–M10
MU10 admin cannot fetch user card/note/media content via admin API
MU11 audit records actor/target/action; secrets/content absent
MU12 25-account load: 8 active browsers, 4 runtimes, p95 review transition ≤350 ms
MU13 same user on two devices cannot double-answer one card; stale lease → 409
```

Sharing/collaboration:

```text
SH1  nonmember denied workspace/release URLs + WS
SH2  invite expires, revokes, consumes once, binds intended user
SH3  viewer/editor/owner permission matrix enforced server-side
SH4  published release immutable + checksum/re-import valid
SH5  copy install reproduces notes/templates/media in recipient root
SH6  follow update preserves recipient scheduling + review history
SH7  divergent local field produces conflict; ⊥ silent overwrite
SH8  tombstone retires by default; mirror delete requires explicit preview
SH9  concurrent stale workspace edit → 409; winning edit preserved
SH10 removed member loses active WS + future job access ≤5 s
SH11 share/workspace/release backup restores + checksum passes
SH12 subscriber cannot access owner private decks outside workspace/release
```

Security suite: horizontal-IDOR matrix ∀ user/share/admin endpoints; CSRF/origin/host checks; rate limits; path fuzzing.

### 11.18 delivery estimate

Assumptions: one engineer; current v1 green; LAN-only; no email/public signup/billing/native sync.

MVP-A — `7–10 engineer days`:

|stage|days|output|exit|
|---|---:|---|---|
|A1|2|app DB users, durable sessions, account invites|auth/CSRF/session tests|
|A2|3|`TenantContext`, `UserStorage`, runtime registry|MU1–MU6 + MU13|
|A3|2|quota, per-user jobs/backups, admin basics|MU7–MU11|
|A4|1–3|migration, load/E2E, pilot docs|MU12 + M1–M10|

MVP-B — `8–12 engineer days`:

|stage|days|output|exit|
|---|---:|---|---|
|B1|2|shares, roles, share invites, admin membership UI|SH1–SH3|
|B2|3|workspace copy, release build, copy/follow install|SH4–SH6|
|B3|2–4|update diff/conflicts, optimistic edits, comments|SH7–SH10|
|B4|1–3|backup/restore, isolation/load/E2E, operations|SH11–SH12|

Pilot gates:

- MVP-A: ≥`7 days`, ≥`2 users`, daily backup, one restore drill
- MVP-B: ≥`7 days`, ≥`2 members`, ≥`2 releases`, update conflict drill
- each phase deployable independently; collaboration ⊥ prerequisite for private accounts

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
|T2|x|create Git repo + import upstream source with attribution|clean reproducible checkout|
|T3|~|add architecture skeleton + ADRs|module boundaries compile|
|T4|x|multi-stage Docker build + Compose|headless image healthy on loopback|
|T5|x|upgrade upstream pin `25.9.4` → `26.09.3`|adapter/asset versions match; tests pass|
|T6|~|create sanitized compatibility fixture + private production-copy test|M1–M10 pass on copy|
|T7|~|implement `/api/v1` + generated TS contracts|contract suite pass|
|T8|~|build mobile shell + deck/review flows|phone E2E review pass|
|T9|~|auth/security/proxy hardening|security checklist pass|
|T10|~|notes/browser/editor/import/export/stats responsive flows|feature E2E pass|
|T11|~|backup/restore/upgrade automation|recovery drill pass|
|T12|.|parallel LAN pilot on alternate port|≥7 days stable; no source writes|
|T13|.|cutover `18443`; Webtop read-only fallback|acceptance + user signoff|
|T14|.|retire Webtop runtime; retain recovery bundle|30-day stable window|
|T15|.|publish extension API v1|capability + compatibility tests|
|T16|.|concurrent private accounts|MU1–MU13 + migration pilot|
|T17|.|admin + account/share invitations|admin RBAC + invite abuse tests|
|T18|.|deck release sharing + subscriptions|SH1–SH8|
|T19|.|collaboration workspaces|SH9–SH12 + pilot|
|T20|.|optional native sync/offline research|ADR + conflict model|

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

Tasks: T15–T16.
Output: extension boundary + isolated concurrent accounts.

### P6 — sharing

Tasks: T17–T19.
Output: administration, invitations, versioned deck sharing, collaborative authoring.

### P7 — optional sync/offline

Task: T20.
Output: ADR + prototype only after multi-user/share stability.

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
|R11|horizontal cross-user data leak|`TenantContext`; UUID roots; IDOR matrix; V16–V18|
|R12|too many open collections exhaust RAM/FDs|runtime cap; idle eviction; load gate MU12|
|R13|quota failure leaves partial import|staging + pre/postflight + rollback|
|R14|shared update destroys local edits/history|base hashes; conflict stop; V23|
|R15|revoked member retains WS/job access|re-auth @ commit; session/member events; ≤5 s close|
|R16|admin role becomes content backdoor|metadata-only admin APIs; ⊥ impersonation; V25|

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

Decide before T4 (resolved 2026-10-03):

- D1: internal project name → `anki-lan-web` (avoid `AnkiWeb` official-service confusion)
- D2: TLS proxy → Caddy service in this repo's Compose (config only; ⊥ bind `:18443` / touch UFW until cutover)
- D3: upstream fork strategy → Git fork; `upstream` remote = `aitsc/ankiweb`

Decide before T8:

- D4: **resolved 2026-10-03** → Anki reviewer/editor wrapped by mobile chrome; thin `/api/v1` for new work; ⊥ full Svelte rewrite v1
- D5: local CA trust workflow for phones

Decide before T15:

- D6: plugin process isolation level
- D7: public extension signing/distribution

Decide before T16 (resolved 2026-10-03):

- D8: provisioning → bootstrap owner + owner/admin one-time invites; ⊥ public signup/email dependency
- D9: runtime → one serialized worker per active user/share inside one app process; capped registry + idle eviction

Decide before T18 (resolved 2026-10-03):

- D10: sharing → immutable releases copied into private collections; ⊥ shared review DB
- D11: update → preserve local scheduling; stop on content conflict; tombstones retire, ⊥ delete default
- D12: collaboration → separate workspace collection + optimistic entity revisions; ⊥ CRDT/live field merge
- D13: admin privacy → metadata operations only; ⊥ impersonation/content browsing

## 24. References

- unofficial browser port: `https://github.com/aitsc/ankiweb`
- official Anki source: `https://github.com/ankitects/anki`
- official sync server docs: `https://docs.ankiweb.net/sync-server.html`
- AnkiConnect: `https://github.com/FooSoft/anki-connect`
