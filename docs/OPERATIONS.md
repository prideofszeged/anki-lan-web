# Operations

## Start the local pilot

```bash
cp .env.example .env
# edit .env: set a real ANKIWEB_PASSWORD (see "Authentication" below)
./scripts/pilot.sh
```

The default Compose mapping is loopback-only at `http://127.0.0.1:18082`. This makes the
pilot safe to test alongside another Anki service. LAN/Tailscale exposure is opt-in (see
"LAN HTTPS"); do not publish the application port directly to the internet.

An existing collection is required by default. For a deliberately empty first-time install,
set `ANKIWEB_INIT_COLLECTION=true` for the first successful start, then remove it or set it back
to `false`. This prevents a missing or incorrectly mounted data directory from looking like an
empty-but-healthy Anki library. Large temporary imports and exports use `data/tmp/`, not the
container's small in-memory `/tmp`.

## Authentication

`python -m ankiweb` **refuses to start** without `ANKIWEB_PASSWORD` or `ANKIWEB_PASSWORD_HASH`
(Argon2id), and rejects the `change-me` placeholder. To run unauthenticated on an already-isolated
host you must say so explicitly with `ANKIWEB_AUTH_DISABLED=1`.

State-changing requests must be same-origin (`Sec-Fetch-Site`, else `Origin`, else `Referer`
must match the `Host` the browser used). A session cookie with none of these headers is rejected,
so a script using a copied cookie must send an `Origin` header. Sessions live in memory: restarting
the app logs everyone out (ADR 0003).

## Verify

```bash
./scripts/verify.sh
```

Checks the Compose model, container health, SQLite integrity, and reports collection and media
counts. `GET /healthz` and `/api/v1/health/live` report process liveness without queueing behind
long Anki work. `/api/v1/health/ready` checks that the collection worker is responsive (503
otherwise).

## Acceptance checks (SPEC M1–M10)

For the opt-in account migration and multi-user pilot procedure, see
[MULTIUSER.md](MULTIUSER.md).

Record a baseline from a known-good collection, then verify copies against it. Both commands work
on a private temporary copy and never write the collection you point them at. Run them against a
stopped app or a backup, not a live WAL database. The baseline holds counts and hashes only (no
note content) but still describes your collection: keep it under `data/` (gitignored).

```bash
python -m ankiweb.adapters.anki.acceptance snapshot --collection data/anki/collection.anki2 --out data/baseline.json
python -m ankiweb.adapters.anki.acceptance verify   --collection <copy>/collection.anki2 --baseline data/baseline.json
```

Statuses: `PASS`; `WARN` for a defect already present in the source (for example a template that
references a media file the original collection never had), which is reported but does not fail the
run; `FAIL` for anything the copy lost or changed; `SKIP` when a check cannot apply (a day rollover
makes queue order incomparable). M6 and M9 are automated approximations: iOS/Android playback and
importing the `.colpkg` into the desktop GUI remain manual steps.

## Back up

```bash
docker compose build app      # the backup tool ships inside the image
./scripts/backup.sh
```

The script briefly stops the app, writes `anki-lan-web-<UTC stamp>.tar.gz` plus a `.sha256`
sidecar under `backups/` (override with `ANKIWEB_BACKUP_DIR`), applies retention, and restarts
the app. The archive contains the data directory (without `backups/`, `import-tmp/`, `home/`),
`manifest.json` (versions, counts, per-file SHA-256) and `acceptance-baseline.json`. It aborts if
the collection changes while it is being read. Archives and checksum sidecars are created with
owner-only permissions (`0600`). Backup and verification scripts share a non-blocking maintenance
lock so they cannot stop or inspect the collection concurrently.

Retention is 7 daily, 4 weekly and 6 monthly backups (newest per bucket; the newest overall is
always kept). Preview with:

```bash
docker compose run --rm --no-deps -T -v "$PWD/backups:/backups" --entrypoint python app \
  -m ankiweb.adapters.anki.backup prune --out /backups --dry-run
```

Copy backups to a second machine or encrypted cloud storage.

## Restore drill

```bash
./scripts/restore-drill.sh                      # newest backup, or pass an archive path
```

Runs in a throwaway container with **no network and no `/data` mount**. It verifies the archive
checksum (R1), extracts safely and checks every file against the manifest (R2), runs SQLite
integrity through Anki's engine (R3), then M1–M9 against the embedded baseline and reports M10.
The extracted copy lives in `backups/.drill-tmp` (override with `ANKIWEB_DRILL_TMP`), not `/tmp`:
Docker Desktop refuses bind mounts outside the paths it shares with containers, and large media
should not sit in RAM. Never test a restore by overwriting the active `data/`. Run the drill after every upgrade and at
least quarterly, and before retiring Webtop (SPEC V12).

Do not use a bare `sqlite3` integrity check on a collection: it uses a custom `unicase` collation
that only Anki's engine registers.

## LAN HTTPS (opt-in)

```bash
# .env: ANKIWEB_SECURE_COOKIE=true, ANKIWEB_LAN_BIND=192.168.1.7 (an address of this host)
docker compose --profile lan up -d
```

Starts a Caddy front (`deploy/proxy/Caddyfile`) on `ANKIWEB_LAN_BIND:18443` with an internal CA.
Nothing listens on 18443 unless you pass `--profile lan`. Firewall rules are yours to manage and
are not changed by this repository. Export the CA root to trust it on owned devices:

```bash
docker compose cp proxy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
```

The Caddy base image is pinned by digest. The small derived image removes an upstream binary
capability that is unnecessary on port 18443, allowing the container to retain both
`no-new-privileges` and a completely empty capability set. Build and validate it before first LAN
use:

```bash
docker compose --profile lan build proxy
docker compose --profile lan run --rm proxy caddy validate --config /etc/caddy/Caddyfile
```

## Diagnosing a hung server

Symptoms: `docker ps` shows `unhealthy`, `/healthz` times out, `docker stats` shows ~100% CPU.

```bash
docker kill -s USR1 anki-lan-web      # dumps every thread's stack; does NOT stop the app
docker logs --tail 80 anki-lan-web    # the traceback shows where it is spinning
docker restart anki-lan-web           # then recover
```

The signal handler is registered at start-up (`ankiweb.__main__.enable_diagnostics`), so it only
works on images built after it was added.

## Upgrade

1. Run a backup and copy it off-host; run `./scripts/restore-drill.sh` on it.
2. Change the pinned `anki` version and matching asset version in one commit.
3. Run `pytest` and the browser integration suite.
4. Build and start on the loopback pilot port.
5. Run `./scripts/verify.sh` and the acceptance `verify` against a copied collection, then complete
   one study/edit/add cycle on that copy.
6. Only then move the reverse proxy to the new container.

Rollback means restoring the prior image and its matching pre-upgrade backup. Do not open a
collection upgraded by a newer Anki release with an older engine unless Anki documents that
path as supported.
