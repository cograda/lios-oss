"""Every read tool defaults to the CALLER's own loops (2026-09-06).

The ledger is household-shared by design — assignment is a field on the row,
not row ownership — and every read tool used to return everyone's rows unless
told otherwise. Invisible with one user; the first `/tunetasks` from Sam's
Mac pulled 201 tasks, 142 of them Alex's, because no command template passes
`owner`. The default now lives in the server, so every client — the loops app,
the slash commands, Claude Code itself — is scoped without editing any of them.
`owner="household"` is the explicit widening.

Mutation-checked: with `_scope_owner` returning None for an absent owner, every
`*_default_*` test here fails.
"""

import json

import pytest

from tests.test_tasks_tools import SAMPLE, _call, ledger  # noqa: F401 - fixture re-export

pytestmark = pytest.mark.db


def _add(session, title, *, as_user):
    from app.auth.context import use_user
    from app.integrations.tasks.tools import tasks_add_handler

    with use_user(as_user):
        return _call(tasks_add_handler, session, title=title)["created"]["uid"]


@pytest.fixture
def two_people(db_session, ledger):
    """Alex's lines, Sam's lines, and the two imported (unowned) SAMPLE rows."""
    alex = [_add(db_session, "Wall-mount the office monitor", as_user=1),
            _add(db_session, "Renew the car insurance", as_user=1)]
    sam = [_add(db_session, "Cut the grass", as_user=2)]
    return {"alex": alex, "sam": sam}


def _uids(out):
    return {t["uid"] for t in out["tasks"]}


def _file(ledger, user_id):
    """The `ledger` fixture is user 1's file; its siblings are the others'."""
    return ledger.parent.parent / f"user{user_id}" / ledger.name


@pytest.mark.anyio
async def test_query_default_is_the_callers_own_tasks(db_session, two_people):
    from app.auth.context import use_user
    from app.integrations.tasks.tools import tasks_query_handler

    with use_user(2):
        assert _uids(_call(tasks_query_handler, db_session, limit=500)) == set(two_people["sam"])
    assert _uids(_call(tasks_query_handler, db_session, limit=500)) == set(two_people["alex"])


@pytest.mark.anyio
async def test_query_household_is_the_explicit_widening(db_session, two_people):
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.tools import tasks_query_handler

    everyone = _uids(_call(tasks_query_handler, db_session, owner="household", limit=500))
    assert everyone >= set(two_people["alex"]) | set(two_people["sam"])
    # The imported, unowned SAMPLE rows are in the household view and in nobody's own view.
    unowned = {t.uid for t in db_session.query(Task).filter(Task.owner_id.is_(None)).all()}
    assert unowned and unowned <= everyone
    assert not unowned & _uids(_call(tasks_query_handler, db_session, limit=500))


@pytest.mark.anyio
async def test_query_by_construction_lenses_are_not_emptied_by_the_default(db_session, two_people):
    """A transfer waiting on me is owned by someone else; scoping it to my
    owner_id would return nothing, which is the one way this default could
    break the multi-user lenses."""
    from app.auth.context import use_user
    from app.integrations.tasks.tools import tasks_query_handler, tasks_transfer_handler

    from app.integrations.tasks.tools import tasks_accept_handler

    uid = two_people["alex"][0]
    _call(tasks_transfer_handler, db_session, uid=uid, to_user="sam")
    with use_user(2):
        assert _uids(_call(tasks_query_handler, db_session, pending_for_me=True)) == {uid}
        assert uid not in _uids(_call(tasks_query_handler, db_session, limit=500))
        _call(tasks_accept_handler, db_session, uid=uid)
        # Accepted: now hers, so in her default view.
        assert uid in _uids(_call(tasks_query_handler, db_session, limit=500))
    # And in Alex's "handed" lens even though he no longer owns it.
    assert _uids(_call(tasks_query_handler, db_session, handed_by_me=True)) == {uid}
    assert uid not in _uids(_call(tasks_query_handler, db_session, limit=500))


@pytest.mark.anyio
async def test_review_default_flags_only_the_callers_lines(db_session, ledger):
    from app.auth.context import use_user
    from app.integrations.tasks.tools import tasks_review_handler

    alex = _add(db_session, "Garden", as_user=1)  # bare noun: flagged
    sam = _add(db_session, "Utility", as_user=2)  # bare noun: flagged

    with use_user(2):
        mine = {f["uid"] for f in _call(tasks_review_handler, db_session)["findings"]}
    assert sam in mine and alex not in mine
    household = {f["uid"] for f in _call(tasks_review_handler, db_session, owner="household")["findings"]}
    assert {alex, sam} <= household


@pytest.mark.anyio
async def test_block_default_is_the_callers_blocked_loops(db_session, two_people):
    from app.auth.context import use_user
    from app.integrations.tasks.tools import tasks_block_handler

    a, b = two_people["alex"]
    _call(tasks_block_handler, db_session, action="add", uid=a, blocked_by=b)

    assert set(_call(tasks_block_handler, db_session)["blocked"]) == {a}
    with use_user(2):
        assert _call(tasks_block_handler, db_session)["blocked"] == {}
        assert set(_call(tasks_block_handler, db_session, owner="household")["blocked"]) == {a}


def test_duplicates_default_is_the_callers_pairs(ledger, db_session, monkeypatch):
    from app.auth.context import use_user
    from app.integrations.tasks import dupes

    monkeypatch.setattr("app.services.embedding.EmbeddingService.near_duplicates", lambda *a, **k: [])
    a = _add(db_session, "Fit a new lock on the utility-room gate", as_user=1)
    b = _add(db_session, "Fit the new lock to the gate by the utility room", as_user=1)
    key = lambda p: tuple(sorted((p["a"], p["b"])))  # noqa: E731

    assert [key(p) for p in _call(dupes.tasks_duplicates_handler, db_session)["pairs"]] == [tuple(sorted((a, b)))]
    with use_user(2):
        assert _call(dupes.tasks_duplicates_handler, db_session)["pairs"] == []
        household = _call(dupes.tasks_duplicates_handler, db_session, owner="household")
        assert [key(p) for p in household["pairs"]] == [tuple(sorted((a, b)))]


@pytest.mark.anyio
async def test_history_default_is_the_callers_own_tasks_events(db_session, two_people):
    """The two reads PR #120 missed. The feed is the caller's; a uid is an
    explicit ask and reads whoever's task it is; `household` is everyone's."""
    from app.auth.context import use_user
    from app.integrations.tasks.loops import tasks_history_handler
    from app.integrations.tasks.tools import tasks_update_handler

    alex_uid, sam_uid = two_people["alex"][0], two_people["sam"][0]
    _call(tasks_update_handler, db_session, uid=alex_uid, priority="low")
    with use_user(2):
        _call(tasks_update_handler, db_session, uid=sam_uid, priority="low")

    def uids(out):
        return {e["uid"] for e in out["events"] if e["uid"]}

    assert uids(_call(tasks_history_handler, db_session)) == set(two_people["alex"])
    with use_user(2):
        assert uids(_call(tasks_history_handler, db_session)) == set(two_people["sam"])
        # An explicit uid is readable whoever owns it — the ledger is shared.
        assert uids(_call(tasks_history_handler, db_session, uid=alex_uid)) == {alex_uid}
    assert uids(_call(tasks_history_handler, db_session, owner="household")) >= {alex_uid, sam_uid}


def test_similar_default_candidates_are_the_callers_and_the_unowned(db_session, two_people, monkeypatch):
    from app.auth.context import use_user
    from app.integrations.tasks import dupes

    monkeypatch.setattr("app.services.embedding.EmbeddingService.similar_to", lambda *a, **k: [])
    alex = _add(db_session, "Cut the grass at the front", as_user=1)
    sam = two_people["sam"][0]  # "Cut the grass"
    unowned = _add(db_session, "Cut the grass out the back", as_user=1)
    from app.integrations.tasks.models import Task
    db_session.query(Task).filter(Task.uid == unowned).update({Task.owner_id: None})
    db_session.commit()

    def similar(**kw):
        return {r["uid"] for r in _call(dupes.tasks_similar_handler, db_session, **kw)["similar"]}

    with use_user(2):
        assert similar(uid=sam) == {unowned}
        assert similar(uid=sam, owner="household") == {unowned, alex}
    # The subject task itself is looked up unscoped: a uid is an explicit ask.
    assert similar(uid=sam) == {unowned, alex}


def test_the_two_late_schemas_say_the_default_is_the_caller():
    from app.integrations.tasks import dupes, loops

    specs = {t["name"]: t for t in dupes.dupes_tools() + loops.loops_tools()}
    for name in ("tasks_history", "tasks_similar"):
        desc = json.dumps(specs[name]["inputSchema"]["properties"]["owner"])
        assert "Defaults to you" in desc and "household" in desc, name


# ── a hand-over refreshes BOTH files ────────────────────────────────────────


@pytest.mark.anyio
async def test_a_transfer_re_renders_the_other_partys_vault_too(db_session, ledger):
    """`_render` only refreshes the caller's file, so the receiving side's
    `Task Backlog.md` said nothing about the hand-over until the 15-minute
    tick. Now both files move with the write."""
    from app.auth.context import use_user
    from app.integrations.tasks.tools import tasks_accept_handler, tasks_transfer_handler

    uid = _add(db_session, "Descale the kettle", as_user=1)
    hers = _file(ledger, 2)
    assert not hers.exists()
    _call(tasks_transfer_handler, db_session, uid=uid, to_user="sam")
    # Her vault was rendered by HIS write. The row is still his until she
    # accepts, so it is not in her file yet — the point is that the file moved.
    assert hers.exists() and "Descale the kettle" not in hers.read_text()

    with use_user(2):
        _call(tasks_accept_handler, db_session, uid=uid)
    # Now hers; and HIS file was re-rendered by HER accept, so it has gone from it.
    assert "Descale the kettle" in hers.read_text()
    assert "Descale the kettle" not in ledger.read_text()


def test_unknown_owner_still_names_the_real_users(db_session, ledger):
    from app.integrations.tasks.tools import tasks_query_handler

    with pytest.raises(ValueError, match="Unknown user: nobody"):
        tasks_query_handler(db_session, {"owner": "nobody"})


# ── the vault file is one person's view ─────────────────────────────────────


@pytest.mark.anyio
async def test_each_vault_renders_its_owners_loops_plus_the_unowned(db_session, two_people, ledger):
    """Sam's `Task Backlog.md` was a copy of the household ledger. The file
    lands in HER vault, so it carries her rows and the unowned ones — never
    Alex's. Alex's file is the mirror image."""
    from app.integrations.tasks import render as render_mod

    render_mod.write_backlog_note(db_session, user_id=2)
    hers = _file(ledger, 2).read_text()
    assert "Cut the grass" in hers
    assert "Wall mount the TV" in hers  # imported, unowned: nobody's, so visible to whoever looks
    assert "Wall-mount the office monitor" not in hers
    assert "Renew the car insurance" not in hers

    render_mod.write_backlog_note(db_session, user_id=1)
    his = ledger.read_text()
    assert "Wall-mount the office monitor" in his and "Wall mount the TV" in his
    assert "Cut the grass" not in his


@pytest.mark.anyio
async def test_a_request_scoped_render_uses_the_bound_user(db_session, two_people, ledger):
    from app.auth.context import use_user
    from app.integrations.tasks import render as render_mod

    with use_user(2):
        render_mod.write_backlog_note(db_session)
    assert "Cut the grass" in _file(ledger, 2).read_text()
    assert "Renew the car insurance" not in _file(ledger, 2).read_text()


@pytest.mark.anyio
async def test_the_unscoped_render_is_still_the_whole_ledger(db_session, two_people):
    """`render_backlog(session)` with no owner is the round-trip the import
    tests prove lossless; it must keep meaning everything."""
    from app.integrations.tasks.render import render_backlog

    out = render_backlog(db_session)
    assert "Cut the grass" in out and "Renew the car insurance" in out and "Wall mount the TV" in out


def test_the_rendered_schema_says_the_default_is_the_caller():
    """The schema is what a client reads; a default that changed in the
    handler but not the description is a trap for the next template."""
    from app.integrations.tasks import tools

    specs = {}
    for t in tools.mcp_tools():
        specs[t["name"]] = t
    for name in ("tasks_query", "tasks_review", "tasks_block", "tasks_duplicates"):  # + history/similar below
        desc = json.dumps(specs[name]["inputSchema"]["properties"]["owner"])
        assert "Defaults to you" in desc and "household" in desc, name
