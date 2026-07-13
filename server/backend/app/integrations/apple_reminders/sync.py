"""Sync Apple Reminders from Mac agent push data into Postgres."""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.integrations.apple_reminders.models import Reminder

logger = logging.getLogger(__name__)


def sync_from_push(
    reminders_data: list[dict],
    session: Session,
    *,
    user_id: int,
) -> dict:
    """Upsert reminders received from the Mac agent.

    Args:
        reminders_data: List of reminder dicts from the Mac agent. Each may
            carry an optional `account_email` key — the iCloud account that
            owns the source list (used in EventKit multi-account routing).
        session: SQLAlchemy session.
        user_id: ID of the user that owns this reminders snapshot. Reminders
            are scoped per-user — the stale-reaper only marks rows belonging
            to this user, so other users' reminders aren't touched.

    Returns:
        Dict with "count" (total processed), "newly_completed" (transitioned
        to completed), "newly_added" (brand new reminders), and "edited"
        (existing reminders with changed summary/priority/due_date).
    """
    now = datetime.now(timezone.utc)
    seen_uids = set()
    synced = 0
    newly_completed = 0
    newly_added = 0
    edited = 0

    for item in reminders_data:
        uid = item.get("uid", "").strip()
        if not uid:
            continue

        seen_uids.add(uid)

        # Parse due_date
        due_date = None
        if item.get("due_date"):
            try:
                due_date = datetime.fromisoformat(item["due_date"])
                if due_date.tzinfo is None:
                    due_date = due_date.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        # Parse completed_date
        completed_date = None
        if item.get("completed_date"):
            try:
                completed_date = datetime.fromisoformat(item["completed_date"])
                if completed_date.tzinfo is None:
                    completed_date = completed_date.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        existing = (
            session.query(Reminder)
            .filter_by(user_id=user_id, uid=uid)
            .first()
        )
        is_now_completed = item.get("completed", False)

        if existing:
            # Detect transition: was incomplete, now completed
            if not existing.completed and is_now_completed:
                newly_completed += 1
            # Detect meaningful edits (summary, priority, or due date changed)
            new_summary = item.get("summary", existing.summary)
            new_priority = item.get("priority", 0)
            if (
                existing.summary != new_summary
                or existing.priority != new_priority
                or existing.due_date != due_date
            ):
                edited += 1
            existing.summary = new_summary
            existing.notes = item.get("notes")
            existing.list_name = item.get("list_name", existing.list_name)
            existing.due_date = due_date
            existing.priority = new_priority
            existing.completed = is_now_completed
            existing.completed_date = completed_date
            existing.synced_at = now
        else:
            if not is_now_completed:
                newly_added += 1
            reminder = Reminder(
                user_id=user_id,
                uid=uid,
                account_email=item.get("account_email"),
                list_name=item.get("list_name", "Reminders"),
                summary=item.get("summary", ""),
                notes=item.get("notes"),
                due_date=due_date,
                priority=item.get("priority", 0),
                completed=is_now_completed,
                completed_date=completed_date,
                sync_direction="from_apple",
                synced_at=now,
            )
            session.add(reminder)
        synced += 1

    # Mark reminders not in the push as completed (deleted or completed on
    # device). Scoped to this user — never touch other users' rows.
    if seen_uids:
        stale = (
            session.query(Reminder)
            .filter(
                Reminder.user_id == user_id,
                Reminder.completed == False,  # noqa: E712
                Reminder.sync_direction == "from_apple",
                Reminder.uid.notin_(seen_uids),
            )
            .all()
        )
        for r in stale:
            r.completed = True
            r.completed_date = now
            r.synced_at = now
            newly_completed += 1

    session.commit()
    logger.info(
        f"Push sync: {synced} processed, {newly_completed} completed, "
        f"{newly_added} added, {edited} edited"
    )
    return {
        "count": synced,
        "newly_completed": newly_completed,
        "newly_added": newly_added,
        "edited": edited,
    }
