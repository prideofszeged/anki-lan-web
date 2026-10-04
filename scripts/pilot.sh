#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_dir}"

export PUID="${PUID:-$(id -u)}"
export PGID="${PGID:-$(id -g)}"
install -d -m 0750 data data/anki data/import-tmp data/backups data/home data/tmp

docker compose up -d --build
for _ in $(seq 1 60); do
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}starting{{end}}' anki-lan-web 2>/dev/null || true)"
  if [[ "${status}" == "healthy" ]]; then
    echo "anki-lan-web is ready at http://127.0.0.1:18082"
    exit 0
  fi
  sleep 1
done

docker compose logs --tail=100 app >&2
exit 1
