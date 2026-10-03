"""Enforces SPEC V6/V7 and the dependency rule (transport -> application -> domain).

``LEGACY_ANKI_IMPORTERS`` is a ratchet over the code inherited from the upstream port: files may
only be *removed* from it (as they migrate behind ``CollectionGateway``), never added. A stale
entry fails the test so the list cannot silently outlive the code it excuses.
"""
from __future__ import annotations
import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent / "ankiweb"
SKIP_DIRS = {"web_assets", "_import_tmp", "shell", "__pycache__"}

# Files that predate the gateway and still reach into pylib directly. Shrink only.
LEGACY_ANKI_IMPORTERS = frozenset({
    "anki_rpc/handlers.py",
    "ankiconnect/actions/cards.py",
    "ankiconnect/actions/gui.py",
    "ankiconnect/actions/import_export.py",
    "ankiconnect/extra_actions/notes.py",
    "ankiconnect/extra_actions/scheduling.py",
    "collection_service.py",       # becomes adapters/anki/collection.py
    "i18n.py",
    "screens/custom_study.py",
    "screens/deckbrowser.py",
    "screens/filtered_deck.py",
    "screens/overview.py",
    "screens/preview.py",
    "screens/reviewer.py",
    "screens/routes.py",
})
ADAPTER_PREFIX = "adapters/anki/"

# Layers that must stay framework- and pylib-free, with what each may not import.
PURE_LAYERS = {
    "domain/": {"anki", "fastapi", "starlette", "uvicorn",
                "ankiweb.application", "ankiweb.screens", "ankiweb.anki_rpc",
                "ankiweb.bridge", "ankiweb.ankiconnect", "ankiweb.adapters"},
    "application/": {"anki", "fastapi", "starlette", "uvicorn",
                     "ankiweb.screens", "ankiweb.anki_rpc", "ankiweb.bridge",
                     "ankiweb.ankiconnect", "ankiweb.adapters"},
}


def imported_modules(source: str) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module)
    return mods


def _matches(mod: str, banned: str) -> bool:
    return mod == banned or mod.startswith(banned + ".")


def imports_anki(source: str) -> bool:
    return any(_matches(m, "anki") for m in imported_modules(source))


def _sources() -> dict[str, str]:
    out = {}
    for path in PKG.rglob("*.py"):
        rel = path.relative_to(PKG)
        if SKIP_DIRS & set(rel.parts):
            continue
        out[rel.as_posix()] = path.read_text(encoding="utf-8")
    return out


# --- scanner self-tests: prove the rules can actually fail -------------------

def test_scanner_distinguishes_anki_from_ankiweb():
    assert imports_anki("import anki.collection")
    assert imports_anki("from anki.collection import Collection")
    assert not imports_anki("from ankiweb.config import Settings")
    assert not imports_anki("import ankiconnect_like")


def test_scanner_sees_function_local_imports():
    assert imports_anki("def f():\n    from anki.notes import Note\n")


# --- repository rules -------------------------------------------------------

def test_new_modules_do_not_import_pylib_directly():
    offenders = sorted(
        rel for rel, src in _sources().items()
        if imports_anki(src) and rel not in LEGACY_ANKI_IMPORTERS
        and not rel.startswith(ADAPTER_PREFIX))
    assert not offenders, (
        "import anki only inside ankiweb/adapters/anki/ (use CollectionGateway elsewhere): "
        f"{offenders}")


def test_legacy_allowlist_has_no_stale_entries():
    sources = _sources()
    stale = sorted(rel for rel in LEGACY_ANKI_IMPORTERS
                   if rel not in sources or not imports_anki(sources[rel]))
    assert not stale, f"migrated away from pylib - remove from LEGACY_ANKI_IMPORTERS: {stale}"


@pytest.mark.parametrize("layer", sorted(PURE_LAYERS))
def test_pure_layers_respect_dependency_direction(layer: str):
    banned = PURE_LAYERS[layer]
    bad = {}
    for rel, src in _sources().items():
        if not rel.startswith(layer):
            continue
        hits = sorted(m for m in imported_modules(src) if any(_matches(m, b) for b in banned))
        if hits:
            bad[rel] = hits
    assert not bad, f"{layer} must not depend on transport/pylib: {bad}"
