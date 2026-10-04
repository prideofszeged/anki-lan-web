"""SPEC section 17.5 migration acceptance (M1-M10) for a collection copy.

    python -m ankiweb.adapters.anki.acceptance snapshot --collection SRC --out baseline.json
    python -m ankiweb.adapters.anki.acceptance verify   --collection COPY --baseline baseline.json

Both commands work on a private temporary copy and never write the collection they are pointed
at (opening a collection can upgrade/checkpoint it). Run against a stopped app or a backup, not
a live WAL database. The baseline holds counts and hashes only - no note content - and belongs
under ``data/`` (gitignored). M10 is "a restored backup reproduces M1-M9": run ``verify
--restored`` on the restore and it reports M10 from the outcome of M1-M9.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import anki.collection  # noqa: F401  (must load before anki.scheduler: circular import)
from anki.collection import Collection
from anki.scheduler.v3 import CardAnswer
from anki.sound import SoundOrVideoTag

QUEUE_SAMPLE = 20
RENDER_SAMPLE_PER_NOTETYPE = 10
_SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")
_SRC_RE = re.compile(r"""<(?:img|source|video|audio)\b[^>]*?\bsrc\s*=\s*["']?([^"'>\s]+)""", re.I)
_CSS_URL_RE = re.compile(r"""url\(\s*["']?([^)"']+)""", re.I)
_REMOTE_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|/|#)", re.I)
_AUDIO_EXT = {".mp3", ".ogg", ".oga", ".opus", ".wav", ".flac", ".m4a", ".aac", ".mp4", ".webm",
              ".mkv", ".3gp", ".spx"}


@dataclass
class Check:
    id: str
    name: str
    status: str   # pass | warn (pre-existing defect, non-blocking) | fail | skip
    detail: str = ""


def _media_dir(collection: Path) -> Path:
    return collection.with_suffix(".media")


def _sha(lines) -> str:
    h = hashlib.sha256()
    for line in lines:
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _fields_hash(col: Collection) -> str:
    return _sha(f"{nid}\x1e{mid}\x1e{flds}\x1e{tags}"
                for nid, mid, flds, tags in col.db.all(
                    "select id, mid, flds, tags from notes order by id"))


def _deck_stats(col: Collection) -> dict[str, dict[str, int]]:
    counts = {col.decks.name(did): n for did, n in col.db.all(
        "select did, count() from cards group by did")}
    out: dict[str, dict[str, int]] = {}

    def walk(node, parent=""):
        path = f"{parent}::{node.name}" if parent else node.name
        out[path] = {"cards": counts.get(path, 0), "new": node.new_count,
                     "learning": node.learn_count, "review": node.review_count}
        for child in node.children:
            walk(child, path)

    root = col.sched.deck_due_tree()
    for child in (root.children if root else []):
        walk(child)
    for name, n in counts.items():          # decks pruned from the due tree still hold cards
        out.setdefault(name, {"cards": n, "new": 0, "learning": 0, "review": 0})
        out[name]["cards"] = n
    return out


def _top_level_decks(col: Collection) -> list[tuple[int, str]]:
    return sorted((d.id, d.name) for d in col.decks.all_names_and_ids() if "::" not in d.name)


def _queue_sample(col: Collection) -> dict[str, list[int]]:
    """Queue head per top-level deck. Anki's queue only covers the *current* deck, so sampling
    just that one (often empty Default) would let any collection pass M4."""
    out: dict[str, list[int]] = {}
    for did, name in _top_level_decks(col):
        col.decks.set_current(did)
        ids = [qc.card.id for qc in col.sched.get_queued_cards(fetch_limit=QUEUE_SAMPLE).cards]
        if ids:
            out[name] = ids
    return out


def _audio_refs(col: Collection) -> set[str]:
    return {m for (flds,) in col.db.all("select flds from notes") for m in _SOUND_RE.findall(flds)}


def _make_snapshot(col: Collection, media_dir: Path) -> dict:
    import anki.buildinfo
    media = [p for p in media_dir.iterdir() if p.is_file()] if media_dir.is_dir() else []
    return {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "anki_version": anki.buildinfo.version,
        "sched_today": col.sched.today,
        "schema_version": col.db.scalar("select ver from col"),
        "note_count": col.note_count(),
        "card_count": col.card_count(),
        "media_count": len(media),
        "audio_refs": len(_audio_refs(col)),
        "missing_audio": _missing(_audio_refs(col), media_dir, audio=True),
        "missing_media": _missing(_m7_refs(col)[0], media_dir),
        "fields_sha256": _fields_hash(col),
        "decks": _deck_stats(col),
        "queue_sample": _queue_sample(col),
    }


class _Copy:
    """Private working copy of collection + media; the source is only ever read."""

    def __init__(self, collection: Path) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ankiweb-accept-")
        self.root = Path(self._tmp.name)
        self.path = self.root / "collection.anki2"
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(collection) + suffix)
            if src.exists():
                shutil.copy2(src, Path(str(self.path) + suffix))
        media = _media_dir(collection)
        if media.is_dir():
            shutil.copytree(media, _media_dir(self.path))

    def cleanup(self) -> None:
        self._tmp.cleanup()


def snapshot(collection: Path) -> dict:
    """Counts + hashes of ``collection`` (read from a private copy; the source is not opened)."""
    work = _Copy(collection)
    try:
        col = Collection(str(work.path))
        try:
            return _make_snapshot(col, _media_dir(work.path))
        finally:
            col.close()
    finally:
        work.cleanup()


def write_snapshot(collection: Path, out: Path) -> dict:
    snap = snapshot(collection)
    out.write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n")
    return snap


def _plausible_audio(path: Path) -> bool:
    if path.suffix.lower() not in _AUDIO_EXT:
        return True
    with path.open("rb") as fh:
        head = fh.read(16)
    return (head.startswith((b"ID3", b"OggS", b"fLaC", b"\x1a\x45\xdf\xa3"))
            or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)   # mp3/aac sync
            or (head[:4] == b"RIFF" and head[8:12] == b"WAVE")
            or head[4:8] == b"ftyp")


def _missing(names, media_dir: Path, *, audio: bool = False) -> list[str]:
    bad = []
    for name in sorted(names):
        p = media_dir / unquote(name)
        if not p.is_file() or p.stat().st_size == 0 or (audio and not _plausible_audio(p)):
            bad.append(name)
    return bad


def _lost_or_inherited(missing: list[str], known: list[str], kind: str) -> tuple[str, str]:
    """Acceptance asks "did the migration lose anything?": files already missing in the source
    are the collection's own defect (WARN, still surfaced); newly missing ones are a regression."""
    lost = [m for m in missing if m not in set(known)]
    if lost:
        return "fail", f"missing/undecodable {kind} not missing in source: {_fmt(lost)}"
    return "warn", f"pre-existing in source, not a migration loss - missing {kind}: {_fmt(missing)}"


def _local(refs) -> set[str]:
    return {r for r in refs if r and not _REMOTE_RE.match(r)}


def _fmt(items, limit: int = 8) -> str:
    items = list(items)
    return ", ".join(items[:limit]) + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


def _m7_refs(col: Collection) -> tuple[set[str], int, list[str]]:
    refs, rendered, errors = set(), 0, []
    for mid_name in col.models.all_names_and_ids():
        mid = mid_name.id
        css = col.models.get(mid).get("css", "")
        refs |= _local(_CSS_URL_RE.findall(css))
        for cid in col.find_cards(f"mid:{mid}")[:RENDER_SAMPLE_PER_NOTETYPE]:
            try:
                card = col.get_card(cid)
                html = card.question() + card.answer()
                av = [t.filename for t in card.question_av_tags() + card.answer_av_tags()
                      if isinstance(t, SoundOrVideoTag)]
            except Exception as exc:       # a template that cannot render is a finding
                errors.append(f"card {cid}: {exc}")
                continue
            rendered += 1
            refs |= _local(_SRC_RE.findall(html)) | set(av)
    return refs, rendered, errors


def _answer_one(col: Collection) -> tuple[bool, str]:
    for did, _name in _top_level_decks(col):
        col.decks.set_current(did)
        queued = col.sched.get_queued_cards(fetch_limit=1)
        if queued.cards:
            break
    else:
        return False, "no due or new cards to answer"
    top = queued.cards[0]
    card = col.get_card(top.card.id)
    card.start_timer()
    col.sched.answer_card(col.sched.build_answer(
        card=card, states=top.states, rating=CardAnswer.Rating.GOOD))
    return True, str(card.id)


def verify(collection: Path, baseline: dict, *, restored: bool = False) -> list[Check]:
    checks: list[Check] = []
    work = _Copy(collection)
    media = _media_dir(work.path)
    col = Collection(str(work.path))

    def run(cid: str, name: str, fn) -> None:
        try:
            status, detail = fn()
        except Exception as exc:
            status, detail = "fail", f"{type(exc).__name__}: {exc}"
        checks.append(Check(cid, name, status, detail))

    def eq(label: str, got, want):
        return ("pass", f"{label} = {got}") if got == want else (
            "fail", f"{label}: expected {want}, found {got}")

    same_day = col.sched.today == baseline.get("sched_today")
    try:
        run("M1", "note count matches", lambda: eq("notes", col.note_count(), baseline["note_count"]))
        run("M2", "card count matches", lambda: eq("cards", col.card_count(), baseline["card_count"]))

        def m3():
            got, want = _deck_stats(col), baseline["decks"]
            if not same_day:           # due counts only mean something on the snapshot's day
                got = {k: {"cards": v["cards"]} for k, v in got.items()}
                want = {k: {"cards": v["cards"]} for k, v in want.items()}
            diff = sorted(k for k in got.keys() | want.keys() if got.get(k) != want.get(k))
            note = "" if same_day else " (day rolled over: structure + card counts only)"
            return ("pass", f"{len(want)} decks match{note}") if not diff else (
                "fail", f"decks differ: {_fmt(diff)}")
        run("M3", "deck tree + scheduling counts", m3)

        def m4():
            if not same_day:
                return "skip", "day rolled over since snapshot; queue order not comparable"
            return eq("queue head", _queue_sample(col), baseline["queue_sample"])
        run("M4", "due/new/learning queue sample", m4)

        def m5():
            if _fields_hash(col) != baseline["fields_sha256"]:
                return "fail", "note fields/tags differ from baseline"
            drift = []
            for nid, flds in col.db.all("select id, flds from notes order by id"):
                if "\x1f".join(col.get_note(nid).fields) != flds:
                    drift.append(str(nid))
            non_ascii = [nid for nid, flds in col.db.all("select id, flds from notes")
                         if not flds.isascii()][:20]
            for nid in non_ascii:                       # unchanged save must not alter text
                col.update_note(col.get_note(nid))
            if _fields_hash(col) != baseline["fields_sha256"]:
                drift.append("after-save")
            return ("pass", f"{len(non_ascii)} non-ASCII notes stable") if not drift else (
                "fail", f"round-trip drift: {_fmt(drift)}")
        run("M5", "Unicode fields round-trip unchanged", m5)

        def m6():
            refs = _audio_refs(col)
            bad = _missing(refs, media, audio=True)
            if not bad:
                return "pass", (f"{len(refs)} audio file(s) present and plausible; "
                                "iOS/Android playback is a manual check")
            return _lost_or_inherited(bad, baseline.get("missing_audio", []), "audio")
        run("M6", "audio files present and decodable", m6)

        def m7():
            refs, rendered, errors = _m7_refs(col)
            bad = _missing(refs, media)
            if errors:
                return "fail", f"render errors: {_fmt(errors, 3)}"
            if bad:
                return _lost_or_inherited(bad, baseline.get("missing_media", []), "media")
            return "pass", f"{rendered} cards rendered, {len(refs)} media refs resolve"
        run("M7", "templates/CSS render with local media", m7)

        def m8():
            nonlocal col
            before = col.db.scalar("select count() from revlog")
            answered, info = _answer_one(col)
            if not answered:
                return "skip", info
            col.close()
            col = Collection(str(work.path))          # a fresh open proves it hit disk
            after = col.db.scalar("select count() from revlog")
            reps = col.get_card(int(info)).reps
            return ("pass", "answer persisted across reopen") if after == before + 1 and reps >= 1 \
                else ("fail", f"revlog {before}->{after}, reps={reps}")
        run("M8", "review answer persisted after restart", m8)

        def m9():
            out = work.root / "export.colpkg"
            col.export_collection_package(str(out), include_media=True, legacy=False)  # closes col
            from anki._backend import RustBackend
            target = work.root / "imported"
            target.mkdir()
            RustBackend().import_collection_package(
                col_path=str(target / "collection.anki2"), backup_path=str(out),
                media_folder=str(target / "collection.media"),
                media_db=str(target / "collection.media.db2"))
            imported = Collection(str(target / "collection.anki2"))
            try:
                got = (imported.note_count(), imported.card_count(), _fields_hash(imported))
            finally:
                imported.close()
            want = (baseline["note_count"], baseline["card_count"], baseline["fields_sha256"])
            return ("pass", "colpkg re-imports with identical counts and fields "
                            "(pylib engine; desktop-GUI import is manual)") if got == want else (
                "fail", f"re-imported {got[:2]} vs baseline {want[:2]}, fields equal={got[2] == want[2]}")
        run("M9", ".colpkg export re-imports", m9)
    finally:
        try:
            col.close()
        except Exception:
            pass
        work.cleanup()

    failed = [c.id for c in checks if c.status == "fail"]
    warned = [c.id for c in checks if c.status == "warn"]
    if restored:
        ok = "restored copy passed M1-M9" + (
            f" with {len(warned)} warning(s): {', '.join(warned)}" if warned else "")
        checks.append(Check("M10", "restore reproduces M1-M9", "fail" if failed else "pass",
                            f"failed: {', '.join(failed)}" if failed else ok))
    else:
        checks.append(Check("M10", "restore reproduces M1-M9", "skip",
                            "run `verify --restored` on a restored backup (scripts/restore-drill.sh)"))
    return checks


def _report(checks: list[Check]) -> str:
    return "\n".join(f"{c.id:<4}{c.status.upper():<5}{c.name} - {c.detail}" for c in checks)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ankiweb.adapters.anki.acceptance", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="record counts/hashes from a known-good collection")
    s.add_argument("--collection", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True)
    v = sub.add_parser("verify", help="check a collection copy against a snapshot")
    v.add_argument("--collection", type=Path, required=True)
    v.add_argument("--baseline", type=Path, required=True)
    v.add_argument("--restored", action="store_true", help="input is a restored backup (reports M10)")
    v.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "snapshot":
        snap = write_snapshot(args.collection, args.out)
        print(f"baseline written: {snap['note_count']} notes, {snap['card_count']} cards, "
              f"{snap['media_count']} media -> {args.out}")
        return 0
    checks = verify(args.collection, json.loads(args.baseline.read_text()), restored=args.restored)
    print(json.dumps([c.__dict__ for c in checks], indent=2) if args.json else _report(checks))
    return 1 if any(c.status == "fail" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
