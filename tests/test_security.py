"""V8 (CSRF / origin), fail-closed auth startup, and baseline response headers."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ankiweb.app import create_app
from ankiweb.config import Settings

HOST = "testserver"


def _client(tmp_path: Path, password: str = "secret", **kw) -> TestClient:
    settings = Settings(collection_path=tmp_path / "c.anki2", password=password, **kw)
    c = TestClient(create_app(settings), base_url=f"http://{HOST}")
    return c


def _login(c: TestClient) -> None:
    r = c.post("/login", data={"password": "secret"}, follow_redirects=False,
               headers={"Origin": f"http://{HOST}"})
    assert r.status_code == 303


# A harmless authenticated mutation target: notify config form.
MUTATE = "/notify"


def test_cross_origin_post_rejected(tmp_path: Path):
    with _client(tmp_path) as c:
        _login(c)
        r = c.post(MUTATE, data={}, headers={"Origin": "http://evil.example"},
                   follow_redirects=False)
        assert r.status_code == 403


def test_cross_site_fetch_metadata_rejected(tmp_path: Path):
    with _client(tmp_path) as c:
        _login(c)
        r = c.post(MUTATE, data={}, headers={"Sec-Fetch-Site": "cross-site",
                                              "Origin": f"http://{HOST}"},
                   follow_redirects=False)
        assert r.status_code == 403


def test_same_origin_post_allowed(tmp_path: Path):
    with _client(tmp_path) as c:
        _login(c)
        r = c.post(MUTATE, data={}, headers={"Origin": f"http://{HOST}"},
                   follow_redirects=False)
        assert r.status_code != 403


def test_same_origin_via_referer_allowed(tmp_path: Path):
    with _client(tmp_path) as c:
        _login(c)
        r = c.post(MUTATE, data={}, headers={"Referer": f"http://{HOST}/notify"},
                   follow_redirects=False)
        assert r.status_code != 403


def test_authenticated_post_without_any_origin_signal_rejected(tmp_path: Path):
    with _client(tmp_path) as c:
        _login(c)
        assert c.post(MUTATE, data={}, follow_redirects=False).status_code == 403


def test_origin_must_match_host_not_just_be_local(tmp_path: Path):
    """localhost is a valid Host for the guard, but must not authorise a different origin."""
    with _client(tmp_path) as c:
        _login(c)
        r = c.post(MUTATE, data={}, headers={"Origin": "http://localhost:9999"},
                   follow_redirects=False)
        assert r.status_code == 403


def test_origin_must_match_current_host_even_when_both_hosts_are_allowed(tmp_path: Path):
    with _client(tmp_path, allowed_hosts=("anki.lan",)) as c:
        _login(c)
        r = c.post(MUTATE, data={}, headers={"Origin": "https://anki.lan"},
                   follow_redirects=False)
        assert r.status_code == 403


def test_allowed_host_accepts_its_own_origin(tmp_path: Path):
    settings = Settings(collection_path=tmp_path / "c.anki2", password="secret",
                        allowed_hosts=("anki.lan",))
    with TestClient(create_app(settings), base_url="https://anki.lan") as c:
        r = c.post("/login", data={"password": "secret"},
                   headers={"Origin": "https://anki.lan"}, follow_redirects=False)
        assert r.status_code == 303


def test_get_requests_not_origin_checked(tmp_path: Path):
    with _client(tmp_path) as c:
        _login(c)
        r = c.get("/deckbrowser", headers={"Origin": "http://evil.example"})
        assert r.status_code == 200


def test_cross_origin_post_rejected_even_when_unauthenticated(tmp_path: Path):
    with _client(tmp_path) as c:
        r = c.post("/login", data={"password": "secret"},
                   headers={"Origin": "http://evil.example"}, follow_redirects=False)
        assert r.status_code == 403


def test_login_without_origin_still_works_for_scripts(tmp_path: Path):
    """No session yet, no browser signals: allowed (curl / health tooling)."""
    with _client(tmp_path) as c:
        r = c.post("/login", data={"password": "secret"}, follow_redirects=False)
        assert r.status_code == 303


# --- response headers -------------------------------------------------------

@pytest.mark.parametrize("path", ["/healthz", "/login", "/deckbrowser"])
def test_baseline_security_headers(tmp_path: Path, path: str):
    with _client(tmp_path) as c:
        r = c.get(path, follow_redirects=False)
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "same-origin"
        assert r.headers["x-frame-options"] == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in r.headers["content-security-policy"]
        assert "strict-transport-security" not in r.headers


def test_hsts_only_when_secure_cookie_enabled(tmp_path: Path):
    with _client(tmp_path, secure_cookie=True) as c:
        r = c.get("/healthz")
        assert r.headers["strict-transport-security"].startswith("max-age=")


def test_forbidden_host_response_also_has_headers(tmp_path: Path):
    with _client(tmp_path) as c:
        r = c.get("/healthz", headers={"Host": "evil.example"})
        assert r.status_code == 403
        assert r.headers["x-content-type-options"] == "nosniff"


# --- fail-closed startup ----------------------------------------------------

def test_auth_error_when_no_password_configured(tmp_path: Path):
    s = Settings(collection_path=tmp_path / "c.anki2")
    assert "ANKIWEB_PASSWORD" in s.auth_error()


def test_auth_error_for_placeholder_password(tmp_path: Path):
    s = Settings(collection_path=tmp_path / "c.anki2", password="change-me")
    assert "placeholder" in s.auth_error()


def test_no_auth_error_with_password_or_hash(tmp_path: Path):
    from argon2 import PasswordHasher
    assert Settings(collection_path=tmp_path / "c", password="a-real-secret").auth_error() is None
    assert Settings(
        collection_path=tmp_path / "c",
        password_hash=PasswordHasher().hash("secret"),
    ).auth_error() is None


def test_invalid_argon2_hash_is_rejected_at_startup(tmp_path: Path):
    problem = Settings(collection_path=tmp_path / "c", password_hash="$argon2id$x").auth_error()
    assert problem and "valid Argon2id" in problem


def test_auth_error_cleared_by_explicit_opt_out(tmp_path: Path):
    s = Settings(collection_path=tmp_path / "c.anki2", auth_disabled=True)
    assert s.auth_error() is None


def test_from_env_reads_auth_disabled(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ANKIWEB_COLLECTION", str(tmp_path / "c.anki2"))
    monkeypatch.setenv("ANKIWEB_AUTH_DISABLED", "1")
    assert Settings.from_env().auth_disabled is True
