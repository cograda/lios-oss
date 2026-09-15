"""notifications' facade — capabilities `notify.push` and `notify.email`.

The only sanctioned way another integration reaches this one (V4 chunk 4.2):
`get_capability("notify.push").send(...)` (or `get_capability("notify.email")
.send_email(...)` — same object, both capability names resolve to it),
having declared the capability in its own manifest's `depends_on`. Nothing
may import `..client`, `..email_client` or `..sweep` directly —
`tests/test_capability_boundaries.py` walks every file under
`app/integrations/` and enforces that.

Deliberately narrow. `send()` is exposed because "tell the household something
happened" is a plausible need for other integrations (a snag captured, a
commute washed out). The sweep and the ledger are not exposed: alert
reconciliation has exactly one owner, and a second caller writing rows would
break the one-open-row-per-fingerprint invariant that makes dedup work.

`send()` swallows failures by design. Every caller is a best-effort notify
alongside real work — the same stance `snags` takes toward the Sheets mirror,
where a Sheets outage must never block the underlying DB write. A dropped push
must never fail the operation that triggered it.

`user_id` (added 2026-08-13, alongside the ntfy -> Home Assistant sink swap):
`None` (the default) means household-wide — fan out to every configured
`household_targets` entry, the shape every caller used before this parameter
existed. Passing a real `user_id` routes to that one person's device via the
`targets` config mapping instead. Routing is resolved in `client.py`, not
here; this facade stays a thin pass-through.

`send_email()` (added 2026-08-29, `notify.email`) takes the identical
best-effort stance for the identical reason: its first intended caller is
the transcription-capture pipeline, where the transcript is already saved
to the inbox before any email is attempted — a mail outage must never fail
the capture. Unlike push there is no household-wide fan-out shape: email
always names one `to_user_id`, resolved against `email_targets` in
`email_client.py`.
"""

from __future__ import annotations

import logging

from app.errors import ComarError
from app.integrations.notifications import client
from app.integrations.notifications import email_client
from app.integrations.notifications.email_client import EmailAttachment

logger = logging.getLogger(__name__)


class NotificationsFacade:
    def send(
        self,
        title: str,
        body: str,
        severity: str = "warning",
        user_id: int | None = None,
        *,
        source: str = "adhoc",
        data: dict | None = None,
    ) -> bool:
        """Best-effort push. Returns whether it landed; never raises.

        `user_id=None` sends household-wide; a real `user_id` routes to that
        one person's configured device.

        `source` (added 2026-09-04, part of "ledger every publish" — see
        `client.py`'s docstring) names the calling integration for
        `notify_recent` — pass e.g. `"household"`, `"tasks"`, `"inbox"`.
        `client.publish()` writes the ledger row itself; this facade just
        forwards the tag.

        `data` (added 2026-09-11 for the signals watchers) is merged into the
        HA notify payload's own `data` — e.g. `{"entity_id": "camera.front_
        door_high_resolution_channel"}` so the push shows a live camera view,
        the same shape `gate_package_alert.yaml`'s automation sends.
        """
        try:
            client.publish(title, body, severity, user_id=user_id, source=source, extra_data=data)
            return True
        except ComarError as exc:
            logger.warning("notify.push send failed (%s): %s", title, exc)
            return False
        except Exception:
            # A facade used from inside other integrations' write paths must be
            # incapable of taking them down, including on a bug in here.
            logger.exception("notify.push send raised unexpectedly (%s)", title)
            return False

    def send_email(
        self,
        to_user_id: int,
        subject: str,
        body_html: str,
        body_text: str | None = None,
        attachments: list[EmailAttachment] | None = None,
    ) -> bool:
        """Best-effort email. Returns whether it landed; never raises.

        Same stance as `send()` above: the immediate caller is a capture
        pipeline that has already persisted the thing this email is about,
        so a dropped or misconfigured send must never fail that write.
        """
        try:
            email_client.send(to_user_id, subject, body_html, body_text, attachments)
            return True
        except ComarError as exc:
            logger.warning("notify.email send failed (%s): %s", subject, exc)
            return False
        except Exception:
            logger.exception("notify.email send raised unexpectedly (%s)", subject)
            return False


FACADE = NotificationsFacade()
