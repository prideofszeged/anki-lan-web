#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_dir}"

exec 9>"${repo_dir}/data/.maintenance.lock"
flock -n 9 || { echo "another backup/verification operation is running" >&2; exit 1; }

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
import shutil
import tempfile
from pathlib import Path
from anki.collection import Collection

collection = Path(os.environ["ANKIWEB_COLLECTION"])
if not collection.is_file():
    raise SystemExit(f"collection is missing: {collection}")
with tempfile.TemporaryDirectory(prefix="ankiweb-verify-") as tmp:
    copy = Path(tmp) / "collection.anki2"
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(collection) + suffix)
        if source.exists():
            shutil.copy2(source, Path(str(copy) + suffix))
    col = Collection(str(copy))
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
