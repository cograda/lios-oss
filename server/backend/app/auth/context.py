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
