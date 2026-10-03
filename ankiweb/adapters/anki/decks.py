"""Anki-backed ``DeckCatalog``. Duck-typed on the collection; every read goes through the gateway."""
from __future__ import annotations

from ankiweb.domain.models import DeckCounts, DeckNode
from ankiweb.domain.ports import CollectionGateway


def _convert(node, parent_path: str = "") -> DeckNode:
    path = f"{parent_path}::{node.name}" if parent_path else node.name
    return DeckNode(
        id=node.deck_id, name=node.name, path=path, filtered=bool(node.filtered),
        counts=DeckCounts(node.new_count, node.learn_count, node.review_count),
        children=[_convert(child, path) for child in node.children])


def _read_tree(col) -> list[DeckNode]:
    root = col.sched.deck_due_tree()
    return [_convert(child) for child in root.children] if root is not None else []


def _read_description(col, deck_id: int) -> str | None:
    deck = col.decks.get(deck_id, default=False)
    return deck.get("desc", "") if deck else None


class AnkiDeckCatalog:
    def __init__(self, gateway: CollectionGateway) -> None:
        self._gateway = gateway

    async def tree(self) -> list[DeckNode]:
        return await self._gateway.run(_read_tree)

    async def description(self, deck_id: int) -> str | None:
        return await self._gateway.run(lambda col: _read_description(col, deck_id))
