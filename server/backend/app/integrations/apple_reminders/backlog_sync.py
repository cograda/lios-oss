"""Two-way sync between Apple Reminders and vault task backlogs.

Uses local fuzzy string matching (difflib) between reminder text and backlog items.
Vault is source of truth for conflicts.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.integrations.apple_reminders.commands import dispatch_command
from app.integrations.apple_reminders.models import Reminder, ReminderCommand

logger = logging.getLogger(__name__)

# ─── Backlog parsing ───

# Priority emoji → Apple Reminders priority (0=none, 1=high, 5=medium, 9=low)
PRIORITY_MAP = {
    "🔺": 1,   # urgent
    "⏫": 1,   # high
    "🔼": 5,   # medium
    "🔽": 9,   # low
}

PRIORITY_REVERSE = {1: "⏫", 5: "🔼", 9: "🔽", 0: ""}

# Reminders list name → vault backlog file (relative to vault root)
# Single-user vault: every reminder list feeds the one unified backlog.
LIST_TO_BACKLOG = {
    "Household": "Task Backlog.md",
    "Reminders": "Task Backlog.md",
    "Alex": "Task Backlog.md",
    "Alex Personal": "Task Backlog.md",
}

# Reverse: backlog file → preferred Reminders list name
BACKLOG_TO_LIST = {
    "Task Backlog.md": "Household",
}

# Regex to parse a task line: - [ ] or - [x] followed by text
TASK_RE = re.compile(
    r"^- \[([ xX])\] (.+)$"
)

# Date pattern in task text: 📅 YYYY-MM-DD
DUE_DATE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")

# Priority emoji pattern
PRIORITY_RE = re.compile(r"[🔺⏫🔼🔽]")

# Tag pattern
TAG_RE = re.compile(r"#[\w/.-]+")


@dataclass
class BacklogTask:
    text: str
    completed: bool = False
    priority: int = 0  # Apple Reminders priority scale
    priority_emoji: str = ""
    due_date: str | None = None
    tags: list[str] = field(default_factory=list)
    section: str = ""
    line_number: int = 0
    raw_line: str = ""


def parse_backlog(content: str) -> list[BacklogTask]:
    """Parse vault backlog markdown into structured tasks.

    Only parses top-level task lines (- [ ] / - [x]).
    Skips Obsidian Tasks plugin query blocks.
    """
    tasks = []
    current_section = ""
    in_code_block = False

    for line_num, line in enumerate(content.split("\n"), start=1):
        stripped = line.strip()

        # Track code blocks (skip Tasks plugin queries)
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        # Track section headers
        if stripped.startswith("## "):
            current_section = stripped[3:].strip()
            continue

        # Only match top-level tasks (no leading whitespace beyond the dash)
        if not line.startswith("- ["):
            continue

        match = TASK_RE.match(stripped)
        if not match:
            continue

        checkbox, task_text = match.groups()
        completed = checkbox.lower() == "x"

        # Extract priority
        priority = 0
        priority_emoji = ""
        pri_match = PRIORITY_RE.search(task_text)
        if pri_match:
            priority_emoji = pri_match.group()
            priority = PRIORITY_MAP.get(priority_emoji, 0)

        # Extract due date
        due_date = None
        date_match = DUE_DATE_RE.search(task_text)
        if date_match:
            due_date = date_match.group(1)

        # Extract tags
        tags = TAG_RE.findall(task_text)

        # Clean text: remove priority emoji, date, tags for matching
        clean = task_text
        clean = PRIORITY_RE.sub("", clean)
        clean = DUE_DATE_RE.sub("", clean)
        clean = TAG_RE.sub("", clean)
        # Remove wiki links but keep display text
        clean = re.sub(r"\[\[([^|\]]*\|)?([^\]]+)\]\]", r"\2", clean)
        clean = clean.strip()

        tasks.append(BacklogTask(
            text=clean,
            completed=completed,
            priority=priority,
            priority_emoji=priority_emoji,
            due_date=due_date,
            tags=tags,
            section=current_section,
            line_number=line_num,
            raw_line=line,
        ))

    return tasks


# ─── Local fuzzy matching ───


def _normalize(text: str) -> str:
    """Normalize text for comparison: lowercase, collapse whitespace, strip punctuation."""
    text = text.lower().strip()
    text = re.sub(r"[—–\-]+", " ", text)  # dashes to spaces
    text = re.sub(r"\s+", " ", text)  # collapse whitespace
    return text


def match_tasks_local(
    backlog_tasks: list[BacklogTask],
    reminders: list[Reminder],
    threshold: float = 0.65,
) -> list[dict]:
    """Fuzzy-match backlog tasks to reminders using SequenceMatcher.

    Returns list of {"backlog_idx": int, "reminder_uid": str, "confidence": float}.
    Uses Ratcliff/Obershelp similarity (difflib). No API key needed.
    """
    if not backlog_tasks or not reminders:
        return []

    matches = []
    matched_uids: set[str] = set()

    for i, task in enumerate(backlog_tasks):
        if task.completed:
            continue

        task_norm = _normalize(task.text)
        if not task_norm:
            continue

        best_ratio = 0.0
        best_reminder = None

        for r in reminders:
            if r.uid in matched_uids:
                continue
            if not r.summary:
                continue

            reminder_norm = _normalize(r.summary)
            ratio = SequenceMatcher(None, task_norm, reminder_norm).ratio()

            # Also check if one contains the other (handles truncation)
            if task_norm in reminder_norm or reminder_norm in task_norm:
                ratio = max(ratio, 0.85)

            if ratio > best_ratio:
                best_ratio = ratio
                best_reminder = r

        if best_ratio >= threshold and best_reminder:
            matches.append({
                "backlog_idx": i,
                "reminder_uid": best_reminder.uid,
                "confidence": round(best_ratio, 3),
            })
            matched_uids.add(best_reminder.uid)

    return matches


# ─── Sync engine ───


def _vault_push_enabled() -> bool:
    """Whether the vault→Reminders direction should actually write.

    Default off. It was effectively off for five months anyway (rows were
    created and never dispatched), so defaulting it on would turn a silent
    no-op into a surprise write of one reminder per unmatched backlog task on
    the very next 30-minute tick. Opt in deliberately, watch what lands.
    """
    from app.plugin.config_store import plugin_config

    try:
        return bool(plugin_config("apple_reminders").reminders_push_vault_to_reminders)
    except Exception:  # noqa: BLE001
        # A config read failure must not turn the write path on.
        logger.exception("could not read reminders_push_vault_to_reminders; treating as off")
        return False


def _already_queued(session: Session, *, user_id: int, summary: str) -> bool:
    r"""Whether an equivalent `add` is already queued for this user.

    Compares the **decoded** `summary` field, never a substring of the stored
    JSON. The previous `payload.contains(task.text[:50])` could not match its
    own output: `json.dumps` escapes `"` to `\"` and non-ASCII to `\uXXXX`, so
    any task with a quote or an em-dash failed to find itself and was re-queued
    every run. That single line produced ~3,300 rows a day.

    Compares full text rather than a 50-character prefix, too — two backlog
    tasks sharing an opening phrase are different tasks, and the prefix test
    would have silently dropped the second.
    """
    rows = (
        session.query(ReminderCommand.payload)
        .filter(
            ReminderCommand.user_id == user_id,
            ReminderCommand.action == "add",
            ReminderCommand.status == "pending",
        )
        .all()
    )
    for (payload,) in rows:
        if not payload:
            continue
        try:
            if json.loads(payload).get("args", {}).get("summary") == summary:
                return True
            # dispatch_command nests under "args"; rows written by the older
            # direct-insert path stored the fields flat. Accept both so the
            # guard still recognises pre-existing rows.
            if json.loads(payload).get("summary") == summary:
                return True
        except (TypeError, ValueError):
            continue
    return False


def sync_backlogs(session: Session, vault_path: str, user_id: int) -> dict:
    """Two-way sync between one user's vault backlogs and their Apple Reminders.

    1. Parse vault backlogs
    2. Match against Reminders in DB (local fuzzy matching, no API key needed)
    3. Vault → Reminders: new backlog tasks → queue add commands
    4. Reminders → Vault: completed reminders → mark done in backlog

    Scoped to `user_id` throughout (sam-rollout A3, 2026-07-26) — every
    user has their own vault and their own reminders, so the caller loops
    over users and calls this once per user rather than this function
    fanning out itself.

    Returns sync stats.
    """
    vp = Path(vault_path)
    if not vp.is_dir():
        return {"error": "Vault path not found"}

    # `dispatch_command` targets the SSE stream by user *name*, not id (see
    # stream_manager.publish's `target_user`), so resolve it once here rather
    # than per task. Resolved even when the push direction is disabled, so a
    # missing user row fails loudly at the start instead of on first write.
    from app.models.users import User

    user_row = session.get(User, user_id)
    if user_row is None:
        return {"error": f"No user row for user_id={user_id}"}
    user_name = user_row.name

    stats = {
        "matched": 0,
        "new_to_reminders": 0,
        "completed_in_vault": 0,
        "errors": 0,
        # Vault tasks with no matching reminder that were *not* pushed, because
        # the vault→Reminders direction is off. Reported so "sync ran, nothing
        # happened" and "sync ran, this direction is disabled" are different
        # readings at a glance.
        "vault_only_not_pushed": 0,
        "queued_not_applied": 0,
    }

    for backlog_file, list_name in BACKLOG_TO_LIST.items():
        backlog_path = vp / backlog_file
        if not backlog_path.exists():
            continue

        try:
            content = backlog_path.read_text(encoding="utf-8")
            backlog_tasks = parse_backlog(content)
        except Exception as e:
            logger.error(f"Failed to parse {backlog_file}: {e}")
            stats["errors"] += 1
            continue

        # Get reminders for this list, scoped to the user whose vault this
        # backlog belongs to (sam-rollout A3 — one pass per user, see
        # run_scheduled_sync below).
        reminders = (
            session.query(Reminder)
            .filter(Reminder.user_id == user_id, Reminder.list_name == list_name)
            .all()
        )

        if not backlog_tasks and not reminders:
            continue

        # Match using local fuzzy matching (no API key needed)
        matches = match_tasks_local(backlog_tasks, reminders)
        matched_backlog_idxs = {m["backlog_idx"] for m in matches}
        matched_reminder_uids = {m["reminder_uid"] for m in matches}
        stats["matched"] += len(matches)

        # Vault → Reminders: unmatched open backlog tasks → create reminders.
        #
        # ⚠️ Gated, and default OFF — because until 2026-08-19 this loop wrote
        # `ReminderCommand` rows that **nothing ever dispatched**. There is no
        # `dispatch_command()` call here and no reaper drains the table, so the
        # rows accumulated forever: 107,058 pending `add`s spanning five months,
        # ~3,300 a day, all abandoned in one sweep on that date. Anyone reading
        # `stats["new_to_reminders"]` was reading the count of rows written to a
        # queue with no reader — which is why a task re-dated in the vault never
        # reached Reminders and looked 18 days overdue instead.
        #
        # Two faults compounded. The volume came from the dedupe guard below,
        # which used `payload.contains(task.text[:50])` — a raw-text substring
        # test against `json.dumps` output. `json.dumps` escapes `"` to `\"` and
        # (with the default `ensure_ascii=True`) `—` to `\u2014`, so a task
        # containing a quote or any non-ASCII character never matched itself and
        # was re-queued on every 30-minute run. Measured on live rows: the test
        # returned False for all of them, including a pure-ASCII one, because it
        # contained quotes.
        #
        # So the guard now decodes payloads and compares the `summary` field,
        # never a substring of serialised JSON, and the whole loop only runs when
        # someone has deliberately enabled it. Turning it on dispatches for real.
        push_to_reminders = _vault_push_enabled()
        for i, task in enumerate(backlog_tasks):
            if task.completed or i in matched_backlog_idxs:
                continue

            if not push_to_reminders:
                # Count it so the gap stays *visible* rather than silent — a
                # disabled path that reports nothing is indistinguishable from
                # a working one with nothing to do.
                stats["vault_only_not_pushed"] += 1
                continue

            if _already_queued(session, user_id=user_id, summary=task.text):
                continue

            payload = {
                "summary": task.text,
                "list": list_name,
                "due_date": task.due_date,
                "priority": (
                    "high" if task.priority == 1
                    else "medium" if task.priority == 5
                    else "low" if task.priority == 9
                    else "none"
                ),
            }
            # Dispatch rather than only enqueue. `dispatch_command` creates the
            # row itself, publishes it over SSE and waits for the daemon's ack,
            # so a row exists only as the record of a real attempt.
            result = dispatch_command(
                session,
                user_id=user_id,
                user_name=user_name,
                action="add",
                args=payload,
            )
            if result.get("synced"):
                stats["new_to_reminders"] += 1
            else:
                # Queued-but-not-applied. Counted separately because conflating
                # the two is the exact bug being fixed here: a queued write is
                # not a completed write, and the reaper (commands.py) is what
                # will retry it.
                stats["queued_not_applied"] += 1

        # Reminders → Vault: completed reminders that match open backlog tasks → mark done
        lines = content.split("\n")
        modified = False

        for match in matches:
            uid = match["reminder_uid"]
            idx = match["backlog_idx"]

            reminder = next((r for r in reminders if r.uid == uid), None)
            if not reminder or not reminder.completed:
                continue

            task = backlog_tasks[idx]
            if task.completed:
                continue

            # Mark task as done in the backlog file
            line_idx = task.line_number - 1
            if 0 <= line_idx < len(lines):
                old_line = lines[line_idx]
                if old_line.startswith("- [ ]"):
                    lines[line_idx] = old_line.replace("- [ ]", "- [x]", 1)
                    modified = True
                    stats["completed_in_vault"] += 1

        if modified:
            backlog_path.write_text("\n".join(lines), encoding="utf-8")

    session.commit()
    return stats


# ─── Scheduled cron entry (manifest background_tasks, moved from
#     app/scheduler.py in V4 chunk 3.1) ───
#
# sam-rollout A3 (2026-07-26): loops over every active user, resolving
# each one's vault via app.services.vault_paths.user_vault_path(user.name)
# (same pattern as obsidian/__init__.py's accounts()/store()) rather than
# the single hardcoded settings.obsidian_vault_path / user_id=1 pass. A user
# with no vault directory on disk is skipped (logged, not an error) — this
# also fixes a pre-existing bug where `asyncio.run(sync_backlogs(...))` was
# passed an already-evaluated dict (sync_backlogs is a plain `def`, not a
# coroutine function), which would have raised a TypeError the first time
# this path was actually exercised.


def _run_scheduled_sync_blocking() -> None:
    """Run vault backlog <-> Reminders sync, once per active user, in a thread."""
    from app.auth.context import use_user
    from app.db import get_db
    from app.models.users import User
    from app.services import vault_paths

    db = get_db()
    with db.session() as session:
        users = session.query(User).filter_by(is_active=True).order_by(User.id).all()
        for user in users:
            vault_dir = vault_paths.user_vault_path(user.name)
            if not vault_dir.is_dir():
                logger.info(f"Backlog sync: no vault for {user.name}, skipping")
                continue
            with use_user(user.id):
                result = sync_backlogs(session, str(vault_dir), user.id)
            if result.get("matched") or result.get("completed_in_vault") or result.get("new_to_reminders"):
                logger.info(f"Backlog sync [{user.name}]: {result}")


async def run_scheduled_sync() -> None:
    """Sync vault backlogs with Apple Reminders (cron: every 30 minutes)."""
    import asyncio

    try:
        await asyncio.to_thread(_run_scheduled_sync_blocking)
    except Exception:
        logger.exception("Backlog sync failed")
