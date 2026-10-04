"""Tenant isolation primitives for multi-user collection runtimes."""

from .context import ResourceKey, ResourceKind, TenantContext, TenantRole
from .collection import CollectionProcessLock, ResourceLockedError, TenantCollectionRuntime
from .runtime import RuntimeCapacityError, RuntimeLease, RuntimeRegistry
from .storage import SharePaths, StorageLayout, UserPaths
from .migration import (
    CollectionIntegrityError,
    LegacyCollectionMigration,
    MigrationPhase,
    MigrationResult,
    TenantMigrationError,
    UnsafeMigrationPath,
)

__all__ = [
    "ResourceKey",
    "ResourceKind",
    "ResourceLockedError",
    "RuntimeCapacityError",
    "RuntimeLease",
    "RuntimeRegistry",
    "SharePaths",
    "StorageLayout",
    "TenantCollectionRuntime",
    "TenantContext",
    "TenantRole",
    "UserPaths",
    "CollectionProcessLock",
    "CollectionIntegrityError",
    "LegacyCollectionMigration",
    "MigrationPhase",
    "MigrationResult",
    "TenantMigrationError",
    "UnsafeMigrationPath",
]
