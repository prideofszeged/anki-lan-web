from __future__ import annotations

from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher

from ankiweb.identity import IdentityDatabase, IdentityRepository, IdentityService, UserQuota


class Clock:
    def __init__(self, value):
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


def test_session_is_durable_hashed_and_revocable_by_auth_epoch(tmp_path):
    clock = Clock(datetime(2026, 10, 3, tzinfo=timezone.utc))
    path = tmp_path / "app.db"
    service = _service(path, clock)
    owner = service.bootstrap_owner(password="safe-password")
    grant = service.login(username="local", password="safe-password")
    assert grant is not None
    assert grant.token.encode() not in _all_database_bytes(path)
    assert grant.csrf_token.encode() not in _all_database_bytes(path)

    restarted = _service(path, clock)
    assert restarted.authenticate(grant.token).user_id == owner.id
    assert restarted.repository.revoke_all_sessions(owner.id, now=clock.value) == 1
    assert restarted.authenticate(grant.token) is None
    assert restarted.repository.get_user(owner.id).auth_epoch == 1


def test_idle_and_absolute_session_expiry(tmp_path):
    clock = Clock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    service = _service(tmp_path / "app.db", clock)
    service.bootstrap_owner(password="safe-password")
    idle = service.login(username="local", password="safe-password")
    clock.value += timedelta(days=30)
    assert service.authenticate(idle.token) is None

    # Repeated idle refreshes may not carry a session past its 90-day absolute lifetime.
    clock.value = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fresh = service.login(username="local", password="safe-password")
    for day in (20, 40, 60, 80):
        clock.value = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day)
        assert service.authenticate(fresh.token) is not None
    clock.value = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=90)
    assert service.authenticate(fresh.token) is None


def test_session_quota_evicts_oldest_idle_session(tmp_path):
    clock = Clock(datetime(2026, 10, 3, tzinfo=timezone.utc))
    service = _service(tmp_path / "app.db", clock)
    owner = service.bootstrap_owner(password="safe-password")
    current = service.repository.get_quota(owner.id)
    service.repository.set_quota(UserQuota(
        user_id=owner.id, storage_bytes=current.storage_bytes,
        import_bytes=current.import_bytes, active_jobs=current.active_jobs,
        active_sessions=2, review_sockets=current.review_sockets,
    ))
    first = service.login(username="local", password="safe-password")
    clock.value += timedelta(seconds=1)
    second = service.login(username="local", password="safe-password")
    clock.value += timedelta(seconds=1)
    third = service.login(username="local", password="safe-password")
    assert service.authenticate(first.token, refresh=False) is None
    assert service.authenticate(second.token, refresh=False) is not None
    assert service.authenticate(third.token, refresh=False) is not None
