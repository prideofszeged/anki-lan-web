"""Backup bundle, restore drill and retention prune (SPEC sections 14-15, T11).

    python -m ankiweb.adapters.anki.backup create        --data /data --out /backups
    python -m ankiweb.adapters.anki.backup restore-drill --archive /backups/anki-lan-web-*.tar.gz
    python -m ankiweb.adapters.anki.backup prune         --out /backups [--dry-run]

Run ``create`` while the app is stopped (scripts/backup.sh does that): the bundle must capture a
quiescent collection, and ``create`` aborts if the collection changes while it is being read.
The bundle is a tar.gz of the data directory (minus ``backups/``, ``import-tmp/``, ``home/``)
plus ``manifest.json`` (SPEC section 15) and ``acceptance-baseline.json`` (counts/hashes, no note
content), with a ``.sha256`` sidecar. ``restore-drill`` extracts into a throwaway directory,
re-verifies everything and runs the M1-M9 acceptance checks, reporting M10. It never writes to
the live data directory.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from anki.collection import Collection

from ankiweb.adapters.anki import acceptance
from ankiweb.adapters.anki.acceptance import Check
from ankiweb.application.retention import STAMP_FORMAT, parse_stamp, select_keep

EXCLUDED_TOP_LEVEL = frozenset({"backups", "import-tmp", "home"})
MANIFEST = "manifest.json"
BASELINE = "acceptance-baseline.json"
COLLECTION_REL = "anki/collection.anki2"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _app_version() -> str:
    try:
        return metadata.version("ankiweb")
    except metadata.PackageNotFoundError:
        return "0+unknown"


def _data_files(data_dir: Path) -> list[Path]:
    files = []
    for path in sorted(data_dir.rglob("*")):
        rel = path.relative_to(data_dir)
        if path.is_file() and rel.parts[0] not in EXCLUDED_TOP_LEVEL:
            files.append(path)
    return files


def _fingerprint(collection: Path) -> tuple:
    return tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size)
                 for p in sorted(collection.parent.glob(collection.name + "*")) if p.is_file())


def _add_bytes(tf: tarfile.TarFile, name: str, payload: bytes, when: datetime) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mtime = int(when.timestamp())
    info.mode = 0o644
    tf.addfile(info, io.BytesIO(payload))


def create_backup(data_dir: Path, out_dir: Path, *, now: datetime | None = None) -> Path:
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    collection = data_dir / COLLECTION_REL
    if not collection.is_file():
        raise FileNotFoundError(f"no collection at {collection}")
    before = _fingerprint(collection)
    snap = acceptance.snapshot(collection)
    files = _data_files(data_dir)
    hashes = {p.relative_to(data_dir).as_posix(): _sha256_file(p) for p in files}
    if _fingerprint(collection) != before:
        raise RuntimeError("collection changed while it was being read; stop the app and retry")
    manifest = {
        "created_at": now.isoformat(),
        "app_version": _app_version(),
        "anki_version": snap["anki_version"],
        "schema_versions": {"collection_schema": snap["schema_version"]},
        "note_count": snap["note_count"],
        "card_count": snap["card_count"],
        "media_count": snap["media_count"],
        "sha256": hashes,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"anki-lan-web-{now.strftime(STAMP_FORMAT)}.tar.gz"
    partial = archive.with_name(archive.name + ".partial")
    try:
        with tarfile.open(partial, "w:gz") as tf:
            _add_bytes(tf, MANIFEST, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(), now)
            _add_bytes(tf, BASELINE, (json.dumps(snap, indent=2, sort_keys=True) + "\n").encode(), now)
            for path in files:
                tf.add(path, arcname=path.relative_to(data_dir).as_posix(), recursive=False)
        partial.replace(archive)
    finally:
        partial.unlink(missing_ok=True)
    Path(str(archive) + ".sha256").write_text(f"{_sha256_file(archive)}  {archive.name}\n")
    return archive


def _r1_archive_intact(archive: Path) -> Check:
    sidecar = Path(str(archive) + ".sha256")
    if not sidecar.is_file():
        return Check("R1", "archive checksum", "fail", f"missing sidecar {sidecar.name}")
    want = sidecar.read_text().split()[0] if sidecar.read_text().split() else ""
    got = _sha256_file(archive)
    return Check("R1", "archive checksum", "pass" if got == want else "fail",
                 "sha256 matches sidecar" if got == want else f"sha256 {got[:12]}... != sidecar {want[:12]}...")


def restore_drill(archive: Path) -> list[Check]:
    """Extract ``archive`` into a temp dir and prove it restores: checksum, manifest hashes,
    SQLite integrity, then M1-M9 against the embedded baseline (M10 = all of that passed)."""
    results = [_r1_archive_intact(archive)]
    if results[0].status == "fail":
        return results
    with tempfile.TemporaryDirectory(prefix="ankiweb-drill-") as tmp:
        root = Path(tmp)
        try:
            with tarfile.open(archive) as tf:
                tf.extractall(root, filter="data")      # rejects absolute paths, '..', links
        except (tarfile.TarError, OSError, EOFError) as exc:
            results.append(Check("R2", "extract + manifest hashes", "fail", f"{type(exc).__name__}: {exc}"))
            return results
        try:
            manifest = json.loads((root / MANIFEST).read_text())
            baseline = json.loads((root / BASELINE).read_text())
        except (OSError, ValueError) as exc:
            results.append(Check("R2", "extract + manifest hashes", "fail", f"unreadable bundle metadata: {exc}"))
            return results
        bad = [name for name, want in manifest["sha256"].items()
               if not (root / name).is_file() or _sha256_file(root / name) != want]
        extra = sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                       if p.is_file() and p.relative_to(root).as_posix() not in manifest["sha256"]
                       and p.name not in (MANIFEST, BASELINE))
        results.append(Check(
            "R2", "extract + manifest hashes", "fail" if bad or extra else "pass",
            "; ".join(filter(None, [f"mismatch/missing: {', '.join(bad[:8])}" if bad else "",
                                    f"unlisted: {', '.join(extra[:8])}" if extra else ""]))
            or f"{len(manifest['sha256'])} files match manifest"))
        collection = root / COLLECTION_REL
        try:
            # Through Anki's engine, not sqlite3: collections use a custom `unicase` collation
            # that only the Rust backend registers. The extracted tree is already throwaway.
            col = Collection(str(collection))
            try:
                verdict = col.db.scalar("pragma integrity_check")
            finally:
                col.close()
        except Exception as exc:
            verdict = f"{type(exc).__name__}: {exc}"
        results.append(Check("R3", "SQLite integrity", "pass" if verdict == "ok" else "fail", verdict))
        results.extend(acceptance.verify(collection, baseline, restored=True))
    return results


def prune(out_dir: Path, *, now: datetime | None = None, dry_run: bool = False) -> list[str]:
    """Delete archives (and sidecars) outside the 7/4/6 retention windows; returns their names."""
    now = now or datetime.now(timezone.utc)
    stamped = {parse_stamp(p.name): p for p in out_dir.iterdir() if parse_stamp(p.name)}
    keep = select_keep(stamped, now)
    doomed = [stamped[s].name for s in sorted(stamped) if s not in keep]
    if not dry_run:
        for name in doomed:
            (out_dir / name).unlink()
            (out_dir / (name + ".sha256")).unlink(missing_ok=True)
    return doomed


def _report(checks: list[Check]) -> str:
    return "\n".join(f"{c.id:<4}{c.status.upper():<5}{c.name} - {c.detail}" for c in checks)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ankiweb.adapters.anki.backup", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="write a backup bundle + sha256 sidecar")
    c.add_argument("--data", type=Path, required=True)
    c.add_argument("--out", type=Path, required=True)
    d = sub.add_parser("restore-drill", help="prove a bundle restores (never touches live data)")
    d.add_argument("--archive", type=Path, required=True)
    d.add_argument("--json", action="store_true")
    r = sub.add_parser("prune", help="apply 7 daily / 4 weekly / 6 monthly retention")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "create":
        print(create_backup(args.data, args.out))
        return 0
    if args.cmd == "prune":
        doomed = prune(args.out, dry_run=args.dry_run)
        print(f"{'would remove' if args.dry_run else 'removed'} {len(doomed)} backup(s)")
        for name in doomed:
            print(f"  {name}")
        return 0
    checks = restore_drill(args.archive)
    print(json.dumps([c.__dict__ for c in checks], indent=2) if args.json else _report(checks))
    return 1 if any(c.status == "fail" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
