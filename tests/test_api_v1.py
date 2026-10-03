"""Thin /api/v1: health, session, JSON auth, decks (SPEC section 7 subset, ADR 0001)."""
from pathlib import Path

import pytest
from anki.collection import Collection
from fastapi.testclient import TestClient

from ankiweb.app import create_app
from ankiweb.auth import COOKIE
from ankiweb.config import Settings

HOST = "testserver"
ORIGIN = {"Origin": f"http://{HOST}"}


@pytest.fixture
def col_path(tmp_path: Path) -> Path:
    path = tmp_path / "c.anki2"
    col = Collection(str(path))
    try:
        parent = col.decks.id("Lang")
        child = col.decks.id("Lang::Verbs")
        col.decks.update_dict({**col.decks.get(child), "desc": "conjugations"})
        for deck in (parent, child):
            note = col.new_note(col.models.by_name("Basic"))
            note["Front"], note["Back"] = f"q{deck}", "a"
            col.add_note(note, deck)
    finally:
        col.close()
    return path


def _client(col_path: Path, password: str = "") -> TestClient:
    settings = Settings(collection_path=col_path, password=password)
    return TestClient(create_app(settings), base_url=f"http://{HOST}")


def test_health_live_needs_no_auth(col_path):
    with _client(col_path, "secret") as c:
        r = c.get("/api/v1/health/live")
        assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_health_ready_when_collection_open(col_path):
    with _client(col_path, "secret") as c:
        r = c.get("/api/v1/health/ready")
        assert r.status_code == 200 and r.json() == {"status": "ready"}


def test_health_ready_503_when_worker_fails(col_path):
    with _client(col_path) as c:
        async def boom(fn):
            raise RuntimeError("collection closed")
        c.app.state.service.run = boom
        r = c.get("/api/v1/health/ready")
        assert r.status_code == 503 and r.json()["status"] == "unavailable"


def test_protected_routes_return_json_401_not_html_redirect(col_path):
    with _client(col_path, "secret") as c:
        for path in ("/api/v1/session", "/api/v1/decks", "/api/v1/decks/1"):
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 401, path
            assert r.json() == {"detail": "unauthenticated"}


def test_session_for_anonymous_when_auth_disabled(col_path):
    with _client(col_path) as c:
        r = c.get("/api/v1/session")
        assert r.status_code == 200
        body = r.json()
        assert body["user"] == {"id": "local"} and body["authenticated"] is True
        assert body["authRequired"] is False


def test_json_login_session_logout_roundtrip(col_path):
    with _client(col_path, "secret") as c:
        bad = c.post("/api/v1/auth/login", json={"password": "nope"}, headers=ORIGIN)
        assert bad.status_code == 401
        ok = c.post("/api/v1/auth/login", json={"password": "secret"}, headers=ORIGIN)
        assert ok.status_code == 204 and ok.cookies.get(COOKIE)
        me = c.get("/api/v1/session")
        assert me.status_code == 200 and me.json()["authRequired"] is True
        out = c.post("/api/v1/auth/logout", headers=ORIGIN)
        assert out.status_code == 204
        assert c.get("/api/v1/session").status_code == 401


def test_json_login_is_rate_limited(col_path):
    with _client(col_path, "secret") as c:
        codes = [c.post("/api/v1/auth/login", json={"password": "x"}, headers=ORIGIN).status_code
                 for _ in range(10)]
        assert codes[:8] == [401] * 8 and 429 in codes[8:]


def test_json_login_cross_origin_blocked(col_path):
    with _client(col_path, "secret") as c:
        r = c.post("/api/v1/auth/login", json={"password": "secret"},
                   headers={"Origin": "http://evil.example"})
        assert r.status_code == 403


def test_decks_tree_with_counts(col_path):
    with _client(col_path) as c:
        r = c.get("/api/v1/decks")
        assert r.status_code == 200
        body = r.json()
        names = {d["path"]: d for d in body["decks"]}
        lang = names["Lang"]
        assert lang["name"] == "Lang" and lang["filtered"] is False
        assert lang["counts"] == {"new": 2, "learning": 0, "review": 0}
        assert [ch["path"] for ch in lang["children"]] == ["Lang::Verbs"]
        assert lang["children"][0]["counts"]["new"] == 1
        assert body["counts"]["new"] == 2


def test_deck_detail_includes_description(col_path):
    with _client(col_path) as c:
        child = c.get("/api/v1/decks").json()["decks"][0]["children"][0]
        r = c.get(f"/api/v1/decks/{child['id']}")
        assert r.status_code == 200
        body = r.json()
        assert body["deck"]["path"] == "Lang::Verbs"
        assert body["description"] == "conjugations"
        assert body["counts"] == {"new": 1, "learning": 0, "review": 0}


def test_unknown_deck_is_404(col_path):
    with _client(col_path) as c:
        r = c.get("/api/v1/decks/987654321")
        assert r.status_code == 404 and r.json() == {"detail": "deck not found"}
