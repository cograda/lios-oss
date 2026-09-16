"""unit-tier tests for `/api/v1/reminders/push`'s reactive inlet trigger.

Replaces the reactive `backlog_sync` call this route used to make (deleted
2026-09-15 along with the rest of that module — see
`app/integrations/apple_reminders/README.md` and `server/CLAUDE.md`'s Known
Issues entry). The route now fires `reminders_inlet.tick_once` in the same
fire-and-forget background thread whenever the push reports a change, so a
reminder completed on the phone reaches the ledger within seconds rather
than waiting for the next 15-minute `reminders_inlet_tick`.

`_trigger_reminders_inlet` spawns a real daemon thread — these tests replace
`threading.Thread` with a stand-in that runs the target function inline and
synchronously, so the assertions don't need to sleep-and-poll for a
background thread to finish.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


class _InlineThread:
    """Drop-in for `threading.Thread(target=..., ...).start()` that just
    calls `target()` synchronously in the calling thread."""

    def __init__(self, target=None, name=None, daemon=None):
        self._target = target

    def start(self) -> None:
        self._target()


@pytest.fixture
def push(monkeypatch):
    """Call `reminders_push` directly against a stubbed DB/sync layer,
    running `_trigger_reminders_inlet`'s thread inline so it's observable."""
    from app.api import v1

    # `_trigger_reminders_inlet` does `import threading` inside its own
    # function body, which binds to the real module object — patch that
    # module directly rather than something imported into v1's namespace.
    monkeypatch.setattr("threading.Thread", _InlineThread)

    db = MagicMock()
    db.session.return_value.__enter__ = lambda s: MagicMock()
    db.session.return_value.__exit__ = lambda s, *a: False
    monkeypatch.setattr(v1, "get_db", lambda: db)

    sync_result = {"count": 1, "newly_completed": 0, "newly_added": 0, "edited": 0}
    monkeypatch.setattr(
        "app.integrations.apple_reminders.sync.sync_from_push",
        lambda reminders_data, session, user_id: sync_result,
    )

    tick_calls: list[int] = []
    monkeypatch.setattr(
        "app.integrations.tasks.reminders_inlet.tick_once",
        lambda session: (tick_calls.append(1) or {"captured": [], "pushed": [], "completed": []}),
    )

    user = MagicMock()
    user.id = 1
    user.name = "alex"

    def _call(*, newly_completed=0, newly_added=0, edited=0):
        sync_result.update(
            newly_completed=newly_completed, newly_added=newly_added, edited=edited,
        )
        payload = v1.RemindersPushRequest(reminders=[])
        return v1.reminders_push(payload, user=user)

    _call.tick_calls = tick_calls
    return _call


class TestReactiveInletTrigger:
    def test_push_with_changes_triggers_the_inlet(self, push):
        push(newly_added=1)
        assert push.tick_calls == [1]

    def test_push_with_no_changes_does_not_trigger_the_inlet(self, push):
        push(newly_completed=0, newly_added=0, edited=0)
        assert push.tick_calls == []
