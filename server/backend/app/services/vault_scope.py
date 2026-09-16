"""Folder scoping for cross-user vault read grants — the ONE chokepoint.

A `vault_read_grants` row may carry `folders`, a list of vault-relative
prefixes (`["Household/", "People/"]`). When dispatch resolves an `as_user`
call under such a grant it binds them on `use_vault_folders(...)` right
beside `use_user(...)`, and from then on **every** query that reads the
vault index must pass through `restrict()` here. The three tables that hold
vault paths — `vault_chunks` (path), `embeddings` and `embedding_queue`
(source_id, `{path}` or `{path}#{n}`) — are the whole surface.

Why one module rather than a filter in each tool: the 2026-09-06 decision was
that Sam reads the renovation and household material in Alex's vault as
shared context rather than holding a copy, and that his `Health/`, `Notes/`,
`Blog/` and `Daily Notes/` are **never** in her scope. A restriction applied
per tool is applied per tool *forever* — the next read tool added to
`obsidian/tools.py` would ship unscoped, and its author would have no reason
to know. Concentrating it here gives that new tool nothing to forget, and
gives the guard below one thing to check.

🔑 **Fail closed, and loudly.** `_refuse_unscoped_vault_reads` is a
`do_orm_execute` listener on every Session: while folders are bound, a
statement that touches one of the three tables without having come through
`restrict()` is refused before it runs. This is the rule from
`app/services/vault_grants.py` — never fall back silently — applied at the
SQL boundary, where a silent fallback would return the *owner's whole vault*
and look exactly like a correct answer. The listener is inert for every
ordinary call: `current_vault_folders()` is None unless dispatch bound it.

The caller-supplied `folder` argument **narrows within** the grant and can
never widen it. A folder outside every granted prefix is refused rather than
returning an empty list — an empty list reads as "there is nothing about
that", which is the wrong conclusion and an unrecoverable one for the
caller. A folder that is *broader* than the grant (`folder="House"` against
`Household/`) is allowed and simply ANDed, so the result is still inside the
grant.

Prefix matching for the grant is case-sensitive `LIKE`, unlike the caller's
`folder` filter which keeps its historical case-insensitive `ILIKE`: vault
paths are case-sensitive on the server's filesystem, and a grant on
`Household/` must not be satisfied by `household/` — that is the widening
direction.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from sqlalchemy import event, or_
from sqlalchemy.orm import Session
from sqlalchemy.sql.util import find_tables

from app.auth.context import current_vault_folders
from app.errors import PermanentError
from app.services.text import ILIKE_ESCAPE_CHAR, escape_ilike

logger = logging.getLogger(__name__)

# The tables whose rows carry a vault path. Names, not model imports: this is
# kernel code (`app/services`) and may not import an integration's models;
# and a name is what the executed statement exposes anyway.
SCOPED_TABLES: frozenset[str] = frozenset({"vault_chunks", "embeddings", "embedding_queue"})

# Execution option stamped on a query by `restrict()`. The listener treats a
# statement over a scoped table WITHOUT this stamp as an unscoped read.
_APPLIED_OPTION = "vault_scope_applied"


# ---------------------------------------------------------------------------
# Folder values — shared by the admin script and the service layer
# ---------------------------------------------------------------------------

def normalise_folder(value: Any) -> str:
    """Canonical form of one granted folder: `Household/Renovation/`.

    Rejects anything that is not a plain relative folder path: empty strings,
    absolute paths, backslashes, and any `.` or `..` component. `..` is the
    one that matters — a prefix filter is a string comparison, and
    `Household/../Health/` would be a *string* under `Household/`.
    """
    if not isinstance(value, str):
        raise ValueError(f"folder must be a string, got {type(value).__name__}")
    text = value.strip()
    if not text:
        raise ValueError("folder must not be empty")
    if text.startswith("/") or "\\" in text:
        raise ValueError(f"folder must be vault-relative with forward slashes: {value!r}")
    parts = text.rstrip("/").split("/")
    if any(p.strip() in ("", ".", "..") for p in parts):
        raise ValueError(f"folder contains an empty, '.' or '..' component: {value!r}")
    return "/".join(p.strip() for p in parts) + "/"


def normalise_folders(values: Iterable[Any]) -> list[str]:
    """Normalise a collection of folders, de-duplicated, order preserved.

    An empty collection is an error rather than "whole vault": the caller
    who meant whole-vault passes `None`, and a grant that silently widened
    because someone passed `[]` is the failure this module exists to prevent.
    """
    out: list[str] = []
    for v in values:
        f = normalise_folder(v)
        if f not in out:
            out.append(f)
    if not out:
        raise ValueError("folders must name at least one folder (use None for the whole vault)")
    return out


# ---------------------------------------------------------------------------
# The chokepoint
# ---------------------------------------------------------------------------

def _requested_within(requested: str, folders: tuple[str, ...]) -> bool:
    """True if `requested` can be satisfied inside the grant.

    Either the request sits under a granted prefix (narrower), or a granted
    prefix sits under the request (broader — ANDing then yields the grant).
    Compared case-insensitively because the caller's filter is ILIKE; the
    grant clause itself stays case-sensitive so this check can only ever
    *admit* a request that the AND then bounds, never widen it.
    """
    # No trailing-slash requirement: "Household" has always matched
    # "Household/..." in the caller's prefix filter, and still does here.
    req = requested.strip().lstrip("/").lower()
    for g in folders:
        gl = g.lower()
        if req.startswith(gl) or gl.startswith(req):
            return True
    return False


def _folder_clause(column: Any, folders: tuple[str, ...]):
    return or_(*(
        column.like(f"{escape_ilike(f)}%", escape=ILIKE_ESCAPE_CHAR) for f in folders
    ))


def _requested_clause(column: Any, requested: str):
    return column.ilike(f"{escape_ilike(requested)}%", escape=ILIKE_ESCAPE_CHAR)


def restrict(query: Any, column: Any, requested: str | None = None) -> Any:
    """Apply the caller's optional folder filter AND the grant's folder scope.

    `query` is a legacy `Session.query(...)` or a `select()`; `column` is the
    path-bearing column on it (`VaultChunk.path`, `Embedding.source_id`, …);
    `requested` is the caller's `folder` argument, if any.

    With no folders bound this is exactly the historical behaviour — the
    caller's prefix filter or nothing. With folders bound it adds the grant
    clause, refuses a `requested` that falls outside the grant, and stamps
    the query so the listener below knows it has been here.
    """
    folders = current_vault_folders()
    wanted = requested.strip() if isinstance(requested, str) else None

    if folders is None:
        return query.filter(_requested_clause(column, wanted)) if wanted else query

    if wanted:
        if not _requested_within(wanted, folders):
            raise PermanentError(
                f"folder {wanted!r} is outside this grant; readable folders: "
                f"{', '.join(folders)}"
            )
        query = query.filter(_requested_clause(column, wanted))

    return query.filter(_folder_clause(column, folders)).execution_options(
        **{_APPLIED_OPTION: True}
    )


def require_vault_only(sources: Iterable[str] | None, *, what: str) -> None:
    """Refuse a cross-source query while a folder scope is bound.

    The folder restriction is a predicate on *vault paths*; an email or
    WhatsApp `source_id` is not a path, so a search that mixes sources cannot
    be restricted and must not run. Under a whole-vault grant (folders None)
    this is a no-op — the owner's other sources are already excluded by the
    scope check in `vault_grants`, because only `obsidian` is grantable.
    """
    if current_vault_folders() is None:
        return
    srcs = list(sources) if sources is not None else None
    if srcs is None or any(s != "vault" for s in srcs):
        raise PermanentError(
            f"{what} refused: this call runs under a folder-scoped vault grant, "
            f"which can only restrict vault paths, but the query covers "
            f"{'all sources' if srcs is None else sorted(srcs)}."
        )


# ---------------------------------------------------------------------------
# The guard — an unscoped read of a scoped table is refused before it runs
# ---------------------------------------------------------------------------

def _table_names(statement: Any) -> set[str]:
    names: set[str] = set()
    try:
        tables = find_tables(
            statement, check_columns=True, include_joins=True, include_aliases=True,
        )
    except Exception:  # noqa: BLE001 — a shape find_tables can't walk is not a read of ours
        return names
    for t in tables:
        # An Alias exposes the alias name; the real table is `.element`.
        inner = getattr(t, "element", None)
        name = getattr(inner, "name", None) or getattr(t, "name", None)
        if isinstance(name, str):
            names.add(name)
    return names


@event.listens_for(Session, "do_orm_execute")
def _refuse_unscoped_vault_reads(state) -> None:
    """Refuse any statement over a vault-path table that skipped `restrict()`.

    Only active while `use_vault_folders` is bound, i.e. inside an `as_user`
    call under a folder-scoped grant. Raises `PermanentError` so dispatch
    reports it as a tool error rather than a 500, and so the caller learns
    that the query was refused instead of receiving an empty (or worse, an
    unscoped) result.
    """
    if current_vault_folders() is None:
        return
    if state.execution_options.get(_APPLIED_OPTION):
        return
    hit = _table_names(state.statement) & SCOPED_TABLES
    if hit:
        logger.warning(
            "folder-scoped vault grant: refusing unscoped statement over %s", sorted(hit),
        )
        raise PermanentError(
            "this call runs under a folder-scoped vault grant, but a query over "
            f"{', '.join(sorted(hit))} did not apply the folder restriction — "
            "refusing rather than returning unscoped data."
        )
