"""Grant, list or revoke read-only cross-user vault access.

Usage (inside the container):
    docker exec -it lios-core python -m app.scripts.grant_vault_read \
        --grantee agent --owner alex --reason "lios agent host operates against the vault"

    # Folder-scoped: the grantee reads ONLY these vault-relative folders.
    docker exec -it lios-core python -m app.scripts.grant_vault_read \
        --grantee sam --owner alex --folders Household/,People/ \
        --reason "renovation and household context, shared rather than copied (2026-09-06)"

    docker exec -it lios-core python -m app.scripts.grant_vault_read --list
    docker exec -it lios-core python -m app.scripts.grant_vault_read \
        --revoke --grantee agent --owner alex

A grant is one row. Revoking is one `DELETE` and takes effect on the next tool
call — there is no credential to rotate, no token to expire and nothing to
redeploy, which is the property that makes granting it reasonable in the first
place.

`--reason` is required on grant and is not decoration: the person deciding
whether to revoke, months from now, is the person reading it.

`--folders` is comma-separated, each entry normalised to a trailing slash;
absolute paths, `..` components and empty entries are rejected here before
anything reaches the database. Omit it for the whole vault. Changing the
folders of an existing grant is revoke-then-grant, deliberately — see
`app.services.vault_grants.grant_read`.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.db import get_db
from app.models.users import User
from app.models.vault_grants import GRANTABLE_SCOPES, VaultReadGrant
from app.services.vault_grants import grant_read
from app.services.vault_scope import normalise_folders


def parse_folders(raw: str | None) -> list[str] | None:
    """`"Household/,People"` -> `["Household/", "People/"]`; None -> None.

    Rejects rather than skips a bad entry: `--folders Household/,` has a
    trailing empty item, and dropping it silently would grant exactly what
    the operator typed minus the part they mistyped — which is fine — but
    `--folders ,` would then grant nothing-and-therefore-everything.
    """
    if raw is None:
        return None
    parts = [p for p in raw.split(",")]
    return normalise_folders(parts)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Manage cross-user vault read grants.")
    p.add_argument("--grantee", help="User who may read (e.g. 'agent').")
    p.add_argument("--owner", help="User whose vault may be read (e.g. 'alex').")
    p.add_argument("--scope", default="obsidian",
                   help=f"Integration scope. Allowed: {', '.join(sorted(GRANTABLE_SCOPES))}.")
    p.add_argument("--reason", default="", help="Why this grant exists (required to grant).")
    p.add_argument(
        "--folders", default=None,
        help=(
            "Comma-separated vault-relative folders the grantee may read, e.g. "
            "'Household/,People/'. Omit for the whole vault."
        ),
    )
    p.add_argument("--list", action="store_true", help="List existing grants and exit.")
    p.add_argument("--revoke", action="store_true", help="Remove the named grant.")
    args = p.parse_args(argv)

    try:
        folders = parse_folders(args.folders)
    except ValueError as e:
        print(f"error: --folders: {e}", file=sys.stderr)
        return 2

    db = get_db()

    if args.list:
        with db.session() as session:
            rows = session.execute(select(VaultReadGrant)).scalars().all()
            if not rows:
                print("no grants")
                return 0
            names = {u.id: u.name for u in session.execute(select(User)).scalars()}
            for g in rows:
                scope_txt = ",".join(g.folders) if g.folders else "whole vault"
                print(
                    f"  {names.get(g.grantee_user_id, g.grantee_user_id)}"
                    f" -> {names.get(g.owner_user_id, g.owner_user_id)}"
                    f"  scope={g.scope}  folders={scope_txt}"
                    f"  since={g.created_at:%Y-%m-%d}  reason={g.reason!r}"
                )
        return 0

    if not args.grantee or not args.owner:
        print("error: --grantee and --owner are required", file=sys.stderr)
        return 2

    with db.session() as session:
        if args.revoke:
            g = session.execute(select(User).where(User.name == args.grantee)).scalar_one_or_none()
            o = session.execute(select(User).where(User.name == args.owner)).scalar_one_or_none()
            if g is None or o is None:
                print("error: unknown user", file=sys.stderr)
                return 2
            row = session.execute(
                select(VaultReadGrant).where(
                    VaultReadGrant.grantee_user_id == g.id,
                    VaultReadGrant.owner_user_id == o.id,
                    VaultReadGrant.scope == args.scope,
                )
            ).scalar_one_or_none()
            if row is None:
                print("no such grant — nothing to revoke")
                return 0
            session.delete(row)
            session.commit()
            print(f"revoked: {args.grantee} -> {args.owner} ({args.scope})")
            return 0

        try:
            grant_read(
                session,
                grantee=args.grantee, owner=args.owner,
                scope=args.scope, reason=args.reason, folders=folders,
            )
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        session.commit()
        scope_txt = f"folders {', '.join(folders)}" if folders else "whole vault"
        print(
            f"granted: {args.grantee} may read {args.owner}'s vault "
            f"({args.scope}, read-only, {scope_txt})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
