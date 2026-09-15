"""Per-request user context for MCP tool handlers.

The legacy MCP handler signature is `(session, arguments)` — it has no slot
for the authenticated user. Rather than rewrite every handler, we stash the
current user_id in a ContextVar that handlers can read on demand.

Usage in tool handlers:

    from app.auth.context import current_user_id

    def handle_foo(session, arguments):
        uid = current_user_id()
        rows = session.query(Reminder).filter_by(user_id=uid).all()

Every entrypoint that invokes a tool handler MUST wrap the call in
`use_user(user.id)`. There is no silent default — `current_user_id()`
raises if called outside a `use_user` block. This is deliberate: a
forgotten binding is a per-user data leak, so we make it loud.
"""

from contextlib import contextmanager
from contextvars import ContextVar


# Sentinel 0 means "not set" — `current_user_id()` raises in that case.
# Every legitimate caller is wrapped in `use_user(user.id)`.
_current_user_id: ContextVar[int] = ContextVar(
    "current_user_id", default=0
)


def current_user_id() -> int:
    """Return the user_id of the bearer making this request.

    Raises RuntimeError if called outside a `use_user(...)` block. This
    prevents per-user-scoped queries from silently returning the wrong
    user's data when an entrypoint forgets to bind the ContextVar.
    """
    uid = _current_user_id.get()
    if not uid:
        raise RuntimeError(
            "current_user_id() called outside use_user() — "
            "every entrypoint must bind a user before invoking handlers"
        )
    return uid


def current_user_id_or_none() -> int | None:
    """Return the bound user_id, or None if no user is bound.

    For code paths that must work both inside and outside a request
    (e.g. EmbeddingService.search, which tools call user-bound but
    background jobs may call unbound). Unbound callers should treat
    None as "household-shared data only" — never as "all users".
    """
    return _current_user_id.get() or None


@contextmanager
def use_user(user_id: int):
    """Bind the current user_id for the duration of the with-block.

    Always uses get/reset (not set without reset) so concurrent requests
    can't bleed user_id across each other in the same event loop.
    """
    token = _current_user_id.set(user_id)
    try:
        yield
    finally:
        _current_user_id.reset(token)


# ---------------------------------------------------------------------------
# Folder scope of a cross-user vault read grant (2026-09-06)
# ---------------------------------------------------------------------------
#
# Bound by `app/plugin/dispatch.py` alongside `use_user(...)` when a read-only
# obsidian tool runs `as_user` under a grant that names folders. `None` means
# "no folder restriction in force" — which is every ordinary call AND an
# `as_user` call under a whole-vault (NULL folders) grant. The restriction
# itself is applied in exactly one place, `app.services.vault_scope.restrict`;
# this module only carries the value from the access decision to the query.
_vault_folders: ContextVar[tuple[str, ...] | None] = ContextVar(
    "vault_folders", default=None
)


def current_vault_folders() -> tuple[str, ...] | None:
    """Folder prefixes the in-flight call may read, or None for unrestricted."""
    return _vault_folders.get()


@contextmanager
def use_vault_folders(folders: tuple[str, ...] | list[str] | None):
    """Bind the folder scope for the duration of the with-block.

    Same get/reset discipline as `use_user` — a binding that outlived its call
    would restrict (or, worse, fail to restrict) the next caller on the loop.
    """
    token = _vault_folders.set(tuple(folders) if folders else None)
    try:
        yield
    finally:
        _vault_folders.reset(token)
