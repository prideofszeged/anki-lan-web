from __future__ import annotations

import time

from anki.collection import Collection
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest

from ankiweb.config import Settings
from ankiweb.identity import (
    IdentityDatabase,
    IdentityRepository,
    IdentityService,
    UserQuota,
)
from ankiweb.multiuser_app import create_multi_user_app
from ankiweb.tenancy import StorageLayout


def _settings(tmp_path) -> Settings:
    return Settings(
        collection_path=tmp_path / "legacy" / "collection.anki2",
        data_root=tmp_path / "data",
        multi_user=True,
        init_collection=False,
        secure_cookie=False,
        max_active_runtimes=2,
        runtime_wait_seconds=1,
    )


def _identity(settings: Settings) -> tuple[IdentityService, StorageLayout]:
    storage = StorageLayout(settings.effective_data_root)
    service = IdentityService(
        IdentityRepository(IdentityDatabase(storage.app_db)),
        provision=storage.provision_empty_user,
        rollback_provision=storage.discard_provisioned_user,
    )
    service.initialize()
    return service, storage


def _add_deck(path, name: str) -> None:
    collection = Collection(str(path), server=False)
    try:
        collection.decks.add_normal_deck_with_name(name)
    finally:
        collection.close()


def test_multiuser_app_authenticates_and_isolates_private_collections(tmp_path) -> None:
    settings = _settings(tmp_path)
    identity, storage = _identity(settings)
    alice = identity.bootstrap_owner(username="alice", password="safe-password")
    invite = identity.create_invite(
        actor_user_id=alice.id, intended_username="bob"
    )
    bob = identity.accept_invite(
        token=invite.token, password="another-safe-password"
    )
    _add_deck(storage.user_paths(alice.id).collection, "Alice Only")
    _add_deck(storage.user_paths(bob.id).collection, "Bob Only")

    with TestClient(create_multi_user_app(settings)) as client:
        assert client.get("/api/v1/decks").status_code == 401

        login = client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "safe-password"},
        )
        assert login.status_code == 200
        alice_names = {
            deck["name"] for deck in client.get("/api/v1/decks").json()["decks"]
        }
        assert "Alice Only" in alice_names
        assert "Bob Only" not in alice_names
        deck_page = client.get("/deckbrowser")
        assert "class='account-link'" in deck_page.text
        account_page = client.get("/account")
        assert account_page.status_code == 200
        assert "Invite an account" in account_page.text
        assert "@alice" in account_page.text

        client.cookies.clear()
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "bob", "password": "another-safe-password"},
        )
        assert login.status_code == 200
        bob_names = {
            deck["name"] for deck in client.get("/api/v1/decks").json()["decks"]
        }
        assert "Bob Only" in bob_names
        assert "Alice Only" not in bob_names


def test_multiuser_app_requires_multiuser_settings(tmp_path) -> None:
    settings = Settings(collection_path=tmp_path / "collection.anki2")
    try:
        create_multi_user_app(settings)
    except ValueError as exc:
        assert "multi-user" in str(exc)
    else:
        raise AssertionError("single-user settings were accepted")


def test_collaboration_mutation_routes_remain_fail_closed_until_runtime_exclusion(tmp_path):
    settings = _settings(tmp_path)
    identity, _ = _identity(settings)
    identity.bootstrap_owner(username="alice", password="safe-password")
    with TestClient(create_multi_user_app(settings)) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "safe-password"},
        )
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        # These handlers manipulate collection roots and intentionally stay unmounted
        # until RuntimeRegistry provides an exclusive resource-maintenance lease.
        assert client.patch(
            "/api/v1/shares/00000000-0000-0000-0000-000000000001/workspace/notes/guid",
            headers={"x-csrf-token": csrf, "origin": "http://testserver"},
            json={"expected_revision": 0, "fields": {"Front": "unsafe"}},
        ).status_code in {404, 405}
        assert client.post(
            "/api/v1/subscriptions/00000000-0000-0000-0000-000000000002/updates",
            headers={
                "x-csrf-token": csrf, "idempotency-key": "blocked",
                "origin": "http://testserver",
            },
            json={"target_version": 2},
        ).status_code in {404, 405}


def test_workspace_provision_job_http_is_async_csrf_protected_and_actor_scoped(tmp_path):
    settings = _settings(tmp_path)
    identity, storage = _identity(settings)
    owner = identity.bootstrap_owner(username="alice", password="safe-password")
    invite = identity.create_invite(
        actor_user_id=owner.id, intended_username="bob",
    )
    identity.accept_invite(token=invite.token, password="another-safe-password")
    col = Collection(str(storage.user_paths(owner.id).collection), server=False)
    try:
        deck_id = int(col.decks.id("Greek Source"))
        note = col.new_note(col.models.by_name("Basic"))
        note["Front"], note["Back"] = "γειά", "hello"
        col.add_note(note, deck_id)
    finally:
        col.close()

    with TestClient(create_multi_user_app(settings)) as client:
        login = client.post("/api/v1/auth/login", json={
            "username": "alice", "password": "safe-password",
        })
        csrf = login.json()["csrf_token"]
        share = client.post(
            "/api/v1/shares",
            headers={"x-csrf-token": csrf, "origin": "http://testserver"},
            json={"name": "Greek"},
        ).json()
        endpoint = f"/api/v1/shares/{share['id']}/workspace/provision"
        assert client.post(endpoint, json={"deck_id": deck_id}).status_code == 403
        submitted = client.post(
            endpoint,
            headers={
                "x-csrf-token": csrf, "origin": "http://testserver",
                "idempotency-key": "http-provision-1",
            },
            json={"deck_id": deck_id},
        )
        assert submitted.status_code == 202, submitted.text
        job_id = submitted.json()["id"]
        duplicate = client.post(
            endpoint,
            headers={
                "x-csrf-token": csrf, "origin": "http://testserver",
                "idempotency-key": "http-provision-1",
            },
            json={"deck_id": deck_id},
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["id"] == job_id
        mismatch = client.post(
            endpoint,
            headers={
                "x-csrf-token": csrf, "origin": "http://testserver",
                "idempotency-key": "http-provision-1",
            },
            json={"deck_id": deck_id + 1},
        )
        assert mismatch.status_code == 409

        client.cookies.clear()
        bob_login = client.post("/api/v1/auth/login", json={
            "username": "bob", "password": "another-safe-password",
        })
        assert bob_login.status_code == 200
        assert client.post(
            endpoint,
            headers={
                "x-csrf-token": bob_login.json()["csrf_token"],
                "origin": "http://testserver", "idempotency-key": "idor",
            },
            json={"deck_id": deck_id},
        ).status_code == 404
        assert client.get(f"/api/v1/jobs/{job_id}").status_code == 404
        assert client.post(
            f"/api/v1/shares/{share['id']}/releases",
            headers={
                "x-csrf-token": bob_login.json()["csrf_token"],
                "origin": "http://testserver", "idempotency-key": "publish-idor",
            },
        ).status_code == 404
        assert client.post(
            f"/api/v1/shares/{share['id']}/installs",
            headers={
                "x-csrf-token": bob_login.json()["csrf_token"],
                "origin": "http://testserver", "idempotency-key": "install-idor",
            },
            json={"version": 1, "mode": "follow"},
        ).status_code == 404

        client.cookies.clear()
        owner_login = client.post("/api/v1/auth/login", json={
            "username": "alice", "password": "safe-password",
        })
        assert owner_login.status_code == 200
        for _ in range(200):
            status = client.get(f"/api/v1/jobs/{job_id}")
            assert status.status_code == 200
            if status.json()["state"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        assert status.json()["state"] == "succeeded", status.text
        assert storage.share_paths(share["id"]).collection.exists()

        workspace_collection = Collection(
            str(storage.share_paths(share["id"]).collection), server=False,
        )
        try:
            workspace_note = workspace_collection.get_note(
                workspace_collection.find_notes("")[0]
            )
            guid = workspace_note.guid
        finally:
            workspace_collection.close()
        edit_endpoint = f"/api/v1/shares/{share['id']}/workspace/notes/{guid}"
        edited = client.patch(
            edit_endpoint,
            headers={
                "x-csrf-token": owner_login.json()["csrf_token"],
                "origin": "http://testserver",
            },
            json={"expected_revision": 0, "fields": {"Front": "χαίρετε"}},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["revision"] == 1
        stale = client.patch(
            edit_endpoint,
            headers={
                "x-csrf-token": owner_login.json()["csrf_token"],
                "origin": "http://testserver",
            },
            json={"expected_revision": 0, "fields": {"Front": "stale"}},
        )
        assert stale.status_code == 409

        release_endpoint = f"/api/v1/shares/{share['id']}/releases"
        assert client.post(release_endpoint).status_code == 403
        assert client.post(
            release_endpoint,
            headers={
                "x-csrf-token": owner_login.json()["csrf_token"],
                "origin": "http://testserver",
            },
        ).status_code == 422
        published = client.post(
            release_endpoint,
            headers={
                "x-csrf-token": owner_login.json()["csrf_token"],
                "origin": "http://testserver", "idempotency-key": "publish-http-1",
            },
        )
        assert published.status_code == 202, published.text
        release_job = published.json()["id"]
        for _ in range(300):
            status = client.get(f"/api/v1/jobs/{release_job}")
            if status.json()["state"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        assert status.json()["state"] == "succeeded", status.text
        assert status.json()["progress"]["version"] == 1
        replay = client.post(
            release_endpoint,
            headers={
                "x-csrf-token": owner_login.json()["csrf_token"],
                "origin": "http://testserver", "idempotency-key": "publish-http-1",
            },
        )
        assert replay.status_code == 202
        assert replay.json()["id"] == release_job


def test_multiuser_app_refuses_to_start_without_an_owner(tmp_path) -> None:
    settings = _settings(tmp_path)
    app = create_multi_user_app(settings)
    try:
        with TestClient(app):
            pass
    except RuntimeError as exc:
        assert "bootstrap" in str(exc)
    else:
        raise AssertionError("unprovisioned multi-user server started")


def test_multiuser_upload_quota_streams_to_cleanup_without_residue(tmp_path) -> None:
    settings = _settings(tmp_path)
    identity, storage = _identity(settings)
    owner = identity.bootstrap_owner(username="alice", password="safe-password")
    identity.repository.set_quota(UserQuota(
        user_id=owner.id,
        import_bytes=4,
    ))

    with TestClient(create_multi_user_app(settings)) as client:
        assert client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "safe-password"},
        ).status_code == 200
        response = client.post(
            "/import/upload",
            files={"file": ("large.csv", b"front,back\nhello,world\n", "text/csv")},
            headers={"Origin": "http://testserver"},
        )
        assert response.status_code == 507
        assert response.json()["detail"]["code"] == "quota_exceeded"

    temporary = storage.user_paths(owner.id).temporary
    assert not [path for path in temporary.rglob("*") if path.is_file()]


def test_multiuser_review_socket_quota_is_enforced_per_user(tmp_path) -> None:
    settings = _settings(tmp_path)
    identity, _storage = _identity(settings)
    owner = identity.bootstrap_owner(username="alice", password="safe-password")
    identity.repository.set_quota(UserQuota(user_id=owner.id, review_sockets=1))

    with TestClient(create_multi_user_app(settings)) as client:
        assert client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "safe-password"},
        ).status_code == 200
        headers = {"Origin": "http://testserver"}
        with client.websocket_connect(
            "/ws?context=deckbrowser", headers=headers,
        ):
            with pytest.raises(WebSocketDisconnect) as rejected:
                with client.websocket_connect(
                    "/ws?context=reviewer", headers=headers,
                ):
                    pass
            assert rejected.value.code == 1008
