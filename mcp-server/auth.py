"""Profile-scoped opaque MCP and personal-context credential storage."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re

CONFIG_DIR = os.path.expanduser("~/.praxys")
LEGACY_CONFIG_DIR = os.path.expanduser("~/.trainsight")
TOKEN_PATH = os.path.join(CONFIG_DIR, "token")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
_LEGACY_TOKEN_PATH = os.path.join(LEGACY_CONFIG_DIR, "token")
_LEGACY_CONFIG_PATH = os.path.join(LEGACY_CONFIG_DIR, "config.json")
_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class AuthScope:
    """Resolved storage paths for one MCP authentication profile."""

    profile: str
    token_path: Path
    config_path: Path
    context_token_path: Path
    pending_context_path: Path
    fallback_token_paths: tuple[Path, ...] = ()
    fallback_config_paths: tuple[Path, ...] = ()
    legacy_suppression_path: Path | None = None


@dataclass(frozen=True)
class LogoutResult:
    """Filesystem changes made while logging out one auth scope."""

    removed_paths: tuple[Path, ...]
    legacy_fallback_suppressed: bool = False


def _profile_name() -> str:
    """Return and validate the selected authentication profile."""
    profile = os.environ.get("PRAXYS_PROFILE", "").strip()
    if not profile:
        return "default"
    if not _PROFILE_PATTERN.fullmatch(profile):
        raise ValueError(
            "PRAXYS_PROFILE must start with a letter or number and contain "
            "only letters, numbers, underscores, or hyphens"
        )
    return profile


def get_auth_scope() -> AuthScope:
    """Resolve token and config paths for the active authentication profile."""
    profile = _profile_name()
    explicit_token_path = os.environ.get("PRAXYS_TOKEN_PATH", "").strip()
    if explicit_token_path:
        token_path = Path(
            os.path.expandvars(explicit_token_path)
        ).expanduser().resolve()
        return AuthScope(
            profile=profile,
            token_path=token_path,
            config_path=token_path.with_name(
                f"{token_path.name}.config.json"
            ),
            context_token_path=token_path.with_name(
                f"{token_path.name}.context"
            ),
            pending_context_path=token_path.with_name(
                f"{token_path.name}.context-pending.json"
            ),
        )

    if profile == "default":
        return AuthScope(
            profile=profile,
            token_path=Path(TOKEN_PATH),
            config_path=Path(CONFIG_PATH),
            context_token_path=Path(CONFIG_DIR) / "context-token",
            pending_context_path=Path(CONFIG_DIR)
            / "context-pending.json",
            fallback_token_paths=(Path(_LEGACY_TOKEN_PATH),),
            fallback_config_paths=(Path(_LEGACY_CONFIG_PATH),),
            legacy_suppression_path=Path(CONFIG_DIR)
            / ".legacy-auth-disabled",
        )

    profile_dir = Path(CONFIG_DIR) / "profiles" / profile
    return AuthScope(
        profile=profile,
        token_path=profile_dir / "token",
        config_path=profile_dir / "config.json",
        context_token_path=profile_dir / "context-token",
        pending_context_path=profile_dir / "context-pending.json",
    )


def _first_existing_path(
    primary: Path,
    fallbacks: tuple[Path, ...],
) -> Path | None:
    """Return the first existing path in priority order."""
    for path in (primary, *fallbacks):
        if path.is_file():
            return path
    return None


def _active_fallbacks(
    scope: AuthScope,
    paths: tuple[Path, ...],
) -> tuple[Path, ...]:
    """Return legacy fallbacks unless this profile explicitly logged out."""
    suppression_path = scope.legacy_suppression_path
    if suppression_path is not None and suppression_path.is_file():
        return ()
    return paths


def get_config() -> dict:
    """Load stored remote API config for the active profile."""
    scope = get_auth_scope()
    path = _first_existing_path(
        scope.config_path,
        _active_fallbacks(scope, scope.fallback_config_paths),
    )
    if path is None:
        return {}
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def save_config(config: dict) -> None:
    """Persist remote API config for the active profile."""
    path = get_auth_scope().config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)


def get_token() -> str | None:
    """Read the cached MCP session for the active profile."""
    scope = get_auth_scope()
    path = _first_existing_path(
        scope.token_path,
        _active_fallbacks(scope, scope.fallback_token_paths),
    )
    if path is None:
        return None
    token = path.read_text(encoding="utf-8").strip()
    return token or None


def save_token(token: str) -> Path:
    """Cache an opaque MCP session for the active profile."""
    scope = get_auth_scope()
    path = scope.token_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    suppression_path = scope.legacy_suppression_path
    if suppression_path is not None:
        try:
            suppression_path.unlink()
        except FileNotFoundError:
            pass
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def get_context_token() -> str | None:
    """Read the short-lived structured-context token for this profile."""
    path = get_auth_scope().context_token_path
    if not path.is_file():
        return None
    token = path.read_text(encoding="utf-8").strip()
    return token or None


def save_context_token(token: str) -> Path:
    """Cache a bounded context token separately from the MCP session."""
    path = get_auth_scope().context_token_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def clear_context_token() -> bool:
    """Remove only the active profile's structured-context token."""
    path = get_auth_scope().context_token_path
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def get_pending_context_access() -> dict | None:
    """Load client-held one-time handoff proof without exposing it to tools."""
    path = get_auth_scope().pending_context_path
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def save_pending_context_access(value: dict) -> Path:
    """Persist one pending context exchange for the active profile."""
    path = get_auth_scope().pending_context_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def clear_pending_context_access() -> bool:
    """Remove the active profile's pending exchange proof."""
    path = get_auth_scope().pending_context_path
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def logout() -> LogoutResult:
    """Log out the active profile without deleting legacy-client state."""
    scope = get_auth_scope()
    removed: list[Path] = []
    for path in (
        scope.token_path,
        scope.config_path,
        scope.context_token_path,
        scope.pending_context_path,
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed.append(path)

    suppressed = False
    suppression_path = scope.legacy_suppression_path
    if suppression_path is not None:
        suppression_path.parent.mkdir(parents=True, exist_ok=True)
        suppression_path.write_text(
            "Legacy auth fallback disabled by the Praxys MCP logout tool.\n",
            encoding="utf-8",
        )
        suppressed = True
    return LogoutResult(
        removed_paths=tuple(removed),
        legacy_fallback_suppressed=suppressed,
    )
