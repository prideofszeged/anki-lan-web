#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backup_dir="${ANKIWEB_BACKUP_DIR:-${repo_dir}/backups}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="${backup_dir}/anki-lan-web-${stamp}.tar.gz"

mkdir -p "${backup_dir}"
cd "${repo_dir}"

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

tar -czf "${archive}" data
sha256sum "${archive}" > "${archive}.sha256"
echo "${archive}"
