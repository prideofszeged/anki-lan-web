#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_dir}"

docker compose config --quiet
docker compose ps

container_id="$(docker compose ps -q app)"
if [[ -z "${container_id}" ]]; then
  echo "app container is not running" >&2
  exit 1
fi

health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${container_id}")"
if [[ "${health}" != "healthy" ]]; then
  echo "app health is ${health}" >&2
  exit 1
fi

docker compose stop app >/dev/null
restart_app() { docker compose start app >/dev/null; }
trap restart_app EXIT

docker compose run --rm --no-deps -T --entrypoint python app - <<'PY'
import os
from pathlib import Path
from anki.collection import Collection

collection = Path(os.environ["ANKIWEB_COLLECTION"])
col = Collection(str(collection))
try:
    check = col.db.scalar("pragma integrity_check")
    notes = col.note_count()
    cards = col.card_count()
finally:
    col.close()
if check != "ok":
    raise SystemExit(f"collection integrity check failed: {check}")
media_dir = collection.with_suffix(".media")
media = sum(1 for path in media_dir.iterdir() if path.is_file()) if media_dir.is_dir() else 0
print(f"ok: {notes} notes, {cards} cards, {media} media files")
PY
