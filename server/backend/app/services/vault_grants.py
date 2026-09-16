"""Resolving a cross-user read grant into an effective user_id.

One public function, called from exactly one place: the `# capability check
lands here` hook in `app/plugin/dispatch.py`. That module's docstring named
itself the intended chokepoint for capability enforcement; this is the first
thing to use it, and nothing else should re-implement the check elsewhere.

Shape of the feature, from the caller's side: a read-only tool in a grantable
scope accepts an `as_user` argument naming another user. Dispatch swaps the
`use_user(...)` binding for that call, so the handler — which already scopes
everything by `current_user_id()` — needs no change at all. `vault_search`,
`vault_stats` and friends were not edited to support this.

🔑 **The read-only half is enforced against each tool's own `readOnlyHint`
annotation, never a list of tool names.** A hand-maintained list of "safe"
tools is a list that goes stale the first time someone adds a tool and
forgets — and it goes stale *silently*, in the permissive direction.
Registration already refuses to start the server for a tool with no
annotations (`MissingAnnotationsError`), so keying off the annotation means a
new write tool is excluded by construction rather than by anyone remembering.

⚠️ **Every failure path here raises, and none of them fall back to the
caller's own scope.** A grant check that quietly degraded to "just search
your own vault" would return an empty result set that looks exactly like a
correct answer — the caller would conclude the vault holds nothing on the
subject rather than that it was refused. Refusals must be loud.

**Folder scope (2026-09-06).** A grant may name `folders`; `resolve_grant`
returns them alongside the effective user id and dispatch binds both
(`use_user` + `use_vault_folders`) for the call. The restriction is then
applied by `app.services.vault_scope.restrict` — one chokepoint in the
query layer, with a Session-level guard that refuses any vault-table read
that skipped it. Why: Sam is to read the renovation and household
material in Alex's vault as shared context rather than a copy, and his
`Health/`, `Notes/`, `Blog/` and `Daily Notes/` are never in that scope. A
whole-vault grant (NULL folders) is unchanged and is what the `agent`
grantee holds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import PermanentError
from app.models.users import User
from app.models.vault_grants import GRANTABLE_SCOPES, VaultReadGrant
from app.services.vault_scope import normalise_folders

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GrantResolution:
    """What dispatch binds for one tool call.

    `folders` is None for the caller's own scope and for a whole-vault grant;
    a tuple of normalised prefixes for a folder-scoped one. Dispatch must bind
    both fields together — a user swap without its folders is exactly the
    widening this exists to prevent, so they travel as one value.
    """

    user_id: int
    folders: tuple[str, ...] | None = None

# The argument a caller uses to request another user's scope. Named for what
# it does — this call runs *as* that user — rather than `user`, which reads
# like a filter and would invite "user=alex" to mean "notes mentioning Alex".
AS_USER_ARG = "as_user"


def is_read_only_tool(annotations: Any) -> bool:
    """True only if the tool positively declares itself read-only.

    Shared by the cross-user grant below and the `readonly` token scope in
    `app/plugin/dispatch.py` — one definition of "read-only", so the two
    cannot disagree about a tool.

    Fails closed: a missing annotation, a missing hint, or a hint of None all
    return False. The absence of a claim is not the claim.
    """
    if annotations is None:
        return False
    if isinstance(annotations, dict):
        # Built tools carry the MCP wire form (camelCase); accept the
        # dataclass field name too so a caller passing ToolAnnotations
        # .__dict__ doesn't silently fail closed for the wrong reason.
        value = annotations.get("readOnlyHint", annotations.get("read_only_hint"))
    else:
        value = getattr(annotations, "read_only_hint", None)
    return value is True


_is_read_only = is_read_only_tool


def resolve_effective_user_id(
    session: Session,
    *,
    caller_id: int,
    caller_name: str,
    arguments: dict[str, Any],
    tool_name: str,
    integration_name: str,
    annotations: Any,
) -> int:
    """Return the user_id this tool call should run as.

    Thin wrapper over `resolve_grant` for callers that only need the id.
    Dispatch itself uses `resolve_grant`, because it must also bind the
    grant's folder scope; anything that swaps users off this function alone
    is running a folder-scoped grant unscoped.
    """
    return resolve_grant(
        session,
        caller_id=caller_id, caller_name=caller_name, arguments=arguments,
        tool_name=tool_name, integration_name=integration_name,
        annotations=annotations,
    ).user_id


def resolve_grant(
    session: Session,
    *,
    caller_id: int,
    caller_name: str,
    arguments: dict[str, Any],
    tool_name: str,
    integration_name: str,
    annotations: Any,
) -> GrantResolution:
    """Return the user_id — and folder scope, if any — this call should run as.

    Returns the caller unchanged when no `as_user` argument is present — the
    overwhelmingly common path, and deliberately the one that touches no
    database rows.

    Takes `caller_id`/`caller_name` as plain values rather than a `User`
    instance on purpose: dispatch holds a `User` loaded by a *different*
    session, and touching an expired attribute on a detached instance raises
    at exactly the moment an access check must not.

    Raises `PermanentError` (surfaced to the caller as a tool error, not a
    500) when an `as_user` is present but not permitted.
    """
    requested = arguments.get(AS_USER_ARG)
    if requested is None:
        return GrantResolution(caller_id)

    requested_name = str(requested).strip().lower()
    if not requested_name:
        raise PermanentError(f"{AS_USER_ARG} must be a non-empty username")

    # Asking to run as yourself is allowed and is a no-op — it keeps a caller
    # that always passes `as_user` from breaking when it names itself.
    if requested_name == caller_name.lower():
        return GrantResolution(caller_id)

    if integration_name not in GRANTABLE_SCOPES:
        raise PermanentError(
            f"{AS_USER_ARG} is not available for {tool_name!r}: the "
            f"{integration_name!r} integration is not a grantable scope. "
            f"Grantable scopes: {', '.join(sorted(GRANTABLE_SCOPES))}."
        )

    if not _is_read_only(annotations):
        raise PermanentError(
            f"{AS_USER_ARG} is refused for {tool_name!r}: cross-user grants "
            f"are read-only and this tool does not declare readOnlyHint. "
            f"Acting on another user's data is never delegated."
        )

    owner = session.execute(
        select(User).where(User.name == requested_name)
    ).scalar_one_or_none()
    if owner is None or not owner.is_active:
        # Same message either way: whether a username exists is not something
        # an unprivileged caller should be able to probe for.
        raise PermanentError(f"no grant to read {requested_name!r}")

    grant = session.execute(
        select(VaultReadGrant).where(
            VaultReadGrant.grantee_user_id == caller_id,
            VaultReadGrant.owner_user_id == owner.id,
            VaultReadGrant.scope == integration_name,
        )
    ).scalar_one_or_none()
    if grant is None:
        raise PermanentError(f"no grant to read {requested_name!r}")

    # Re-normalise on read rather than trusting the stored list: a row edited
    # by hand to `[]` or `["Household"]` must still resolve to a real
    # restriction, and a malformed row must refuse rather than open the vault.
    folders: tuple[str, ...] | None = None
    if grant.folders is not None:
        try:
            folders = tuple(normalise_folders(grant.folders))
        except ValueError as e:
            raise PermanentError(
                f"grant to read {requested_name!r} has an invalid folder list "
                f"({e}); refusing rather than treating it as the whole vault"
            ) from e

    logger.info(
        "grant used: tool=%s grantee=%s owner=%s scope=%s folders=%s",
        tool_name, caller_name, owner.name, integration_name,
        ",".join(folders) if folders else "*",
    )
    return GrantResolution(owner.id, folders)


def grant_read(
    session: Session, *, grantee: str, owner: str, scope: str, reason: str,
    folders: Iterable[str] | None = None,
) -> VaultReadGrant:
    """Create (or return the existing) grant. Used by the admin script.

    `reason` is required here rather than at the column, because the column
    cannot tell the difference between "not supplied" and "genuinely blank",
    and a grant with no recorded reason is one nobody will feel safe revoking.

    `folders` narrows the grant to those vault-relative prefixes; None is the
    whole vault. Re-granting with a *different* folder list is refused rather
    than silently kept or silently rewritten — either would leave the
    operator believing the scope is what they just typed. Revoke, then grant.
    """
    if scope not in GRANTABLE_SCOPES:
        raise ValueError(
            f"scope {scope!r} is not grantable; allowed: {sorted(GRANTABLE_SCOPES)}"
        )
    if not reason.strip():
        raise ValueError("reason is required — a grant with no reason is unrevocable in practice")
    wanted = normalise_folders(folders) if folders is not None else None

    g_user = session.execute(select(User).where(User.name == grantee)).scalar_one_or_none()
    o_user = session.execute(select(User).where(User.name == owner)).scalar_one_or_none()
    if g_user is None:
        raise ValueError(f"no such user: {grantee!r}")
    if o_user is None:
        raise ValueError(f"no such user: {owner!r}")
    if g_user.id == o_user.id:
        raise ValueError("a user already reads their own vault; no grant needed")

    existing = session.execute(
        select(VaultReadGrant).where(
            VaultReadGrant.grantee_user_id == g_user.id,
            VaultReadGrant.owner_user_id == o_user.id,
            VaultReadGrant.scope == scope,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (existing.folders or None) != (wanted or None):
            raise ValueError(
                f"a grant {grantee} -> {owner} ({scope}) already exists with folders="
                f"{existing.folders if existing.folders else 'whole vault'}; "
                f"revoke it first, then grant with the new folders"
            )
        return existing

    grant = VaultReadGrant(
        grantee_user_id=g_user.id,
        owner_user_id=o_user.id,
        scope=scope,
        reason=reason.strip(),
        folders=wanted,
    )
    session.add(grant)
    session.flush()
    return grant
