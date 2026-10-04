from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

_BASE_HOST_PREFIXES = ("127.0.0.1:", "localhost:", "[::1]:")
_BASE_HOSTS = ("127.0.0.1", "localhost", "testserver", "[::1]")


def host_allowed(host: str, extra=()) -> bool:
    """DNS-rebinding guard. Always allows localhost; allows any host explicitly listed in
    `extra` (matched with OR without a :port); `'*'` in `extra` disables the check entirely
    (open to any Host header — only do this on a trusted network)."""
    if "*" in extra:
        return True
    if host.startswith(_BASE_HOST_PREFIXES) or host in _BASE_HOSTS:
        return True
    if host in extra:
        return True
    bare = host.rsplit(":", 1)[0] if host.count(":") == 1 else host  # strip :port (not IPv6)
    return bare in extra


@dataclass(frozen=True)
class Settings:
    collection_path: Path
    host: str = "127.0.0.1"
    port: int = 8000
    assets_dir: Path = Path(__file__).parent / "web_assets"
    shell_dir: Path = Path(__file__).parent / "shell"
    import_tmp_dir: Path = Path(__file__).parent / "_import_tmp"
    # Extra Host-header values accepted by the DNS-rebinding guard (beyond localhost),
    # e.g. ("192.168.1.50:8000",) or ("myhost.local",). "*" disables the check.
    allowed_hosts: tuple = ()
    # AGPL §13 source offer: where this deployment's Corresponding Source lives. Shown on
    # the /about page + the toolbar "Source" link. Set ANKIWEB_SOURCE_URL when deploying.
    source_url: str = ""
    # UI language chosen at startup (Anki locale code, e.g. "zh-CN", "ja"). Empty = English.
    # Applied by CollectionService.open() via anki.lang.set_lang(); there is no in-UI switcher.
    lang: str = ""
    # Web-UI password. When set, the web app requires a /login session cookie; the
    # AnkiConnect server keeps its own apiKey. `python -m ankiweb` refuses to start with neither
    # this nor password_hash unless auth_disabled (see auth_error); create_app() stays permissive.
    password: str = ""
    password_hash: str = ""
    secure_cookie: bool = False
    # Explicit opt-out of the fail-closed startup check (see auth_error). Only for a server
    # that is already isolated (loopback/VPN); never the default.
    auth_disabled: bool = False
    # Tests and embedded callers may create a collection intentionally. Environment-driven
    # server startup defaults to fail-closed unless ANKIWEB_INIT_COLLECTION is explicit.
    init_collection: bool = True
    # Multi-user stays opt-in until migration verifies the legacy collection copy.
    multi_user: bool = False
    data_root: Path | None = None
    max_active_runtimes: int = 4
    runtime_idle_seconds: int = 900
    runtime_wait_seconds: int = 30
    # Only direct peers in these CIDRs may supply X-Forwarded-For for rate limiting.
    trusted_proxy_cidrs: tuple[str, ...] = ()
    # Explicit exception for containers bound internally to 0.0.0.0 whose published
    # host port is loopback-only. Never enable when the app port is LAN-reachable.
    insecure_cookie_ok: bool = False

    @property
    def effective_data_root(self) -> Path:
        return self.data_root or self.collection_path.parent

    def auth_error(self) -> str | None:
        """Why the server must refuse to start, or None. Fail closed: a LAN-reachable
        Anki collection without a password is never the implicit default (SPEC V8)."""
        if self.multi_user:
            if self.auth_disabled:
                return "multi-user mode cannot disable authentication"
            if self.password or self.password_hash:
                return (
                    "multi-user mode uses app.db credentials; remove ANKIWEB_PASSWORD and "
                    "ANKIWEB_PASSWORD_HASH after migration"
                )
            if (
                not self.secure_cookie
                and self.host not in {"127.0.0.1", "localhost", "::1"}
                and not self.insecure_cookie_ok
            ):
                return (
                    "multi-user mode requires Secure cookies when bound beyond loopback; "
                    "enable ANKIWEB_SECURE_COOKIE behind HTTPS"
                )
            return None
        if self.password_hash:
            try:
                from argon2 import extract_parameters
                params = extract_parameters(self.password_hash)
                if not self.password_hash.startswith("$argon2id$") or params.time_cost < 1:
                    raise ValueError("not Argon2id")
            except Exception:
                return "ANKIWEB_PASSWORD_HASH is not a valid Argon2id encoded hash"
            return None
        if self.auth_disabled:
            return None
        if not self.password:
            return ("no web password configured: set ANKIWEB_PASSWORD (or "
                    "ANKIWEB_PASSWORD_HASH), or ANKIWEB_AUTH_DISABLED=1 to run unauthenticated")
        if self.password == "change-me":
            return "ANKIWEB_PASSWORD is still the .env.example placeholder; set a real password"
        return None

    @classmethod
    def from_env(cls) -> "Settings":
        default = Path.home() / ".local/share/ankiweb/collection.anki2"
        collection = Path(os.environ.get("ANKIWEB_COLLECTION", str(default)))
        return cls(
            collection_path=collection,
            host=os.environ.get("ANKIWEB_HOST", "127.0.0.1"),
            port=int(os.environ.get("ANKIWEB_PORT", "8000")),
            import_tmp_dir=Path(os.environ["ANKIWEB_IMPORT_TMP_DIR"]) if os.environ.get("ANKIWEB_IMPORT_TMP_DIR") else (collection.parent / "import-tmp"),
            allowed_hosts=tuple(
                h.strip() for h in os.environ.get("ANKIWEB_ALLOWED_HOSTS", "").split(",") if h.strip()),
            source_url=os.environ.get("ANKIWEB_SOURCE_URL", ""),
            lang=os.environ.get("ANKIWEB_LANG", ""),
            password=os.environ.get("ANKIWEB_PASSWORD", ""),
            password_hash=os.environ.get("ANKIWEB_PASSWORD_HASH", ""),
            secure_cookie=os.environ.get("ANKIWEB_SECURE_COOKIE", "").lower()
            in ("1", "true", "yes", "on"),
            auth_disabled=os.environ.get("ANKIWEB_AUTH_DISABLED", "").lower()
            in ("1", "true", "yes", "on"),
            init_collection=os.environ.get("ANKIWEB_INIT_COLLECTION", "").lower()
            in ("1", "true", "yes", "on"),
            multi_user=os.environ.get("ANKIWEB_MULTI_USER", "").lower()
            in ("1", "true", "yes", "on"),
            data_root=Path(os.environ["ANKIWEB_DATA_ROOT"])
            if os.environ.get("ANKIWEB_DATA_ROOT") else None,
            max_active_runtimes=int(os.environ.get("ANKIWEB_MAX_ACTIVE_RUNTIMES", "4")),
            runtime_idle_seconds=int(os.environ.get("ANKIWEB_RUNTIME_IDLE_SECONDS", "900")),
            runtime_wait_seconds=int(os.environ.get("ANKIWEB_RUNTIME_WAIT_SECONDS", "30")),
            trusted_proxy_cidrs=tuple(
                item.strip()
                for item in os.environ.get("ANKIWEB_TRUSTED_PROXY_CIDRS", "").split(",")
                if item.strip()
            ),
            insecure_cookie_ok=os.environ.get("ANKIWEB_INSECURE_COOKIE_OK", "").lower()
            in ("1", "true", "yes", "on"),
        )
