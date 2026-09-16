"""A small, boring error-code enum for the REST projection envelope.

Used by `POST /api/v1/batch` (always) and `POST /api/v1/tools/{name}` (only
when the caller opts into the envelope — see `v1.py`'s `_envelope_requested`).
Deliberately closed and small: `dispatch_tool()` collapses every handler
failure into a plain string today (see `app/plugin/dispatch.py::ToolResult`),
so this module's job is turning what little structure survives that (an
unknown-tool 404, a timeout, the read-only-scope refusal) into one of a
handful of codes a client can branch on, not inventing false precision.
"""

from __future__ import annotations

# code -> default retryable. A caller may see a code here that was inferred
# from message text (see `classify_dispatch_error` in v1.py) rather than
# raised as this code directly — the mapping still holds either way.
_RETRYABLE: dict[str, bool] = {
    "not_found": False,
    "invalid_args": False,
    "forbidden": False,
    "unknown_tool": False,
    "timeout": True,
    "upstream": True,
    "internal": False,
}

VALID_CODES = frozenset(_RETRYABLE)


def retryable_for(code: str) -> bool:
    """Default retryable-ness for `code`. Unknown codes are not retryable —
    the safe default when we can't say for sure."""
    return _RETRYABLE.get(code, False)


def error_obj(code: str, message: str, retryable: bool | None = None) -> dict:
    """Build the envelope's `error` object: `{"code", "message", "retryable"}`.

    An unrecognised `code` is coerced to `"internal"` rather than left as
    free text — the whole point of a closed enum is that a client's switch
    statement can't silently fall through on a typo'd code.
    """
    if code not in VALID_CODES:
        code = "internal"
    return {
        "code": code,
        "message": message,
        "retryable": retryable if retryable is not None else retryable_for(code),
    }
