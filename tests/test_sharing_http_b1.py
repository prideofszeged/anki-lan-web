from __future__ import annotations

from datetime import datetime, timezone

from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ankiweb.identity import IdentityDatabase, IdentityRepository, IdentityService
from ankiweb.identity.http import CSRF_HEADER, build_identity_http
from ankiweb.sharing import SharingRepository, SharingService
from ankiweb.sharing.http import build_sharing_router

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _app(tmp_path):
    database = IdentityDatabase(tmp_path / "app.db")
    identities = IdentityRepository(database)
    identity = IdentityService(
        identities,
        password_hasher=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1),
        clock=lambda: NOW,
    )
    identity.initialize()
    owner = identity.bootstrap_owner(username="owner", password="owner-password")
    invites = []
    for username in ("editor", "viewer", "stranger"):
        grant = identity.create_invite(actor_user_id=owner.id, intended_username=username)
        user = identity.accept_invite(token=grant.token, password=f"{username}-password")
        invites.append(user)
    identity_http = build_identity_http(identity, secure_cookie=False)
    sharing = SharingService(SharingRepository(database), clock=lambda: NOW)
    app = FastAPI()
    app.include_router(identity_http.router)
    app.include_router(build_sharing_router(sharing, identity_http))
    return app


def _login(app, username):
    client = TestClient(app)
    response = client.post("/api/v1/auth/login", json={
        "username": username, "password": f"{username}-password",
    })
    assert response.status_code == 200, response.text
    return client, response.json()["csrf_token"]


def test_share_api_horizontal_idor_and_csrf_matrix(tmp_path):
    app = _app(tmp_path)
    owner, owner_csrf = _login(app, "owner")
    created = owner.post("/api/v1/shares", headers={CSRF_HEADER: owner_csrf}, json={
        "name": "Greek A1",
    })
    assert created.status_code == 201, created.text
    share_id = created.json()["id"]

    stranger, stranger_csrf = _login(app, "stranger")
    assert stranger.get(f"/api/v1/shares/{share_id}").status_code == 404
    assert stranger.post(
        f"/api/v1/shares/{share_id}/invitations",
        headers={CSRF_HEADER: stranger_csrf},
        json={"role": "viewer"},
    ).status_code == 404
    assert stranger.delete(
        f"/api/v1/shares/{share_id}/members/not-a-user",
        headers={CSRF_HEADER: stranger_csrf},
    ).status_code == 404
    assert stranger.get("/api/v1/shares").json() == {"shares": []}

    assert owner.post(
        f"/api/v1/shares/{share_id}/invitations", json={"role": "viewer"},
    ).status_code == 403
    invite = owner.post(
        f"/api/v1/shares/{share_id}/invitations",
        headers={CSRF_HEADER: owner_csrf}, json={"role": "viewer"},
    )
    assert invite.status_code == 201
    token = invite.json()["token"]

    viewer, viewer_csrf = _login(app, "viewer")
    accepted = viewer.post(
        "/api/v1/share-invitations/accept",
        headers={CSRF_HEADER: viewer_csrf}, json={"token": token},
    )
    assert accepted.status_code == 200
    detail = viewer.get(f"/api/v1/shares/{share_id}")
    assert detail.status_code == 200
    assert detail.json()["membership"]["role"] == "viewer"
    assert viewer.post(
        f"/api/v1/shares/{share_id}/invitations",
        headers={CSRF_HEADER: viewer_csrf}, json={"role": "viewer"},
    ).status_code == 403


def test_invite_token_never_appears_in_path_and_replay_is_idempotent(tmp_path):
    app = _app(tmp_path)
    owner, csrf = _login(app, "owner")
    share = owner.post(
        "/api/v1/shares", headers={CSRF_HEADER: csrf}, json={"name": "Greek A1"},
    ).json()
    invite = owner.post(
        f"/api/v1/shares/{share['id']}/invitations",
        headers={CSRF_HEADER: csrf}, json={"role": "editor"},
    ).json()
    assert invite["token"] not in invite["accept_url"]
    assert "#" in invite["accept_url"]

    editor, editor_csrf = _login(app, "editor")
    payload = {"token": invite["token"]}
    first = editor.post(
        "/api/v1/share-invitations/accept",
        headers={CSRF_HEADER: editor_csrf}, json=payload,
    )
    replay = editor.post(
        "/api/v1/share-invitations/accept",
        headers={CSRF_HEADER: editor_csrf}, json=payload,
    )
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()

