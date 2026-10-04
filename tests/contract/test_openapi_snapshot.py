"""Contract gate: the /api/v1 OpenAPI document is checked in and must not drift silently.

Intentional change? Regenerate with ``UPDATE_OPENAPI_SNAPSHOT=1 pytest tests/contract`` and
commit packages/contracts/openapi-v1.json (additive only within /v1, SPEC section 7).
"""
import json
import os
from pathlib import Path

from ankiweb.api.contract import v1_openapi
from ankiweb.app import create_app
from ankiweb.config import Settings

SNAPSHOT = Path(__file__).resolve().parents[2] / "packages/contracts/openapi-v1.json"


def _actual(tmp_path):
    return v1_openapi(create_app(Settings(collection_path=tmp_path / "c.anki2")))


def test_openapi_v1_matches_snapshot(tmp_path):
    actual = _actual(tmp_path)
    if os.environ.get("UPDATE_OPENAPI_SNAPSHOT"):
        SNAPSHOT.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
    assert SNAPSHOT.exists(), "missing snapshot: run with UPDATE_OPENAPI_SNAPSHOT=1"
    assert actual == json.loads(SNAPSHOT.read_text()), (
        "/api/v1 contract changed; if intentional run UPDATE_OPENAPI_SNAPSHOT=1 and commit")


def test_snapshot_contains_only_v1_paths_and_resolved_refs(tmp_path):
    doc = _actual(tmp_path)
    assert doc["paths"] and all(p.startswith("/api/v1/") for p in doc["paths"])
    refs = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "$ref":
                    refs.add(v.rsplit("/", 1)[-1])
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(doc)
    assert refs <= set(doc["components"]["schemas"])
