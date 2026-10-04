from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from argon2 import PasswordHasher

from ankiweb.identity import (
    AuthorizationError, ExpiredTokenError, GlobalRole, IdentityDatabase,
    IdentityRepository, IdentityService, InvalidTokenError,
)
from ankiweb.tenancy import StorageLayout


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self):
        return self.value


def _service(path, clock):
    repo = IdentityRepository(IdentityDatabase(path))
    service = IdentityService(
        repo,
        password_hasher=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1),
        clock=clock,
    )
    service.initialize()
    return service


def _all_database_bytes(path):
    return b"".join(candidate.read_bytes() for candidate in path.parent.glob(path.name + "*"))


def test_invite_token_is_hashed_and_consumed_exactly_once(tmp_path):
    clock = Clock(datetime(2026, 10, 3, tzinfo=timezone.utc))
    path = tmp_path / "app.db"
    service = _service(path, clock)
    owner = service.bootstrap_owner(password="safe-password")
    grant = service.create_invite(
        actor_user_id=owner.id, intended_username="NewUser", role=GlobalRole.USER,
    )
    assert grant.token.encode() not in _all_database_bytes(path)

    user = service.accept_invite(
        token=grant.token, password="another-safe-password", display_name="New User",
    )
    assert user.username_norm == "newuser"
    assert service.verify_password("newuser", "another-safe-password") == user
    with pytest.raises(InvalidTokenError, match="already used"):
        service.accept_invite(token=grant.token, password="third-safe-password")
    assert grant.token.encode() not in _all_database_bytes(path)


def test_invite_expiry_and_role_authorization(tmp_path):
    clock = Clock(datetime(2026, 10, 3, tzinfo=timezone.utc))
    service = _service(tmp_path / "app.db", clock)
    owner = service.bootstrap_owner(password="safe-password")
    grant = service.create_invite(
        actor_user_id=owner.id, intended_username="late",
        lifetime=timedelta(seconds=1),
    )
    clock.value += timedelta(seconds=1)
    with pytest.raises(ExpiredTokenError):
        service.accept_invite(token=grant.token, password="another-safe-password")

    user = service.repository.create_user(username="ordinary", now=clock.value)
    with pytest.raises(AuthorizationError):
        service.create_invite(actor_user_id=user.id, intended_username="nope")
    admin = service.repository.create_user(
        username="admin", role=GlobalRole.ADMIN, now=clock.value,
    )
    with pytest.raises(AuthorizationError, match="owners"):
        service.create_invite(
            actor_user_id=admin.id, intended_username="admin2", role=GlobalRole.ADMIN,
        )


def test_concurrent_invite_acceptance_has_one_winner(tmp_path):
    clock = Clock(datetime(2026, 10, 3, tzinfo=timezone.utc))
    path = tmp_path / "app.db"
    service = _service(path, clock)
    owner = service.bootstrap_owner(password="safe-password")
    grant = service.create_invite(actor_user_id=owner.id, intended_username="only-once")

    def accept():
        worker = _service(path, clock)
        try:
            return worker.accept_invite(token=grant.token, password="another-safe-password")
        except InvalidTokenError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: accept(), range(2)))
    assert sum(item is not None for item in results) == 1


def test_invite_activation_provisions_a_private_collection(tmp_path):
    clock = Clock(datetime(2026, 10, 3, tzinfo=timezone.utc))
    storage = StorageLayout(tmp_path / "data")
    repo = IdentityRepository(IdentityDatabase(storage.app_db))
    service = IdentityService(
        repo,
        password_hasher=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1),
        clock=clock,
        provision=storage.provision_empty_user,
        rollback_provision=storage.discard_provisioned_user,
    )
    service.initialize()
    owner = service.bootstrap_owner(password="safe-password")
    grant = service.create_invite(
        actor_user_id=owner.id, intended_username="provisioned"
    )

    user = service.accept_invite(
        token=grant.token, password="another-safe-password"
    )

    paths = storage.user_paths(user.id)
    assert user.state.value == "active"
    assert paths.collection.is_file()
    assert paths.media.is_dir()


def test_invite_provision_failure_rolls_back_identity_and_token(tmp_path):
    clock = Clock(datetime(2026, 10, 3, tzinfo=timezone.utc))
    repo = IdentityRepository(IdentityDatabase(tmp_path / "app.db"))
    service = IdentityService(
        repo,
        password_hasher=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1),
        clock=clock,
        provision=lambda _user: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    service.initialize()
    owner = service.bootstrap_owner(password="safe-password", provision=lambda _user: None)
    grant = service.create_invite(actor_user_id=owner.id, intended_username="retryable")

    with pytest.raises(OSError, match="disk unavailable"):
        service.accept_invite(token=grant.token, password="another-safe-password")
    assert repo.get_user_by_username("retryable") is None

    retry_service = IdentityService(
        repo,
        password_hasher=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1),
        clock=clock,
        provision=lambda _user: None,
    )
    user = retry_service.accept_invite(
        token=grant.token, password="another-safe-password"
    )
    assert user.state.value == "active"
