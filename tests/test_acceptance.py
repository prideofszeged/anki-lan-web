"""T6: M1-M10 acceptance tooling against a generated, sanitized fixture collection."""
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from anki.collection import Collection

from ankiweb.adapters.anki import acceptance as acc

from fixture_collection import GREEK, build as _build


@pytest.fixture
def source(tmp_path: Path) -> Path:
    return _build(tmp_path / "src")


@pytest.fixture
def baseline(source: Path, tmp_path: Path) -> dict:
    out = tmp_path / "baseline.json"
    acc.write_snapshot(source, out)
    return json.loads(out.read_text())


def _by_id(checks):
    return {c.id: c for c in checks}


def test_clean_collection_passes_m1_to_m9(source, baseline):
    got = _by_id(acc.verify(source, baseline))
    failing = {k: v.detail for k, v in got.items() if v.status == "fail"}
    assert not failing, failing
    for m in ("M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9"):
        assert got[m].status == "pass", (m, got[m])
    assert got["M10"].status == "skip"


def test_restored_flag_marks_m10_pass_only_when_everything_passed(source, baseline):
    got = _by_id(acc.verify(source, baseline, restored=True))
    assert got["M10"].status == "pass"


def test_verify_never_writes_the_source_collection(source, baseline):
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    acc.verify(source, baseline)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_baseline_holds_hashes_not_note_content(baseline):
    blob = json.dumps(baseline, ensure_ascii=False)
    assert not any(word in blob for word in GREEK)
    assert baseline["note_count"] == len(GREEK)


def test_deleted_note_fails_m1_and_m2(source, baseline):
    col = Collection(str(source))
    try:
        col.remove_notes(col.find_notes("")[:1])
    finally:
        col.close()
    got = _by_id(acc.verify(source, baseline))
    assert got["M1"].status == "fail" and got["M2"].status == "fail"


def test_edited_greek_field_fails_m5(source, baseline):
    col = Collection(str(source))
    try:
        note = col.get_note(col.find_notes("")[0])
        note["Front"] = note["Front"].replace("ώ", "w")
        note["Back"] = note["Back"] + " changed"
        col.update_note(note)
    finally:
        col.close()
    assert _by_id(acc.verify(source, baseline))["M5"].status == "fail"


def test_missing_media_fails_m6_and_m7(source, baseline):
    (source.parent / "collection.media" / "lt01.mp3").unlink()
    got = _by_id(acc.verify(source, baseline))
    assert got["M6"].status == "fail" and "lt01.mp3" in got["M6"].detail
    assert got["M7"].status == "fail"


def test_non_audio_bytes_in_sound_file_fail_m6(source, baseline):
    (source.parent / "collection.media" / "lt01.mp3").write_bytes(b"not audio at all")
    assert _by_id(acc.verify(source, baseline))["M6"].status == "fail"


def test_cli_exit_codes(source, tmp_path, capsys):
    base = tmp_path / "b.json"
    assert acc.main(["snapshot", "--collection", str(source), "--out", str(base)]) == 0
    assert acc.main(["verify", "--collection", str(source), "--baseline", str(base)]) == 0
    shutil.rmtree(source.parent / "collection.media")
    assert acc.main(["verify", "--collection", str(source), "--baseline", str(base)]) == 1
    assert "M6" in capsys.readouterr().out


def test_m4_baseline_is_not_vacuous(baseline):
    """The queue is per *current deck*; sampling only the default (empty) deck would pass anything."""
    assert any(baseline["queue_sample"].values()), baseline["queue_sample"]


def test_suspended_card_changes_queue_and_fails_m4(source, baseline):
    col = Collection(str(source))
    try:
        col.sched.suspend_cards(col.find_cards("")[:1])
    finally:
        col.close()
    got = _by_id(acc.verify(source, baseline))
    assert got["M4"].status == "fail"


def _snapshot_after_deleting_media(source: Path, tmp_path: Path, name: str) -> dict:
    (source.parent / "collection.media" / name).unlink()
    out = tmp_path / "preexisting.json"
    acc.write_snapshot(source, out)
    return json.loads(out.read_text())


def test_preexisting_missing_audio_is_warn_not_fail(source, tmp_path):
    """Missing in the source already => a defect of the collection, not a migration loss."""
    base = _snapshot_after_deleting_media(source, tmp_path, "lt01.mp3")
    assert base["missing_audio"] == ["lt01.mp3"]
    got = _by_id(acc.verify(source, base))
    assert got["M6"].status == "warn" and "lt01.mp3" in got["M6"].detail
    assert got["M7"].status == "warn"
    assert not [c for c in got.values() if c.status == "fail"]


def test_media_lost_after_snapshot_still_fails_even_if_other_files_were_already_missing(source, tmp_path):
    base = _snapshot_after_deleting_media(source, tmp_path, "pic.png")
    (source.parent / "collection.media" / "lt01.mp3").unlink()          # NEW loss
    got = _by_id(acc.verify(source, base))
    assert got["M6"].status == "fail" and "lt01.mp3" in got["M6"].detail
    assert got["M7"].status == "fail"


def test_restored_m10_passes_with_warnings_and_says_so(source, tmp_path):
    base = _snapshot_after_deleting_media(source, tmp_path, "lt01.mp3")
    m10 = _by_id(acc.verify(source, base, restored=True))["M10"]
    assert m10.status == "pass" and "warn" in m10.detail.lower()


def test_cli_exit_code_is_zero_with_only_warnings(source, tmp_path):
    (source.parent / "collection.media" / "lt01.mp3").unlink()
    base = tmp_path / "b.json"
    assert acc.main(["snapshot", "--collection", str(source), "--out", str(base)]) == 0
    assert acc.main(["verify", "--collection", str(source), "--baseline", str(base)]) == 0
