"""Google Docs + Drive API client — thin service builders, same shape as
sheets/client.py, google_calendar/client.py and google_mail/client.py."""

import logging

from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from app.auth.oauth import get_credentials
from app.errors import PermanentError
from app.plugin.sync_runtime import classify_exc

logger = logging.getLogger(__name__)

# See google_calendar/client.py::_classify for the full rationale (auth
# failures are handled upstream by get_credentials before any API call is
# made — a 401/403 reaching here means insufficient scope or a disabled API,
# still permanent but distinct — override keeps that instead of classify_exc's
# oauth-provider NeedsReauthError default).
#
# 401/403 is the *expected* failure when an existing token predates this
# integration: the `documents` scope is new to the union, and a token minted
# before it was declared does not carry it. The fix is re-consent, not a
# retry, so PermanentError is the honest classification.
_OVERRIDES = {401: PermanentError, 403: PermanentError}


def _classify(exc: Exception, context: str) -> Exception:
    return classify_exc(exc, context, provider="google", overrides=_OVERRIDES)


def get_docs_service(account_email: str, session: Session, *, user_id: int):
    creds = get_credentials(account_email, session, user_id=user_id)
    if creds is None:
        logger.warning(f"[google_docs] no credentials for {account_email}")
        return None
    return build("docs", "v1", credentials=creds)


def get_drive_service(account_email: str, session: Session, *, user_id: int):
    creds = get_credentials(account_email, session, user_id=user_id)
    if creds is None:
        logger.warning(f"[google_docs] no credentials for {account_email}")
        return None
    return build("drive", "v3", credentials=creds)
