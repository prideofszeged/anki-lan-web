from __future__ import annotations

from fastapi.testclient import TestClient

from ankiweb.config import Settings
from ankiweb.identity import IdentityDatabase, IdentityRepository, IdentityService
from ankiweb.tenancy import StorageLayout


def test_admin_page_is_mobile_metadata_only_and_role_protected(tmp_path):
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
    owner = identity.bootstrap_owner(username="owner", password="owner-password")
    invite = identity.create_invite(actor_user_id=owner.id, intended_username="member")
    identity.accept_invite(token=invite.token, password="member-password")

    from ankiweb.multiuser_app import create_multi_user_app

    with TestClient(create_multi_user_app(settings)) as client:
        login = client.post("/api/v1/auth/login", json={
            "username": "owner", "password": "owner-password",
        })
        assert login.status_code == 200
        page = client.get("/admin")
        assert page.status_code == 200
        assert "Account administration" in page.text
        assert "@member" in page.text
        assert "Storage quota" in page.text
        assert "Backup age" in page.text
        assert "viewport-fit=cover" in page.text
        assert "aria-live='polite'" in page.text
        assert "note text" not in page.text.lower()
        account = client.get("/account")
        assert "href='/admin'" in account.text

    with TestClient(create_multi_user_app(settings)) as client:
        login = client.post("/api/v1/auth/login", json={
            "username": "member", "password": "member-password",
        })
        assert login.status_code == 200
        assert client.get("/admin").status_code == 403
        assert "href='/admin'" not in client.get("/account").text
