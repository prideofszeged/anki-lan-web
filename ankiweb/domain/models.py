"""Transport- and Anki-independent value types (SPEC section 5.2 ``domain/``)."""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DeckCounts:
    new: int
    learning: int
    review: int


@dataclass(frozen=True)
class DeckNode:
    id: int
    name: str          # last path component
    path: str          # full "Parent::Child" name
    counts: DeckCounts
    filtered: bool = False
    children: list["DeckNode"] = field(default_factory=list)
