# Multi-user pilot

Multi-user mode gives each account a separate Anki collection, media directory,
scheduler, durable login sessions, and browser/reviewer state. It is opt-in and does
not start AnkiConnect, because that API still assumes one process-global collection.

## New installation

Build the current image, then create the first owner while the app is stopped:

```bash
docker compose build app
printf '%s\n' 'replace-with-a-long-password' | \
  docker compose run --rm --no-deps -T app user bootstrap \
    --username local --display-name 'Local owner' --password-stdin
```

Set these values in `.env` and remove `ANKIWEB_PASSWORD` and
`ANKIWEB_PASSWORD_HASH`:

```dotenv
ANKIWEB_MULTI_USER=true
```

For the loopback-only `127.0.0.1:18082` Compose pilot, also set:

```dotenv
ANKIWEB_INSECURE_COOKIE_OK=true
```

That exception is safe only while the published app port remains loopback-only. For
phone/LAN access, use the HTTPS proxy profile, set `ANKIWEB_SECURE_COOKIE=true`, and
remove `ANKIWEB_INSECURE_COOKIE_OK`.

Start and verify:

```bash
docker compose up -d app
docker compose logs --tail 100 app
curl -fsS http://127.0.0.1:18082/api/v1/health/ready
```

## Migrate the existing collection

The migration refuses to run unless the app is stopped, a byte-identical backup has
passed R1-R3 and M10, and the target filesystem has at least twice the source size free.
It preserves the old source and quarantines the owner's previously provisioned empty
collection.

First create a backup, then stop the app again because `backup.sh` restores its prior
running state:

```bash
./scripts/backup.sh
docker compose stop app
```

Create the owner as above. Then generate durable migration evidence from the newest
backup (replace the user UUID and archive name shown by the preceding commands):

```bash
docker compose run --rm --no-deps -T \
  -v "$PWD/backups:/backups:ro" app user prepare-migration \
  --user-id USER_UUID --backup /backups/anki-lan-web-YYYYMMDDTHHMMSSZ.tar.gz \
  --confirm-stopped

docker compose run --rm --no-deps -T app user migrate-legacy \
  --user-id USER_UUID
```

Only after both commands succeed should you enable multi-user mode and remove the
legacy password variables. Keep the original collection and migration quarantine for
at least 30 days.

## Invite another account

Sign in as the owner, fetch/rotate the CSRF token with
`GET /api/v1/auth/csrf`, then call `POST /api/v1/admin/account-invites`. The token in
the response is shown once and is submitted in the body of
`POST /api/v1/account-invites/accept`; it must not be placed in a URL or logs. Invite
acceptance provisions and validates an empty private collection before activating the
account.

## Current boundary

Private concurrent accounts, durable sessions, runtime isolation, invitation-based
provisioning, and the migration safety gate are implemented. The larger collaboration
phase—shared workspaces, immutable releases, subscriptions, three-way update conflicts,
comments, and share backup/restore—remains disabled until its acceptance gates are
implemented.
