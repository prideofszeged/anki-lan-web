"""T11: backup bundle + manifest, restore drill, retention prune (against the generated fixture)."""
import hashlib
import io
import json
import stat
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ankiweb.adapters.anki import backup as bk
from fixture_collection import GREEK, build

NOW = datetime(2026, 10, 3, 17, 5, 0, tzinfo=timezone.utc)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    build(data / "anki")
    (data / "backups").mkdir()
    (data / "backups" / "old.tar.gz").write_bytes(b"must not be re-archived")
    (data / "import-tmp").mkdir()
    (data / "import-tmp" / "scratch.apkg").write_bytes(b"temp")
    (data / "tmp").mkdir()
    (data / "tmp" / "tmp-stale.colpkg").write_bytes(b"large temporary export")
    (data / ".maintenance.lock").write_text("do not archive")
    (data / "anki" / "ankiconnect.json").write_text('{"apiKey": "k"}')
    return data


def _names(archive: Path) -> set[str]:
    with tarfile.open(archive) as tf:
        return set(tf.getnames())


def test_create_backup_writes_archive_and_sidecar(data_dir, tmp_path):
    out = tmp_path / "out"
    archive = bk.create_backup(data_dir, out, now=NOW)
    assert archive.name == "anki-lan-web-20261003T170500Z.tar.gz"
    sidecar = Path(str(archive) + ".sha256")
    assert sidecar.read_text().split()[0] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


def test_bundle_contents_exclude_scratch_and_other_backups(data_dir, tmp_path):
    names = _names(bk.create_backup(data_dir, tmp_path / "out", now=NOW))
    assert {"manifest.json", "acceptance-baseline.json", "anki/collection.anki2",
            "anki/collection.media/lt01.mp3", "anki/ankiconnect.json"} <= names
    assert not any(n.startswith(("backups", "import-tmp", "tmp", ".")) for n in names)


def test_manifest_matches_spec_section_15(data_dir, tmp_path):
    archive = bk.create_backup(data_dir, tmp_path / "out", now=NOW)
    with tarfile.open(archive) as tf:
        manifest = json.load(tf.extractfile("manifest.json"))
    assert manifest["created_at"] == "2026-10-03T17:05:00+00:00"
    assert manifest["anki_version"] == "26.09.3"
    assert manifest["note_count"] == len(GREEK) and manifest["card_count"] == len(GREEK)
    assert manifest["media_count"] == 2
    assert manifest["app_version"] and "collection_schema" in manifest["schema_versions"]
    assert set(manifest["sha256"]) >= {"anki/collection.anki2", "anki/collection.media/lt01.mp3"}
    assert "manifest.json" not in manifest["sha256"]


def test_manifest_and_baseline_hold_no_note_content(data_dir, tmp_path):
    archive = bk.create_backup(data_dir, tmp_path / "out", now=NOW)
    with tarfile.open(archive) as tf:
        blob = (tf.extractfile("manifest.json").read() + tf.extractfile("acceptance-baseline.json").read()
                ).decode("utf-8")
    assert not any(w in blob for w in GREEK)


def test_backup_never_modifies_the_data_dir(data_dir, tmp_path):
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in data_dir.rglob("*") if p.is_file()}
    bk.create_backup(data_dir, tmp_path / "out", now=NOW)
    after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in data_dir.rglob("*") if p.is_file()}
    assert before == after


def test_restore_drill_passes_on_a_good_backup(data_dir, tmp_path):
    archive = bk.create_backup(data_dir, tmp_path / "out", now=NOW)
    results = {c.id: c for c in bk.restore_drill(archive)}
    bad = {k: v.detail for k, v in results.items() if v.status == "fail"}
    assert not bad, bad
    assert results["R1"].status == results["R2"].status == results["R3"].status == "pass"
    assert results["M10"].status == "pass"


def test_restore_drill_never_touches_the_live_data_dir(data_dir, tmp_path):
    archive = bk.create_backup(data_dir, tmp_path / "out", now=NOW)
    before = {p: p.stat().st_mtime_ns for p in data_dir.rglob("*") if p.is_file()}
    bk.restore_drill(archive)
    assert before == {p: p.stat().st_mtime_ns for p in data_dir.rglob("*") if p.is_file()}


def test_drill_detects_corrupted_archive(data_dir, tmp_path):
    archive = bk.create_backup(data_dir, tmp_path / "out", now=NOW)
    raw = bytearray(archive.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    archive.write_bytes(bytes(raw))
    results = {c.id: c for c in bk.restore_drill(archive)}
    assert results["R1"].status == "fail"


def test_drill_detects_file_swapped_inside_a_valid_archive(data_dir, tmp_path):
    archive = bk.create_backup(data_dir, tmp_path / "out", now=NOW)
    rebuilt = tmp_path / "rebuilt.tar.gz"
    with tarfile.open(archive) as src, tarfile.open(rebuilt, "w:gz") as dst:
        for member in src.getmembers():
            data = src.extractfile(member).read() if member.isfile() else None
            if member.name == "anki/collection.media/lt01.mp3":
                data = b"swapped contents"
                member.size = len(data)
            dst.addfile(member, io.BytesIO(data) if data is not None else None)
    Path(str(rebuilt) + ".sha256").write_text(
        hashlib.sha256(rebuilt.read_bytes()).hexdigest() + "  rebuilt.tar.gz\n")
    results = {c.id: c for c in bk.restore_drill(rebuilt)}
    assert results["R1"].status == "pass"          # archive itself is intact...
    assert results["R2"].status == "fail"          # ...but a member no longer matches the manifest
    assert results["M10"].status == "fail"


def test_drill_requires_the_sha256_sidecar(data_dir, tmp_path):
    archive = bk.create_backup(data_dir, tmp_path / "out", now=NOW)
    Path(str(archive) + ".sha256").unlink()
    assert {c.id: c for c in bk.restore_drill(archive)}["R1"].status == "fail"


def test_drill_rejects_path_traversal_members(tmp_path):
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tf:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 3
        tf.addfile(info, io.BytesIO(b"bad"))
    Path(str(evil) + ".sha256").write_text(hashlib.sha256(evil.read_bytes()).hexdigest() + "  evil\n")
    results = {c.id: c for c in bk.restore_drill(evil)}
    assert any(c.status == "fail" for c in results.values())
    assert not (tmp_path.parent / "escape.txt").exists()


def test_prune_dry_run_lists_without_deleting(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    names = []
    for i in range(30):
        day = datetime(2026, 10, 3, 3, tzinfo=timezone.utc).timestamp() - i * 86400
        stamp = datetime.fromtimestamp(day, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for suffix in ("", ".sha256"):
            (out / f"anki-lan-web-{stamp}.tar.gz{suffix}").write_text("x")
        names.append(stamp)
    (out / "unrelated.txt").write_text("keep me")
    doomed = bk.prune(out, now=NOW, dry_run=True)
    assert doomed and len(list(out.iterdir())) == 61           # untouched
    removed = bk.prune(out, now=NOW)
    assert set(doomed) == set(removed)
    left = {p.name for p in out.iterdir()}
    assert "unrelated.txt" in left
    for name in removed:
        assert name not in left and name + ".sha256" not in left
    assert f"anki-lan-web-{names[0]}.tar.gz" in left            # newest survives with its sidecar
    assert f"anki-lan-web-{names[0]}.tar.gz.sha256" in left


def test_cli_roundtrip(data_dir, tmp_path, capsys):
    out = tmp_path / "out"
    assert bk.main(["create", "--data", str(data_dir), "--out", str(out)]) == 0
    archive = next(out.glob("*.tar.gz"))
    assert bk.main(["restore-drill", "--archive", str(archive)]) == 0
    assert "M10" in capsys.readouterr().out
