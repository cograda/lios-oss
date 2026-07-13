"""One-off: encrypt any plaintext OAuth tokens in the database.

Demoted from a startup hook in main.py (it ran on every boot long after
the one-time migration completed). Run manually if HOME_OAUTH_ENCRYPTION_KEY
is introduced or rotated while plaintext rows exist:

    docker compose exec app python -m app.scripts.encrypt_oauth_tokens
"""

import logging

from app.config import settings
from app.db import get_db

logger = logging.getLogger(__name__)


def encrypt_plaintext_tokens() -> int:
    """Encrypt plaintext OAuth tokens in place. Returns count migrated."""
    if not settings.oauth_encryption_key:
        logger.error("HOME_OAUTH_ENCRYPTION_KEY not set — nothing to do")
        return 0

    from app.auth.encryption import encrypt_token, is_encrypted
    from app.models.tokens import OAuthToken

    db = get_db()
    with db.session() as session:
        migrated = 0
        for t in session.query(OAuthToken).all():
            changed = False
            if t.access_token and not is_encrypted(t.access_token):
                t.access_token = encrypt_token(t.access_token)
                changed = True
            if t.refresh_token and not is_encrypted(t.refresh_token):
                t.refresh_token = encrypt_token(t.refresh_token)
                changed = True
            if changed:
                migrated += 1
        if migrated:
            session.commit()
    return migrated


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    count = encrypt_plaintext_tokens()
    print(f"Encrypted {count} plaintext OAuth token(s)")
