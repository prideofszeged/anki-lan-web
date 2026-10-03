# Operations

## Start the local pilot

```bash
cp .env.example .env
# edit .env and set a long password
./scripts/pilot.sh
```

The default Compose mapping is loopback-only at `http://127.0.0.1:18082`. This makes the
pilot safe to test alongside another Anki service. LAN/Tailscale exposure should be done at
the reverse-proxy and firewall layer after acceptance; do not publish the application port
directly to the internet.

## Verify

```bash
./scripts/verify.sh
```

This checks the Compose model, container health, SQLite integrity, and reports collection
and media counts.

## Back up

```bash
./scripts/backup.sh
```

The script briefly stops the app to guarantee a quiescent collection, archives the complete
`data/` directory, writes a SHA-256 sidecar, and restarts the app. Backups are written under
`backups/` by default. Copy them to a second machine or encrypted cloud storage.

Test restores into a new directory and a temporary container; never overwrite the active
`data/` directory to test a backup.

## Upgrade

1. Run a backup and copy it off-host.
2. Change the pinned `anki` version and matching asset version in one commit.
3. Run `pytest` and the browser integration suite.
4. Build and start on the loopback pilot port.
5. Run `./scripts/verify.sh` and complete one study/edit/add cycle on a copied collection.
6. Only then move the reverse proxy to the new container.

Rollback means restoring the prior image and its matching pre-upgrade backup. Do not open a
collection upgraded by a newer Anki release with an older engine unless Anki documents that
path as supported.
