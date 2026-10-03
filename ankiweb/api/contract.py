"""Stable, self-contained OpenAPI document for ``/api/v1`` (input for generated clients)."""
from __future__ import annotations

from ankiweb.api.v1 import PREFIX


def _refs(node, out: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref":
                out.add(value.rsplit("/", 1)[-1])
            else:
                _refs(value, out)
    elif isinstance(node, list):
        for value in node:
            _refs(value, out)


def v1_openapi(app) -> dict:
    """Only /api/v1 paths plus the schemas they (transitively) reference, with fixed metadata
    so unrelated app routes or the app title never churn the checked-in snapshot."""
    full = app.openapi()
    paths = {p: v for p, v in full["paths"].items() if p.startswith(PREFIX + "/")}
    schemas = full.get("components", {}).get("schemas", {})
    wanted: set[str] = set()
    _refs(paths, wanted)
    pending = list(wanted)
    while pending:
        nested: set[str] = set()
        _refs(schemas[pending.pop()], nested)
        pending.extend(nested - wanted)
        wanted |= nested
    return {
        "openapi": full["openapi"],
        "info": {"title": "anki-lan-web API", "version": "1.0.0"},
        "paths": paths,
        "components": {"schemas": {name: schemas[name] for name in sorted(wanted)}},
    }
