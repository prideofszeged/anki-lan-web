from __future__ import annotations

from fastapi.testclient import TestClient

from ankiweb.config import Settings
from ankiweb.identity import IdentityDatabase, IdentityRepository, IdentityService
from ankiweb.identity.http import CSRF_HEADER
from ankiweb.multiuser_app import create_multi_user_app
from ankiweb.tenancy import StorageLayout


def test_multiuser_share_membership_page_is_mobile_and_private(tmp_path):
    settings = Settings(
        collection_path=tmp_path / "legacy/collection.anki2",
        data_root=tmp_path / "data", multi_user=True, init_collection=False,
        secure_cookie=False, max_active_runtimes=2, runtime_wait_seconds=1,
    )
    storage = StorageLayout(settings.effective_data_root)
    identity = IdentityService(
        IdentityRepository(IdentityDatabase(storage.app_db)),
        provision=storage.provision_empty_user,
        rollback_provision=storage.discard_provisioned_user,
    )
    identity.initialize()
    identity.bootstrap_owner(username="owner", password="owner-password")

    with TestClient(create_multi_user_app(settings)) as client:
        login = client.post("/api/v1/auth/login", json={
            "username": "owner", "password": "owner-password",
        })
        csrf = login.json()["csrf_token"]
        created = client.post(
            "/api/v1/shares", headers={CSRF_HEADER: csrf, "Origin": "http://testserver"},
            json={"name": "Greek A1"},
        )
        assert created.status_code == 201
        page = client.get("/shares")
        assert page.status_code == 200
        assert "Greek A1" in page.text
        assert "viewport-fit=cover" in page.text
        assert "Create a shared deck" in page.text
        assert "Create workspace" in page.text
        assert "Publish next release" in page.text
        assert "Follow" in page.text and "Copy" in page.text
        assert "Installed subscription" in page.text
        assert "Preview mirror changes" in page.text
        assert "Apply all resolutions" in page.text
        assert "Back up workspace and releases" in page.text
        assert "Restore backup" in page.text
        assert "'/backups'" in page.text and "'/restores'" in page.text
        assert "/api/v1/subscriptions" in page.text
        assert "role='status' aria-live='polite'" in page.text
        assert "job.error" not in page.text
        account = client.get("/account")
        assert "href='/shares'" in account.text
