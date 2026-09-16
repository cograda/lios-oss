"""Generic 'push a table of rows to a Google Sheet' writer.

Creates the spreadsheet once per `key` (tracked in `sheet_exports`), shares it
with the given collaborators at creation time, then overwrites its single
data sheet wholesale on every subsequent call — simplest possible contract
for a small household table, no incremental diffing.

Failures here raise (classified Transient/Permanent) rather than swallow —
callers decide whether a Sheets hiccup should block their own write. The
snags integration's caller wraps this in a broad try/except precisely because
a Sheets outage must never block the underlying DB write it mirrors.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.integrations.sheets.client import (
    _classify, get_drive_service, get_sheets_service,
)
from app.integrations.sheets.models import SheetExport

logger = logging.getLogger(__name__)

DATA_RANGE = "A1"  # top-left of the single data sheet; we overwrite wholesale
CLEAR_RANGE = "A:Z"  # generous — clears any width a previous write used


def _create_spreadsheet(
    sheets_service, drive_service, title: str, share_with: list[str],
) -> tuple[str, str]:
    """Create a new spreadsheet, share it, return (spreadsheet_id, url)."""
    body = {"properties": {"title": title}}
    result = (
        sheets_service.spreadsheets()
        .create(body=body, fields="spreadsheetId,spreadsheetUrl")
        .execute()
    )
    spreadsheet_id = result["spreadsheetId"]
    url = result["spreadsheetUrl"]

    for email in share_with:
        try:
            drive_service.permissions().create(
                fileId=spreadsheet_id,
                body={"type": "user", "role": "writer", "emailAddress": email},
                sendNotificationEmail=True,
                fields="id",
            ).execute()
        except Exception:
            # Non-fatal — the sheet exists even if one share invite fails;
            # log it so it surfaces rather than silently leaving someone out.
            logger.exception(f"[sheets] failed to share {spreadsheet_id} with {email}")

    return spreadsheet_id, url


def ensure_export(
    session: Session,
    *,
    key: str,
    title: str,
    owner_account_email: str,
    owner_user_id: int,
    share_with: list[str],
) -> SheetExport | None:
    """Get the SheetExport row for `key`, creating the spreadsheet on first call.

    Returns None (not an error) if the owner account has no valid Google
    credentials yet — callers should treat this as "export not configured".
    """
    export = session.query(SheetExport).filter_by(key=key).one_or_none()
    if export is not None:
        return export

    sheets_service = get_sheets_service(owner_account_email, session, user_id=owner_user_id)
    drive_service = get_drive_service(owner_account_email, session, user_id=owner_user_id)
    if sheets_service is None or drive_service is None:
        return None

    try:
        spreadsheet_id, url = _create_spreadsheet(sheets_service, drive_service, title, share_with)
    except Exception as exc:
        raise _classify(exc, f"create spreadsheet for {key!r}") from exc

    export = SheetExport(
        key=key,
        title=title,
        spreadsheet_id=spreadsheet_id,
        spreadsheet_url=url,
        owner_account_email=owner_account_email,
        shared_with=json.dumps(share_with),
    )
    session.add(export)
    session.commit()
    logger.info(f"[sheets] created export {key!r} -> {url}")
    return export


def write_rows(
    session: Session,
    export: SheetExport,
    *,
    owner_user_id: int,
    headers: list[str],
    rows: list[list],
) -> None:
    """Overwrite the export's data sheet with headers + rows."""
    sheets_service = get_sheets_service(export.owner_account_email, session, user_id=owner_user_id)
    if sheets_service is None:
        return

    values = [headers] + rows
    try:
        # Clear first — row/column count can shrink between writes (e.g. a
        # snag closes and its row disappears), and `update` alone would leave
        # stale trailing rows from a previous, longer write.
        sheets_service.spreadsheets().values().clear(
            spreadsheetId=export.spreadsheet_id, range=CLEAR_RANGE,
        ).execute()
        sheets_service.spreadsheets().values().update(
            spreadsheetId=export.spreadsheet_id,
            range=DATA_RANGE,
            valueInputOption="RAW",
            body={"values": values},
        ).execute()
    except Exception as exc:
        raise _classify(exc, f"write rows for {export.key!r}") from exc

    export.last_synced_at = datetime.now(timezone.utc)
    session.commit()
