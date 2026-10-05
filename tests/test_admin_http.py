from __future__ import annotations

from datetime import datetime, timezone

from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ankiweb.identity import (
    GlobalRole, IdentityDatabase, IdentityRepository, IdentityService,
)
from ankiweb.identity.http import CSRF_HEADER, build_identity_http


NOW = datetime(2026, 10, 5, 18, 0, tzinfo=timezone.utc)


def _app(tmp_path):
    repository = IdentityRepository(IdentityDatabase(tmp_path / "app.db"))
    service = IdentityService(
        repository,
        password_hasher=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1),
        clock=lambda: NOW,
    )
    service.initialize()
    owner = service.bootstrap_owner(username="owner", password="owner-password")
    grant = service.create_invite(
        actor_user_id=owner.id, intended_username="member", role=GlobalRole.USER,
    )
    member = service.accept_invite(token=grant.token, password="member-password")
    identity_http = build_identity_http(
        service, secure_cookie=False,
        usage_provider=lambda user_id: 123 if user_id == member.id else 456,
        backup_age_provider=lambda user_id: 3600 if user_id == member.id else None,
    )
    app = FastAPI()
    app.include_router(identity_http.router)
    return app, repository, owner, member


def _login(app, username, password):
    client = TestClient(app)
    response = client.post("/api/v1/auth/login", json={
        "username": username, "password": password,
    })
    assert response.status_code == 200
    return client, response.json()["csrf_token"]


def test_admin_user_metadata_is_rbac_scoped_and_content_free(tmp_path):
    app, _, owner, member = _app(tmp_path)
    owner_client, _ = _login(app, "owner", "owner-password")
    response = owner_client.get("/api/v1/admin/users")
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["users"]}
    assert set(rows) == {owner.id, member.id}
    assert rows[member.id]["usage_bytes"] == 123
    assert rows[member.id]["backup_age_seconds"] == 3600
    assert rows[member.id]["quota"]["storage_bytes"] > 0
    assert "PRIVATE-SENTINEL" not in response.text
    assert not ({"note", "card", "media", "password_hash"} & rows[member.id].keys())

    member_client, _ = _login(app, "member", "member-password")
    assert member_client.get("/api/v1/admin/users").status_code == 403
    assert member_client.get("/api/v1/admin/audit").status_code == 403


def test_admin_suspend_reactivates_and_quota_changes_require_csrf(tmp_path):
    app, repository, _, member = _app(tmp_path)
    owner_client, csrf = _login(app, "owner", "owner-password")
    member_client, _ = _login(app, "member", "member-password")
    endpoint = f"/api/v1/admin/users/{member.id}"
    assert owner_client.patch(endpoint, json={"state": "suspended"}).status_code == 403
    suspended = owner_client.patch(endpoint, headers={CSRF_HEADER: csrf}, json={
        "state": "suspended", "storage_bytes": 1024 * 1024,
        "import_bytes": 512 * 1024,
    })
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["state"] == "suspended"
    assert suspended.json()["quota"]["storage_bytes"] == 1024 * 1024
    assert repository.list_sessions(member.id, now=NOW) == []

    assert member_client.get("/api/v1/auth/me").status_code == 401
    active = owner_client.patch(endpoint, headers={CSRF_HEADER: csrf}, json={
        "state": "active",
    })
    assert active.status_code == 200
    assert active.json()["state"] == "active"
    audit = owner_client.get("/api/v1/admin/audit")
    assert audit.status_code == 200
    assert any(row["action"] == "admin.user.updated" for row in audit.json()["events"])
    assert "PRIVATE-SENTINEL" not in audit.text
