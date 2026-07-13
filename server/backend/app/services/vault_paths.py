"""User-aware vault path resolution.

Logical paths the MCP layer accepts:
    "Daily Notes/Alex/2026-05-16.md"     → /vaults/<user>/Daily Notes/Alex/2026-05-16.md
    "Inbox/handoff.md"                   → /vaults/<user>/Inbox/handoff.md

Every logical path is scoped to the calling user's personal vault. This is
enforced regardless of how `..` shenanigans the caller attempts — `resolve()`
collapses them and we reject any path that escapes the user's vault root.

The vault is single-user as of 2026-06-05; the old `Shared/` cross-user
namespace was removed when Sam moved to her own separate vault.
"""

from __future__ import annotations

from pathlib import Path

from app.auth.context import current_user_id
from app.config import settings
from app.db import get_db
from app.models.users import User


def _vaults_root() -> Path:
    return Path(settings.vaults_root_path)


def user_vault_path(user_name: str) -> Path:
    """Filesystem path to a named user's personal vault."""
    return _vaults_root() / user_name


def _resolve_user_name(user_id_override: int | None = None) -> str:
    """Look up the calling user's `name` (used as a directory name)."""
    uid = user_id_override if user_id_override is not None else current_user_id()
    if uid is None:
        raise RuntimeError(
            "vault_paths.resolve called outside a user-scoped request "
            "(current_user_id is unbound). Wrap the call in `use_user(...)`."
        )
    db = get_db()
    with db.session() as session:
        row = session.get(User, uid)
        if row is None:
            raise RuntimeError(f"vault_paths: user_id={uid} not found in users table")
        return row.name


def resolve(logical_path: str, user_id_override: int | None = None) -> Path:
    """Map a logical (caller-facing) path to an absolute filesystem path.

    Rejects:
      - empty paths
      - absolute paths (must be relative to the vault root)
      - paths that escape the user's vault root after `.resolve()`

    Returns the absolute Path object. Does not check existence — callers do that.
    """
    if not logical_path or logical_path.startswith("/"):
        raise ValueError(f"vault path must be a non-empty relative path: {logical_path!r}")

    user_name = _resolve_user_name(user_id_override)
    root = user_vault_path(user_name)

    candidate = (root / Path(logical_path)).resolve()
    root_resolved = root.resolve()
    # Guardrail: candidate must be inside the user's vault after symlink/.. resolution.
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        raise ValueError(f"vault path escapes its allowed root: {logical_path!r}")

    return candidate
