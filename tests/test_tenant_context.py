from uuid import uuid4

import pytest

from ankiweb.tenancy import ResourceKey, ResourceKind, TenantContext, TenantRole


def test_resource_keys_canonicalize_uuid_strings() -> None:
    ident = uuid4()
    key = ResourceKey.user(str(ident).upper())
    assert key.resource_id == ident
    assert key.kind is ResourceKind.USER
    assert key.stable_name == f"user:{ident}"


def test_private_context_is_owner_and_writable() -> None:
    ident = uuid4()
    context = TenantContext.private_collection(ident)
    assert context.actor_user_id == ident
    assert context.resource_owner_id == ident
    assert context.resource_key == ResourceKey.user(ident)
    assert context.role is TenantRole.OWNER
    assert context.can_write


def test_path_fragment_is_not_a_resource_id() -> None:
    with pytest.raises(ValueError):
        ResourceKey.user("../../another-user")
