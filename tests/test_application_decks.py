"""DeckService against a fake catalog: proves the application layer needs no Anki or HTTP."""
import pytest

from ankiweb.application.decks import DeckService
from ankiweb.domain.models import DeckCounts, DeckNode


def _tree() -> list[DeckNode]:
    verbs = DeckNode(id=3, name="Verbs", path="Lang::Verbs", counts=DeckCounts(1, 2, 3))
    lang = DeckNode(id=2, name="Lang", path="Lang", counts=DeckCounts(4, 5, 6), children=[verbs])
    other = DeckNode(id=5, name="Other", path="Other", counts=DeckCounts(0, 0, 7), filtered=True)
    return [lang, other]


class FakeCatalog:
    def __init__(self, tree, descriptions=None):
        self._tree = tree
        self._desc = descriptions or {}

    async def tree(self):
        return self._tree

    async def description(self, deck_id: int):
        return self._desc.get(deck_id)


async def test_listing_totals_are_sum_of_top_level_decks():
    listing = await DeckService(FakeCatalog(_tree())).list()
    assert [d.id for d in listing.decks] == [2, 5]
    assert listing.counts == DeckCounts(new=4, learning=5, review=13)


async def test_listing_empty_collection_has_zero_counts():
    listing = await DeckService(FakeCatalog([])).list()
    assert listing.decks == [] and listing.counts == DeckCounts(0, 0, 0)


async def test_get_finds_nested_deck_with_description():
    svc = DeckService(FakeCatalog(_tree(), {3: "conjugations"}))
    detail = await svc.get(3)
    assert detail is not None
    assert detail.deck.path == "Lang::Verbs"
    assert detail.counts == DeckCounts(1, 2, 3)
    assert detail.description == "conjugations"


async def test_get_missing_deck_returns_none():
    assert await DeckService(FakeCatalog(_tree())).get(999) is None


async def test_get_description_defaults_to_empty_string():
    detail = await DeckService(FakeCatalog(_tree())).get(5)
    assert detail is not None and detail.description == "" and detail.deck.filtered is True
