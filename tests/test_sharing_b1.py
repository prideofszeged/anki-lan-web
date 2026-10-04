from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ankiweb.identity import (
    GlobalRole, IdentityDatabase, IdentityRepository,
)
from ankiweb.identity.repository import AuthorizationError, ExpiredTokenError, InvalidTokenError
from ankiweb.sharing import (
    ShareRole, SharingRepository, SharingService,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _stack(tmp_path, clock=None):
    database = IdentityDatabase(tmp_path / "app.db")
    identities = IdentityRepository(database)
    identities.initialize()
    users = {
        name: identities.create_user(
            username=name, display_name=name.title(), role=role, now=NOW,
        )
        for name, role in (
            ("owner", GlobalRole.OWNER),
            ("editor", GlobalRole.USER),
            ("viewer", GlobalRole.USER),
            ("stranger", GlobalRole.USER),
            ("admin", GlobalRole.ADMIN),
        )
    }
    repository = SharingRepository(database)
    service = SharingService(repository, clock=clock or (lambda: NOW))
    return database, service, users


def test_sh1_nonmember_and_global_admin_cannot_discover_private_share(tmp_path):
    _, service, users = _stack(tmp_path)
    share = service.create_share(actor_user_id=users["owner"].id, name="Greek A1")

    assert [item.id for item in service.list_shares(users["owner"].id)] == [share.id]
    for outsider in (users["stranger"], users["admin"]):
        assert service.list_shares(outsider.id) == []
        with pytest.raises(AuthorizationError, match="not found"):
            service.get_share(actor_user_id=outsider.id, share_id=share.id)


def test_sh2_invite_is_hashed_bound_expiring_revocable_and_one_time(tmp_path):
    database, service, users = _stack(tmp_path)
    share = service.create_share(actor_user_id=users["owner"].id, name="Greek A1")
    grant = service.create_invite(
        actor_user_id=users["owner"].id,
        share_id=share.id,
        role=ShareRole.EDITOR,
        intended_user_id=users["editor"].id,
    )
    raw_database = b"".join(
        item.read_bytes() for item in database.path.parent.glob(database.path.name + "*")
    )
    assert grant.token.encode() not in raw_database

    with pytest.raises(AuthorizationError):
        service.accept_invite(actor_user_id=users["viewer"].id, token=grant.token)
    membership = service.accept_invite(
        actor_user_id=users["editor"].id, token=grant.token,
    )
    assert membership.role is ShareRole.EDITOR
    # Exact replay by the intended user is idempotent, not a second membership.
    assert service.accept_invite(
        actor_user_id=users["editor"].id, token=grant.token,
    ) == membership

    revoked = service.create_invite(
        actor_user_id=users["owner"].id, share_id=share.id,
        role=ShareRole.VIEWER,
    )
    service.revoke_invite(
        actor_user_id=users["owner"].id, share_id=share.id,
        invite_id=revoked.invite.id,
    )
    with pytest.raises(InvalidTokenError):
        service.accept_invite(actor_user_id=users["viewer"].id, token=revoked.token)

    expired = service.create_invite(
        actor_user_id=users["owner"].id, share_id=share.id,
        role=ShareRole.VIEWER, lifetime=timedelta(seconds=1),
    )
    service._clock = lambda: NOW + timedelta(seconds=1)
    with pytest.raises(ExpiredTokenError):
        service.accept_invite(actor_user_id=users["viewer"].id, token=expired.token)


def test_sh3_role_matrix_is_enforced_server_side(tmp_path):
    _, service, users = _stack(tmp_path)
    share = service.create_share(actor_user_id=users["owner"].id, name="Greek A1")
    editor_invite = service.create_invite(
        actor_user_id=users["owner"].id, share_id=share.id, role=ShareRole.EDITOR,
    )
    viewer_invite = service.create_invite(
        actor_user_id=users["owner"].id, share_id=share.id, role=ShareRole.VIEWER,
    )
    service.accept_invite(actor_user_id=users["editor"].id, token=editor_invite.token)
    service.accept_invite(actor_user_id=users["viewer"].id, token=viewer_invite.token)

    for member in (users["editor"], users["viewer"]):
        assert service.get_share(actor_user_id=member.id, share_id=share.id).share.id == share.id
        with pytest.raises(AuthorizationError):
            service.create_invite(
                actor_user_id=member.id, share_id=share.id, role=ShareRole.VIEWER,
            )
        with pytest.raises(AuthorizationError):
            service.remove_member(
                actor_user_id=member.id, share_id=share.id,
                target_user_id=users["viewer"].id,
            )

    service.remove_member(
        actor_user_id=users["owner"].id, share_id=share.id,
        target_user_id=users["viewer"].id,
    )
    with pytest.raises(AuthorizationError, match="not found"):
        service.get_share(actor_user_id=users["viewer"].id, share_id=share.id)
    with pytest.raises(AuthorizationError):
        service.remove_member(
            actor_user_id=users["owner"].id, share_id=share.id,
            target_user_id=users["owner"].id,
        )


def test_share_mutation_audit_has_metadata_not_tokens_or_content(tmp_path):
    database, service, users = _stack(tmp_path)
    share = service.create_share(actor_user_id=users["owner"].id, name="Private deck name")
    invite = service.create_invite(
        actor_user_id=users["owner"].id, share_id=share.id, role=ShareRole.VIEWER,
    )
    with database.read() as conn:
        rows = conn.execute(
            "SELECT action, metadata_json FROM audit_events ORDER BY id"
        ).fetchall()
    assert [row["action"] for row in rows] == ["share.created", "share.invite.created"]
    serialized = "".join(row["metadata_json"] for row in rows)
    assert invite.token not in serialized
    assert "Private deck name" not in serialized
