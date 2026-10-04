from __future__ import annotations

from pathlib import Path

from anki.collection import Collection


def create_empty_collection(path: Path) -> None:
    """Create and close a valid empty Anki collection at ``path``."""
    collection = Collection(str(path), server=False)
    collection.close()
