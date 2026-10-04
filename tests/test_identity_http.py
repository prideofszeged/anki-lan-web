from __future__ import annotations

from datetime import datetime, timezone

from argon2 import PasswordHasher
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from ankiweb.identity import IdentityDatabase, IdentityRepository, IdentityService
from ankiweb.identity.http import CSRF_HEADER, IDENTITY_COOKIE, build_identity_http

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def _stack(path, *, bootstrap=False, attempt_gate=None, login_succeeded=None,
           invite_attempt_gate=None):
    repo = IdentityRepository(IdentityDatabase(path))
    service = IdentityService(
        repo,
        password_hasher=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1),
        clock=lambda: NOW,
    )
    service.initialize()
    if bootstrap:
        service.bootstrap_owner(password="safe-password")
    boundary = build_identity_http(
        service, secure_cookie=False, attempt_gate=attempt_gate,
        login_succeeded=login_succeeded, invite_attempt_gate=invite_attempt_gate,
    )
    app = FastAPI()
    app.include_router(boundary.router)

    @app.get("/protected")
    async def protected(principal=Depends(boundary.require_principal)):
        return {"user_id": principal.user.id}

    return service, boundary, TestClient(app)


def _login(client: TestClient, username="local", password="safe-password"):
    response = client.post("/api/v1/auth/login", json={
        "username": username, "password": password,
    })
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def test_login_cookie_authentication_and_restart_durability(tmp_path):
    path = tmp_path / "app.db"
    _, _, client = _stack(path, bootstrap=True)
    csrf = _login(client)
    cookie = client.cookies.get(IDENTITY_COOKIE)
    csrf_cookie = client.cookies.get(f"{IDENTITY_COOKIE}_csrf")
    assert cookie and cookie != "safe-password"
    assert csrf_cookie == csrf
    set_cookie = client.post("/api/v1/auth/login", json={
        "username": "local", "password": "safe-password",
    }).headers["set-cookie"]
    assert "HttpOnly" in set_cookie and "SameSite=strict" in set_cookie
    assert csrf
    assert client.get("/api/v1/auth/me").json()["username"] == "local"
    assert client.get("/protected").status_code == 200

    # A new app/service instance accepts the durable opaque session.
    _, _, restarted = _stack(path)
    restarted.cookies.set(IDENTITY_COOKIE, cookie)
    assert restarted.get("/protected").status_code == 200


def test_typed_auth_errors_and_login_callbacks(tmp_path):
    calls = []
    gate_open = {"value": False}

    def gate(_request):
        return gate_open["value"]

    def succeeded(_request):
        calls.append("success")

    _, _, client = _stack(
        tmp_path / "app.db", bootstrap=True, attempt_gate=gate, login_succeeded=succeeded,
    )
    unauthenticated = client.get("/api/v1/auth/me")
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["detail"]["code"] == "authentication_required"
    limited = client.post("/api/v1/auth/login", json={
        "username": "local", "password": "safe-password",
    })
    assert limited.status_code == 429
    assert limited.json()["detail"]["code"] == "rate_limited"
    gate_open["value"] = True
    wrong = client.post("/api/v1/auth/login", json={
        "username": "local", "password": "not-the-password",
    })
    assert wrong.status_code == 401
    assert wrong.json()["detail"]["code"] == "invalid_credentials"
    _login(client)
    assert calls == ["success"]


def test_invalid_normalized_username_is_invalid_credentials_not_server_error(tmp_path):
    _, _, client = _stack(tmp_path / "app.db", bootstrap=True)
    for username in (" ", "a b", "\\"):
        response = client.post("/api/v1/auth/login", json={
            "username": username, "password": "safe-password",
        })
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "invalid_credentials"


def test_csrf_is_required_rotatable_and_logout_revokes(tmp_path):
    _, _, client = _stack(tmp_path / "app.db", bootstrap=True)
    csrf = _login(client)
    assert client.post("/api/v1/auth/csrf").status_code == 403
    rotated = client.post(
        "/api/v1/auth/csrf", headers={CSRF_HEADER: csrf},
    )
    assert rotated.status_code == 200
    new_csrf = rotated.json()["csrf_token"]
    assert new_csrf != csrf
    assert client.cookies.get(f"{IDENTITY_COOKIE}_csrf") == new_csrf
    assert client.post("/api/v1/auth/logout", headers={CSRF_HEADER: csrf}).status_code == 403
    logout = client.post("/api/v1/auth/logout", headers={CSRF_HEADER: new_csrf})
    assert logout.status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401


def test_authenticated_get_recovers_missing_raw_csrf_token(tmp_path):
    _, _, client = _stack(tmp_path / "app.db", bootstrap=True)
    old = _login(client)
    client.cookies.delete(f"{IDENTITY_COOKIE}_csrf")
    recovered = client.get("/api/v1/auth/csrf")
    assert recovered.status_code == 200
    new = recovered.json()["csrf_token"]
    assert new != old
    assert client.cookies.get(f"{IDENTITY_COOKIE}_csrf") == new
    assert client.post(
        "/api/v1/auth/logout", headers={CSRF_HEADER: new},
    ).status_code == 204


def test_session_listing_and_owner_scoped_revocation(tmp_path):
    path = tmp_path / "app.db"
    _, _, first = _stack(path, bootstrap=True)
    csrf_first = _login(first)
    _, _, second = _stack(path)
    _login(second)
    listing = first.get("/api/v1/auth/sessions")
    assert listing.status_code == 200
    sessions = listing.json()["sessions"]
    assert len(sessions) == 2
    current = next(item for item in sessions if item["current"])
    other = next(item for item in sessions if not item["current"])

    revoked = first.delete(
        f"/api/v1/auth/sessions/{other['id']}", headers={CSRF_HEADER: csrf_first},
    )
    assert revoked.status_code == 204
    assert second.get("/api/v1/auth/me").status_code == 401
    missing = first.delete(
        "/api/v1/auth/sessions/not-a-session", headers={CSRF_HEADER: csrf_first},
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "session_not_found"
    assert current["id"] in {item["id"] for item in first.get(
        "/api/v1/auth/sessions").json()["sessions"]}


def test_account_invite_create_accept_replay_and_revoke(tmp_path):
    path = tmp_path / "app.db"
    _, _, owner = _stack(path, bootstrap=True)
    csrf = _login(owner)
    created = owner.post("/api/v1/admin/account-invites", headers={CSRF_HEADER: csrf}, json={
        "intended_username": "alice", "global_role": "user",
    })
    assert created.status_code == 201, created.text
    token = created.json()["token"]

    _, _, invitee = _stack(path)
    accepted = invitee.post("/api/v1/account-invites/accept", json={
        "token": token, "password": "alice-password", "display_name": "Alice",
    })
    assert accepted.status_code == 201
    assert accepted.json()["username"] == "alice"
    replay = invitee.post("/api/v1/account-invites/accept", json={
        "token": token, "password": "alice-password",
    })
    assert replay.status_code == 400
    assert replay.json()["detail"]["code"] == "invalid_invite"
    assert _login(invitee, "alice", "alice-password")

    second = owner.post("/api/v1/admin/account-invites", headers={CSRF_HEADER: csrf}, json={
        "intended_username": "bob",
    }).json()
    assert owner.delete(
        f"/api/v1/admin/account-invites/{second['id']}", headers={CSRF_HEADER: csrf},
    ).status_code == 204
    rejected = TestClient(invitee.app).post("/api/v1/account-invites/accept", json={
        "token": second["token"], "password": "bob-password-safe",
    })
    assert rejected.status_code == 400


def test_invite_accept_is_rate_limited_before_token_work(tmp_path):
    _, _, client = _stack(
        tmp_path / "app.db", bootstrap=True, invite_attempt_gate=lambda _request: False,
    )
    response = client.post("/api/v1/account-invites/accept", json={
        "token": "x" * 43,
        "password": "safe-password",
    })
    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "rate_limited"


def test_invalid_invite_username_is_typed_validation_error(tmp_path):
    _, _, owner = _stack(tmp_path / "app.db", bootstrap=True)
    csrf = _login(owner)
    response = owner.post(
        "/api/v1/admin/account-invites",
        headers={CSRF_HEADER: csrf},
        json={"intended_username": "a b"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_input"
