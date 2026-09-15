"""Mint a single-use install code for onboarding a new machine.

Usage (inside the container):
    docker exec -it lios-core python -m app.scripts.create_install_code \
        --user sam --label sam-macbook

Prints:
    - The curl one-liner the new machine runs.
    - The raw bearer token (for AirDrop-a-.command fallback if Tailscale isn't
      up at install time and the URL approach can't reach the server).

The code expires in 24h or on first redemption, whichever comes first.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.auth.encryption import encrypt_token
from app.db import get_db
from app.models.clients import ClientToken, InstallCode
from app.models.users import User


PUBLIC_URL_DEFAULT = "https://ubuntudockerbox.tail78010b.ts.net"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Mint a single-use install code.")
    p.add_argument("--user", required=True, help="User short name (e.g. 'sam').")
    p.add_argument("--label", required=True, help="Device label (e.g. 'sam-macbook').")
    p.add_argument("--ttl-hours", type=int, default=24, help="Code lifetime in hours (default 24).")
    p.add_argument("--public-url", default=PUBLIC_URL_DEFAULT,
                   help=f"Public server URL to print in the curl one-liner (default {PUBLIC_URL_DEFAULT}).")
    args = p.parse_args(argv)

    db = get_db()
    with db.session() as session:
        user = session.execute(select(User).where(User.name == args.user)).scalar_one_or_none()
        if user is None:
            print(f"error: no user with name '{args.user}' in users table", file=sys.stderr)
            return 2
        if not user.is_active:
            print(f"error: user '{args.user}' is not active", file=sys.stderr)
            return 2

        # Mint the bearer this install will use. Only the hash lands in
        # client_tokens; the plaintext rides on the InstallCode row until
        # redemption (or 24h TTL), same as the code itself — see
        # InstallCode's docstring for the threat-model reasoning.
        token, plaintext = ClientToken.mint(
            user_id=user.id, label=args.label, is_active=True,
        )
        session.add(token)
        session.flush()  # populate token.id without commit

        now = datetime.now(timezone.utc)
        install = InstallCode(
            code=secrets.token_urlsafe(16),
            user_id=user.id,
            label=args.label,
            token_id=token.id,
            # F4: encrypted at rest (same Fernet machinery as oauth_tokens /
            # integration_config) — decrypted only in app/routes/install.py
            # at redemption time.
            token_plaintext=encrypt_token(plaintext),
            created_at=now,
            expires_at=now + timedelta(hours=args.ttl_hours),
        )
        session.add(install)

        # Snapshot for printing before commit closes the session.
        code = install.code
        token_str = plaintext
        expires = install.expires_at

        session.commit()

    public = args.public_url.rstrip("/")
    print()
    print(f"  User:    {args.user}")
    print(f"  Label:   {args.label}")
    print(f"  Expires: {expires.isoformat()}  ({args.ttl_hours}h)")
    print()
    print("Run on the new machine:")
    print()
    print(f"  curl -fsSL {public}/api/install/{code} | bash")
    print()
    print("Fallback (if curl can't reach the server before Tailscale is up):")
    print()
    print(f"  Bearer token: {token_str}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
