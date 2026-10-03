"""Generated, sanitized fixture collection (Greek text + audio + image) shared by T6/T11 tests."""
from pathlib import Path

from anki.collection import Collection

GREEK = ["καλημέρα", "ευχαριστώ", "παρακαλώ", "Γειά σου"]
MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def build(root: Path) -> Path:
    """collection.anki2 + collection.media in ``root``; returns the collection path."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "collection.anki2"
    col = Collection(str(path))
    try:
        media = Path(col.media.dir())
        (media / "lt01.mp3").write_bytes(MP3)
        (media / "pic.png").write_bytes(PNG)
        deck = col.decks.id("Greek::Basics")
        for i, word in enumerate(GREEK):
            note = col.new_note(col.models.by_name("Basic"))
            note["Front"] = f"{word} [sound:lt01.mp3]"
            note["Back"] = f"word {i} <img src=\"pic.png\">"
            col.add_note(note, deck)
    finally:
        col.close()
    return path
