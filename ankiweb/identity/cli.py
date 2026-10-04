from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from ankiweb.config import Settings
from ankiweb.tenancy import LegacyCollectionMigration, StorageLayout

from .database import IdentityDatabase
from .repository import IdentityRepository
from .service import IdentityService


def bootstrap_owner(
    settings: Settings,
    *,
    username: str,
    display_name: str,
    password: str,
):
    storage = StorageLayout(settings.effective_data_root)
    storage.prepare()
    identity = IdentityService(
        IdentityRepository(IdentityDatabase(storage.app_db)),
        provision=storage.provision_empty_user,
        rollback_provision=storage.discard_provisioned_user,
    )
    identity.initialize()
    user = identity.bootstrap_owner(
        username=username,
        display_name=display_name,
        password=password,
    )
    return user


def migrate_legacy_collection(
    settings: Settings,
    *,
    user_id: str,
    source_collection: Path | None = None,
    source_media: Path | None = None,
):
    storage = StorageLayout(settings.effective_data_root)
    identity = IdentityService(IdentityRepository(IdentityDatabase(storage.app_db)))
    identity.initialize()
    user = identity.repository.get_user(user_id)
    if user is None:
        raise ValueError("migration target user does not exist")
    collection = source_collection or settings.collection_path
    media = source_media or collection.with_name("collection.media")
    return LegacyCollectionMigration(
        storage=storage,
        user_id=user.id,
        source_collection=collection,
        source_media=media,
    ).migrate()


def prepare_legacy_migration(
    settings: Settings,
    *,
    user_id: str,
    backup_archive: Path,
    source_collection: Path | None = None,
    source_media: Path | None = None,
):
    storage = StorageLayout(settings.effective_data_root)
    identity = IdentityService(IdentityRepository(IdentityDatabase(storage.app_db)))
    identity.initialize()
    user = identity.repository.get_user(user_id)
    if user is None:
        raise ValueError("migration target user does not exist")
    collection = source_collection or settings.collection_path
    media = source_media or collection.with_name("collection.media")
    migration = LegacyCollectionMigration(
        storage=storage,
        user_id=user.id,
        source_collection=collection,
        source_media=media,
    )
    return migration.prepare_evidence(backup_archive)


def run_identity_cli(
    argv: Sequence[str],
    *,
    settings: Settings | None = None,
    password_reader: Callable[[str], str] = getpass.getpass,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m ankiweb user")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap", help="create the first local owner")
    bootstrap.add_argument("--username", default="local")
    bootstrap.add_argument("--display-name", default="Local owner")
    bootstrap.add_argument(
        "--password-stdin",
        action="store_true",
        help="read one password line from stdin (for container secret plumbing)",
    )
    migrate = commands.add_parser(
        "migrate-legacy", help="copy and verify the legacy collection for one owner"
    )
    migrate.add_argument("--user-id", required=True)
    migrate.add_argument("--collection", type=Path)
    migrate.add_argument("--media", type=Path)
    prepare = commands.add_parser(
        "prepare-migration",
        help="verify an exact backup and attest that the stopped legacy source is ready",
    )
    prepare.add_argument("--user-id", required=True)
    prepare.add_argument("--backup", type=Path, required=True)
    prepare.add_argument("--collection", type=Path)
    prepare.add_argument("--media", type=Path)
    prepare.add_argument(
        "--confirm-stopped", action="store_true",
        help="confirm the legacy app is stopped (also checked with an advisory lock)",
    )
    args = parser.parse_args(list(argv))

    resolved_settings = settings or Settings.from_env()

    if args.command == "prepare-migration":
        if not args.confirm_stopped:
            parser.error("prepare-migration requires --confirm-stopped")
        offline, backup = prepare_legacy_migration(
            resolved_settings,
            user_id=args.user_id,
            backup_archive=args.backup,
            source_collection=args.collection,
            source_media=args.media,
        )
        print(f"migration evidence written: {offline} and {backup}")
        return 0

    if args.command == "migrate-legacy":
        result = migrate_legacy_collection(
            resolved_settings,
            user_id=args.user_id,
            source_collection=args.collection,
            source_media=args.media,
        )
        print(f"migration {result.phase.name.lower()} for {result.user_id}")
        return 0

    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = password_reader("New owner password: ")
        if password != password_reader("Repeat password: "):
            parser.error("passwords do not match")

    user = bootstrap_owner(
        resolved_settings,
        username=args.username,
        display_name=args.display_name,
        password=password,
    )
    print(f"created owner {user.username} ({user.id})")
    return 0
