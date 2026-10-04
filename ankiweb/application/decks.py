"""Deck use cases. Pure: depends only on the ``DeckCatalog`` port (no Anki, no HTTP)."""
from __future__ import annotations
from dataclasses import dataclass

from ankiweb.domain.models import DeckCounts, DeckNode
from ankiweb.domain.ports import DeckCatalog


@dataclass(frozen=True)
class DeckListing:
    decks: list[DeckNode]
    counts: DeckCounts


@dataclass(frozen=True)
class DeckDetail:
    deck: DeckNode
    counts: DeckCounts
    description: str


def _find(nodes: list[DeckNode], deck_id: int) -> DeckNode | None:
    for node in nodes:
        if node.id == deck_id:
            return node
        if (hit := _find(node.children, deck_id)) is not None:
            return hit
    return None


class DeckService:
    def __init__(self, catalog: DeckCatalog) -> None:
        self._catalog = catalog

    async def list(self) -> DeckListing:
        decks = await self._catalog.tree()
        # Anki's parent counts already include their children, so only top-level decks add up.
        return DeckListing(decks, DeckCounts(
            new=sum(d.counts.new for d in decks),
            learning=sum(d.counts.learning for d in decks),
            review=sum(d.counts.review for d in decks)))

    async def get(self, deck_id: int) -> DeckDetail | None:
        node = _find(await self._catalog.tree(), deck_id)
        if node is None:
            return None
        return DeckDetail(node, node.counts, await self._catalog.description(deck_id) or "")
