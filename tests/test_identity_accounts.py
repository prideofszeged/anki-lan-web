from __future__ import annotations

from datetime import datetime, timezone

import pytest
from argon2 import PasswordHasher

from ankiweb.identity import (
    AccountState, ConflictError, GlobalRole, IdentityDatabase, IdentityRepository,
    IdentityService, LastOwnerError, UserQuota,
)

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def _service(path):
    repo = IdentityRepository(IdentityDatabase(path))
    service = IdentityService(
        repo,
        password_hasher=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1),
        clock=lambda: NOW,
    )
    service.initialize()
    return service


def test_owner_credentials_and_quota_survive_restart(tmp_path):
    path = tmp_path / "app.db"
    service = _service(path)
    owner = service.bootstrap_owner(username="Local", password="safe-password")
    assert owner.username_norm == "local"
    assert service.verify_password("LOCAL", "safe-password") == owner
    quota = service.repository.get_quota(owner.id)
    assert quota.storage_bytes == 5 * 1024**3
    assert quota.active_sessions == 10

    restarted = _service(path)
    assert restarted.verify_password("local", "safe-password").id == owner.id
    assert restarted.repository.get_quota(owner.id) == quota


def test_bootstrap_is_allowed_only_on_an_empty_database(tmp_path):
    service = _service(tmp_path / "app.db")
    service.repository.create_user(username="preexisting", now=NOW)
    with pytest.raises(ConflictError, match="already provisioned"):
        service.bootstrap_owner(username="another-name", password="safe-password")


def test_normalized_username_is_unique(tmp_path):
    service = _service(tmp_path / "app.db")
    service.repository.create_user(
        username="ΣΤΕΛΙΟΣ", now=NOW, display_name="First",
    )
    with pytest.raises(ConflictError, match="already"):
        # Greek final sigma and ordinary sigma normalize to the same casefolded name.
        service.repository.create_user(
            username="στελιοσ", now=NOW, display_name="Second",
        )


def test_last_active_owner_cannot_be_demoted_or_suspended(tmp_path):
    service = _service(tmp_path / "app.db")
    first = service.bootstrap_owner(password="safe-password")
    with pytest.raises(LastOwnerError):
        service.repository.set_role(first.id, GlobalRole.ADMIN)
    with pytest.raises(LastOwnerError):
        service.repository.set_state(first.id, AccountState.SUSPENDED, now=NOW)

    second = service.repository.create_user(
        username="second", role=GlobalRole.OWNER, now=NOW,
    )
    changed = service.repository.set_state(first.id, AccountState.SUSPENDED, now=NOW)
    assert changed.state is AccountState.SUSPENDED
    # Protection moved to the sole remaining active owner.
    with pytest.raises(LastOwnerError):
        service.repository.set_role(second.id, GlobalRole.USER)


def test_quota_round_trip_and_metadata_only_audit(tmp_path):
    service = _service(tmp_path / "app.db")
    owner = service.bootstrap_owner(password="safe-password")
    quota = UserQuota(
        user_id=owner.id, storage_bytes=99, import_bytes=50, active_jobs=1,
        active_sessions=3, review_sockets=2,
    )
    service.repository.set_quota(quota)
    assert service.repository.get_quota(owner.id) == quota

    event_id = service.repository.append_audit(
        now=NOW, actor_user_id=owner.id, target_user_id=owner.id,
        action="quota.update", resource_type="user", resource_id=owner.id,
        request_id="request-1", outcome="success", metadata={"storage_bytes": 99},
    )
    event = service.repository.list_audit()[0]
    assert event.id == event_id
    assert event.actor_user_id == owner.id
    assert event.metadata == {"storage_bytes": 99}
    with pytest.raises(ValueError, match="binary"):
        service.repository.append_audit(
            now=NOW, action="bad", resource_type="note", outcome="success",
            metadata={"card_html": b"secret content"},
        )
