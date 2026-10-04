"""Anki 26.09.3 adapter boundary.

Existing screen handlers still migrate incrementally; new modules must depend on
``CollectionGateway`` instead of importing pylib collection objects directly.
"""

from ankiweb.collection_service import CollectionService

__all__ = ["CollectionService"]
