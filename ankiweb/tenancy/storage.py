from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .context import canonical_uuid


@dataclass(frozen=True, slots=True)
class UserPaths:
    root: Path
    collection: Path
    media: Path
    temporary: Path
    app: Path
    backups: Path


@dataclass(frozen=True, slots=True)
class SharePaths:
    root: Path
    collection: Path
    media: Path
    releases: Path
    backups: Path


class StorageLayout:
    """Resolve opaque UUIDs into fixed storage roots.

    Callers can provide identifiers, never path fragments. Existing symlinks in a
    managed path are rejected before directories are created.
    """

    def __init__(self, data_root: Path | str) -> None:
        self.root = Path(data_root).expanduser().resolve()
        self.users_root = self.root / "users"
        self.shares_root = self.root / "shares"
        self.backups_root = self.root / "backups"

    @property
    def app_db(self) -> Path:
        return self.root / "app" / "app.db"

    def user_paths(self, user_id: UUID | str) -> UserPaths:
        ident = str(canonical_uuid(user_id))
        root = self.users_root / ident
        return UserPaths(
            root=root,
            collection=root / "anki" / "collection.anki2",
            media=root / "anki" / "collection.media",
            temporary=root / "tmp",
            app=root / "app",
            backups=self.backups_root / "users" / ident,
        )

    def share_paths(self, share_id: UUID | str) -> SharePaths:
        ident = str(canonical_uuid(share_id))
        root = self.shares_root / ident
        return SharePaths(
            root=root,
            collection=root / "anki" / "collection.anki2",
            media=root / "anki" / "collection.media",
            releases=root / "releases",
            backups=self.backups_root / "shares" / ident,
        )

    def prepare(self) -> None:
        self._prepare_directories(
            self.root,
            self.root / "app",
            self.users_root,
            self.shares_root,
            self.backups_root,
            self.backups_root / "system",
            self.backups_root / "users",
            self.backups_root / "shares",
        )

    def prepare_user(self, user_id: UUID | str) -> UserPaths:
        paths = self.user_paths(user_id)
        self.prepare()
        self._prepare_directories(
            paths.root,
            paths.collection.parent,
            paths.media,
            paths.temporary,
            paths.app,
            paths.backups,
        )
        return paths

    def prepare_share(self, share_id: UUID | str) -> SharePaths:
        paths = self.share_paths(share_id)
        self.prepare()
        self._prepare_directories(
            paths.root,
            paths.collection.parent,
            paths.media,
            paths.releases,
            paths.backups,
        )
        return paths

    def provision_empty_user(self, user) -> UserPaths:
        """Create a valid empty Anki collection for a freshly-created identity."""
        paths = self.user_paths(user.id)
        root_existed = paths.root.exists()
        backup_existed = paths.backups.exists()
        if paths.collection.exists():
            raise FileExistsError(f"user collection already exists: {paths.collection}")
        try:
            self.prepare_user(user.id)
            from ankiweb.adapters.anki.provision import create_empty_collection

            create_empty_collection(paths.collection)
            os.chmod(paths.collection, 0o600)
            return paths
        except BaseException:
            if not root_existed:
                self._remove_managed_tree(paths.root)
            if not backup_existed:
                self._remove_managed_tree(paths.backups)
            raise

    def discard_provisioned_user(self, user) -> None:
        """Rollback a just-provisioned UUID root after the identity transaction fails."""
        paths = self.user_paths(user.id)
        self._remove_managed_tree(paths.root)
        self._remove_managed_tree(paths.backups)

    def user_usage_bytes(self, user_id: UUID | str) -> int:
        """Count private collection, media, temp, app state, and retained backups."""
        paths = self.user_paths(user_id)
        total = 0
        for root in (paths.root, paths.backups):
            if not root.exists():
                continue
            self._assert_no_symlink(root)
            for current, directories, names in os.walk(root, followlinks=False):
                current_path = Path(current)
                for name in directories:
                    if (current_path / name).is_symlink():
                        raise ValueError("symlink not allowed in managed storage")
                for name in names:
                    file_path = current_path / name
                    if file_path.is_symlink() or not file_path.is_file():
                        raise ValueError("non-regular file in managed storage")
                    total += file_path.stat().st_size
        return total

    def _remove_managed_tree(self, path: Path) -> None:
        self._assert_under_root(path)
        if path.is_symlink():
            raise ValueError(f"symlink not allowed in managed path: {path}")
        if path.exists():
            shutil.rmtree(path)

    def _prepare_directories(self, *paths: Path) -> None:
        for path in paths:
            self._assert_under_root(path)
            self._assert_no_symlink(path)
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(path, 0o700)

    def _assert_under_root(self, path: Path) -> None:
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"managed path escapes data root: {path}") from exc

    def _assert_no_symlink(self, path: Path) -> None:
        current = path
        while current != self.root.parent:
            if current.is_symlink():
                raise ValueError(f"symlink not allowed in managed path: {current}")
            if current == self.root:
                return
            current = current.parent
        raise ValueError(f"managed path escapes data root: {path}")
