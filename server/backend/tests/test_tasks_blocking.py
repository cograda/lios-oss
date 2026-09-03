"""Blocking relationships between tasks.

`TaskLink` carried a `blocked_by` predicate from the day the schema shipped
and nothing ever wrote one. These tests are the behaviour that makes the edge
worth recording rather than merely storable.
"""

import pytest

from app.integrations.tasks.blocking import (
    BlockingError, add_blocker, most_blocking, open_blockers, remove_blocker,
)
from app.integrations.tasks.models import Task

pytestmark = pytest.mark.db


def _task(session, uid, title, *, status="next", priority=None):
    task = Task(uid=uid, title=title, status=status, priority=priority)
    session.add(task)
    session.flush()
    return task


@pytest.mark.anyio
async def test_a_blocker_is_recorded_and_read_back(db_session):
    _task(db_session, "TASK-1", "Talk to the builder")
    _task(db_session, "TASK-2", "Compare the quotes")
    add_blocker(db_session, "TASK-2", "TASK-1")
    assert open_blockers(db_session) == {"TASK-2": ["TASK-1"]}


@pytest.mark.anyio
async def test_completing_the_blocker_clears_the_dependent(db_session):
    """Nobody has to remember to untick anything.

    The alternative is a `#blocked` tag that outlives its cause, and a backlog
    full of tasks marked blocked by work finished weeks ago teaches you that
    the marker means nothing.
    """
    blocker = _task(db_session, "TASK-1", "Talk to the builder")
    _task(db_session, "TASK-2", "Compare the quotes")
    add_blocker(db_session, "TASK-2", "TASK-1")

    blocker.status = "done"
    db_session.flush()

    assert open_blockers(db_session) == {}


@pytest.mark.anyio
async def test_a_task_cannot_block_itself(db_session):
    _task(db_session, "TASK-1", "Do the thing")
    with pytest.raises(BlockingError, match="cannot block itself"):
        add_blocker(db_session, "TASK-1", "TASK-1")


@pytest.mark.anyio
async def test_a_direct_cycle_is_refused(db_session):
    _task(db_session, "TASK-1", "A")
    _task(db_session, "TASK-2", "B")
    add_blocker(db_session, "TASK-2", "TASK-1")
    with pytest.raises(BlockingError, match="cycle"):
        add_blocker(db_session, "TASK-1", "TASK-2")


@pytest.mark.anyio
async def test_a_cycle_through_a_third_task_is_refused(db_session):
    """The one a direct-comparison check misses. A→B→C→A is still three
    tasks none of which can ever start."""
    for uid in ("TASK-1", "TASK-2", "TASK-3"):
        _task(db_session, uid, uid)
    add_blocker(db_session, "TASK-2", "TASK-1")
    add_blocker(db_session, "TASK-3", "TASK-2")
    with pytest.raises(BlockingError, match="cycle"):
        add_blocker(db_session, "TASK-1", "TASK-3")


@pytest.mark.anyio
async def test_adding_the_same_blocker_twice_is_not_an_error(db_session):
    _task(db_session, "TASK-1", "A")
    _task(db_session, "TASK-2", "B")
    first = add_blocker(db_session, "TASK-2", "TASK-1")
    assert add_blocker(db_session, "TASK-2", "TASK-1").id == first.id


@pytest.mark.anyio
async def test_removing_a_blocker(db_session):
    _task(db_session, "TASK-1", "A")
    _task(db_session, "TASK-2", "B")
    add_blocker(db_session, "TASK-2", "TASK-1")
    assert remove_blocker(db_session, "TASK-2", "TASK-1") == 1
    assert open_blockers(db_session) == {}


@pytest.mark.anyio
async def test_one_blocker_gating_several_tasks_ranks_first(db_session):
    _task(db_session, "TASK-1", "Decide the layout", priority="low")
    for uid in ("TASK-2", "TASK-3", "TASK-4"):
        _task(db_session, uid, uid, priority="high")
        add_blocker(db_session, uid, "TASK-1")
    _task(db_session, "TASK-5", "Something else", priority="low")
    _task(db_session, "TASK-6", "Waits on 5", priority="low")
    add_blocker(db_session, "TASK-6", "TASK-5")

    ranked = most_blocking(db_session)
    assert ranked[0]["uid"] == "TASK-1"
    assert ranked[0]["blocking_count"] == 3


@pytest.mark.anyio
async def test_a_low_priority_blocker_on_urgent_work_is_marked(db_session):
    """The finding worth surfacing. A low-priority task holding up urgent
    work says the priority is wrong at least as loudly as it says the link
    was missing."""
    _task(db_session, "TASK-1", "Decide the layout", priority="low")
    _task(db_session, "TASK-2", "Order the units", priority="highest")
    add_blocker(db_session, "TASK-2", "TASK-1")

    assert most_blocking(db_session)[0]["priority_inverted"] is True


@pytest.mark.anyio
async def test_a_higher_priority_blocker_is_not_marked_inverted(db_session):
    _task(db_session, "TASK-1", "Urgent groundwork", priority="highest")
    _task(db_session, "TASK-2", "Later thing", priority="low")
    add_blocker(db_session, "TASK-2", "TASK-1")

    assert most_blocking(db_session)[0]["priority_inverted"] is False


@pytest.mark.anyio
async def test_an_unknown_uid_says_so(db_session):
    _task(db_session, "TASK-1", "A")
    with pytest.raises(BlockingError, match="Unknown task uid"):
        add_blocker(db_session, "TASK-1", "TASK-404")
