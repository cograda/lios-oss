"""Small, shared text-handling helpers with no integration home of their own.

First (and so far only) occupant: escaping user input for SQLAlchemy `ilike`.
"""

from __future__ import annotations

# The escape character SQLAlchemy's `ilike(..., escape=...)` is told to
# respect. Must itself be escaped first, or a literal backslash in the input
# would be reinterpreted as starting an escape sequence.
ILIKE_ESCAPE_CHAR = "\\"


def escape_ilike(value: str) -> str:
    """Escape `%`, `_`, and `\\` in `value` for use inside an `ilike` pattern.

    ~12 call sites across the integrations built patterns like
    `col.ilike(f"%{q}%")` with no escaping — a caller search term containing
    `%` or `_` silently turned into a wildcard instead of a literal character
    (e.g. searching for a WhatsApp caption containing a literal `%`, or a
    filename with an underscore, would match far more broadly than intended).
    Nothing here is a security hole (no injection — SQLAlchemy already
    parameterizes the value), just a correctness bug in what the wildcard
    means.

    Usage — wrap the interpolated value, then pass `escape=` matching this
    module's escape char:

        col.ilike(f"%{escape_ilike(q)}%", escape=ILIKE_ESCAPE_CHAR)

    Order matters: backslash must be escaped first, or escaping `%`/`_`
    afterward would double-escape the backslashes just inserted.
    """
    return (
        value.replace(ILIKE_ESCAPE_CHAR, ILIKE_ESCAPE_CHAR * 2)
        .replace("%", ILIKE_ESCAPE_CHAR + "%")
        .replace("_", ILIKE_ESCAPE_CHAR + "_")
    )
