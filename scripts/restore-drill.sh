#!/usr/bin/env bash
# Prove a backup restores: checksum, manifest hashes, SQLite integrity, then M1-M10 acceptance on
# the restored copy. Runs in a throwaway container with NO network and NO /data mount, so the
# live collection cannot be touched. Usage: scripts/restore-drill.sh [archive.tar.gz]
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backup_dir="${ANKIWEB_BACKUP_DIR:-${repo_dir}/backups}"
image="${ANKIWEB_IMAGE:-local/anki-lan-web:26.09.3}"

archive="${1:-}"
if [[ -z "${archive}" ]]; then
  archive="$(ls -1t "${backup_dir}"/anki-lan-web-*.tar.gz 2>/dev/null | head -n 1 || true)"
fi
if [[ -z "${archive}" || ! -f "${archive}" ]]; then
  echo "no backup archive found (pass one, or create with scripts/backup.sh)" >&2
  exit 2
fi
archive="$(cd "$(dirname "${archive}")" && pwd)/$(basename "${archive}")"

# Scratch space for the extracted copy. Not /tmp: Docker Desktop only shares $HOME-ish paths, so a
# bind mount from /tmp is denied. Keep it next to the backups (large media never touches RAM).
drill_tmp="${ANKIWEB_DRILL_TMP:-${backup_dir}/.drill-tmp}"
mkdir -p "${drill_tmp}"
scratch="$(mktemp -d "${drill_tmp}/drill.XXXXXX")"
trap 'rm -rf "${scratch}"' EXIT

docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --user "${PUID:-$(id -u)}:${PGID:-$(id -g)}" \
  -e HOME=/tmp \
  -v "$(dirname "${archive}"):/backups:ro" \
  -v "${scratch}:/tmp" \
  --entrypoint python "${image}" \
  -m ankiweb.adapters.anki.backup restore-drill --archive "/backups/$(basename "${archive}")"
