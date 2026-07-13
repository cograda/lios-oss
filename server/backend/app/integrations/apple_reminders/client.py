"""Apple Reminders push-based sync.

The Mac agent reads Reminders via osascript/EventKit and POSTs them here.
For writes (add/complete), the server queues commands that the Mac agent
picks up and executes locally.
"""

from dataclasses import dataclass, asdict
from datetime import datetime


@dataclass
class ReminderItem:
    """A reminder as received from the Mac agent."""

    uid: str
    list_name: str
    summary: str
    notes: str | None = None
    due_date: datetime | None = None
    priority: int = 0  # 0=none, 1=high, 5=medium, 9=low
    completed: bool = False
    completed_date: datetime | None = None
    flagged: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["due_date"]:
            d["due_date"] = d["due_date"].isoformat()
        if d["completed_date"]:
            d["completed_date"] = d["completed_date"].isoformat()
        return d


@dataclass
class ReminderCommand:
    """A queued command for the Mac agent to execute."""

    action: str  # 'add' or 'complete'
    payload: dict  # action-specific data

    def to_dict(self) -> dict:
        return asdict(self)
