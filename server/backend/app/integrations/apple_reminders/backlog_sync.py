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


def sync_backlogs(session: Session, vault_path: str) -> dict:
    """Two-way sync between vault backlogs and Apple Reminders.

    1. Parse vault backlogs
    2. Match against Reminders in DB (local fuzzy matching, no API key needed)
    3. Vault → Reminders: new backlog tasks → queue add commands
    4. Reminders → Vault: completed reminders → mark done in backlog

    Returns sync stats.
    """
    vp = Path(vault_path)
    if not vp.is_dir():
        return {"error": "Vault path not found"}

    stats = {"matched": 0, "new_to_reminders": 0, "completed_in_vault": 0, "errors": 0}

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

        # Get reminders for this list. Backlog sync runs outside an MCP
        # request context, so we hardcode Alex (user_id=1) — the vault is
        # single-user and all lists feed the one unified backlog.
        reminders = (
            session.query(Reminder)
            .filter(Reminder.user_id == 1, Reminder.list_name == list_name)
            .all()
        )

        if not backlog_tasks and not reminders:
            continue

        # Match using local fuzzy matching (no API key needed)
        matches = match_tasks_local(backlog_tasks, reminders)
        matched_backlog_idxs = {m["backlog_idx"] for m in matches}
        matched_reminder_uids = {m["reminder_uid"] for m in matches}
        stats["matched"] += len(matches)

        # Vault → Reminders: unmatched open backlog tasks → create reminders
        for i, task in enumerate(backlog_tasks):
            if task.completed or i in matched_backlog_idxs:
                continue

            # Check if we already queued this task (avoid duplicates)
            existing_cmd = (
                session.query(ReminderCommand)
                .filter(
                    ReminderCommand.action == "add",
                    ReminderCommand.status == "pending",
                    ReminderCommand.payload.contains(task.text[:50]),
                )
                .first()
            )
            if existing_cmd:
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
            cmd = ReminderCommand(
                action="add",
                payload=json.dumps(payload),
                status="pending",
            )
            session.add(cmd)
            stats["new_to_reminders"] += 1

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
