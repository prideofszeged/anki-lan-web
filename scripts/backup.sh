#!/usr/bin/env bash
# Quiescent backup: stop the app, write a verified bundle (tar.gz + manifest + sha256 sidecar),
# apply 7 daily / 4 weekly / 6 monthly retention, restart the app. Requires an image built from
# this tree (`docker compose build app`) since the tool ships inside the image.
set -euo pipefail
umask 077

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backup_dir="${ANKIWEB_BACKUP_DIR:-${repo_dir}/backups}"

mkdir -p "${backup_dir}"
backup_dir="$(cd "${backup_dir}" && pwd)"
cd "${repo_dir}"

exec 9>"${repo_dir}/data/.maintenance.lock"
flock -n 9 || { echo "another backup/verification operation is running" >&2; exit 1; }

was_running="$(docker compose ps --status running --services | grep -Fx app || true)"
if [[ -n "${was_running}" ]]; then
  docker compose stop app >/dev/null
fi

restart_app() {
  if [[ -n "${was_running}" ]]; then
    docker compose start app >/dev/null
  fi
}
trap restart_app EXIT

tool=(docker compose run --rm --no-deps -T -v "${backup_dir}:/backups" --entrypoint python app
      -m ankiweb.adapters.anki.backup)
"${tool[@]}" create --data /data --out /backups
"${tool[@]}" prune --out /backups
