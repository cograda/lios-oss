"""SMTP transport for the `notify.email` capability.

Why stdlib `smtplib` rather than a provider SDK: comar **vendors** `coglib`
(and, by the same logic, its own integration packages) into deployed images
— `Code/CLAUDE.md`'s house convention, already followed by `coglib.llm`
("no provider SDKs... because coglib is vendored into deployed images"). An
SDK is a dependency in every deployed image forever; `smtplib` +
`email.message.EmailMessage` are already in the standard library and are
enough to talk to any SMTP server, including the SMTP2GO account this
integration is configured against. There is exactly one transport method
here (`SMTP_SSL` on port 465) because that's what SMTP2GO's `mail-eu`
endpoint speaks — no provider abstraction to build for a second provider
that doesn't exist yet.

Why recipients come from a config dict, not a `User.email` column: there is
no `email` column on the `User` model (`app/models/users.py`) — the closest
thing is OAuth account rows scoped to specific integrations (Google, etc.),
none of which is "this person's mailbox for household mail." Adding one
would mean a migration for a value that already has a home: `targets`
(`notify.push`'s user_id -> HA notify-service mapping) already establishes
the pattern of "who receives what" living in this integration's own config
rather than on the user row, precisely because it's notification routing,
not identity — the same reasoning the manifest gives for why `targets` has
no default (a real address/device name here would fail
`tests/test_personalisation_guard.py`). `email_targets` is that same shape
for email: user_id-as-string -> address.

Failure classification mirrors `client.py` (the push sibling): a network
blip or an SMTP 4xx/`SMTPServerDisconnected` is `TransientError` (worth a
caller retrying, though `facade.py` doesn't retry — it just logs and
returns `False`); a missing config key or an SMTP 5xx auth/permission
rejection is `PermanentError` (retrying with the same inputs will not help).
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from app.errors import PermanentError, TransientError
from app.plugin.config_store import plugin_config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailAttachment:
    """One file attachment. Deliberately this narrow — the immediate caller
    (the transcription-capture pipeline, wired up in a later change) needs
    exactly one attachment shape: a `transcript.txt` of `text/plain`. Extend
    this if a second shape shows up rather than generalising ahead of need.
    """

    filename: str
    content_type: str
    content: bytes


class EmailConfigError(PermanentError):
    """notify.email isn't configured for the requested recipient, or at all.

    Permanent: retrying cannot fix a missing config key.
    """


def _resolve_recipient(user_id: int) -> str:
    """Look up the address for `user_id` in `email_targets`.

    Mirrors `client.py::_resolve_targets` for `notify.push` — same
    enforcement point, same reason the manifest doesn't mark this
    `required` (see `manifest.py`'s docstring): the call site has to check
    either way, so it checks and raises a `PermanentError` naming the
    missing key.
    """
    cfg = plugin_config("notifications")
    mapping = cfg.email_targets or {}
    address = mapping.get(str(user_id))
    if not address:
        raise EmailConfigError(
            f"notify.email is not configured for user {user_id}: add an "
            f"entry for \"{user_id}\" to email_targets via "
            "PUT /api/integrations/notifications/config"
        )
    return address


def send(
    to_user_id: int,
    subject: str,
    body_html: str,
    body_text: str | None = None,
    attachments: list[EmailAttachment] | None = None,
) -> None:
    """Send one email via the configured SMTP2GO account.

    Raises `EmailConfigError` (a `PermanentError`) if `to_user_id` has no
    `email_targets` entry, or if SMTP credentials are unset. Raises
    `TransientError` for anything worth retrying and `PermanentError` for
    anything that won't fix itself (auth rejected, etc.) — see the module
    docstring for the classification rule. Callers wanting best-effort
    behaviour should go through `facade.py`'s `send_email()`, which catches
    both and returns `False` instead.
    """
    address = _resolve_recipient(to_user_id)

    cfg = plugin_config("notifications")
    username = cfg.smtp_username
    password = cfg.smtp_password
    if not username or not password:
        raise EmailConfigError(
            "notify.email is not configured: set smtp_username and "
            "smtp_password via PUT /api/integrations/notifications/config"
        )
    host = cfg.smtp_host
    port = cfg.smtp_port
    from_address = cfg.smtp_from_address

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_address
    message["To"] = address
    message.set_content(body_text or "")
    if body_html:
        message.add_alternative(body_html, subtype="html")

    for attachment in attachments or []:
        maintype, _, subtype = attachment.content_type.partition("/")
        message.add_attachment(
            attachment.content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment.filename,
        )

    try:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        # Wrong/revoked credentials — retrying with the same password does
        # nothing.
        raise PermanentError(f"SMTP auth rejected for {username}: {exc}") from exc
    except smtplib.SMTPResponseException as exc:
        # SMTP status codes: 4xx is transient (rate limit, temporary
        # rejection), 5xx is permanent (bad recipient, policy rejection).
        if 400 <= exc.smtp_code < 500:
            raise TransientError(f"SMTP {exc.smtp_code}: {exc.smtp_error!r}") from exc
        raise PermanentError(f"SMTP {exc.smtp_code}: {exc.smtp_error!r}") from exc
    except (OSError, smtplib.SMTPException) as exc:
        # Connection refused, timeout, disconnected mid-transaction — worth
        # a retry.
        raise TransientError(f"SMTP send failed: {exc}") from exc
