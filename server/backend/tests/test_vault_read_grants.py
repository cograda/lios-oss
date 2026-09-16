"""Cross-user vault read grants.

The tests that matter here are the *refusals*. A grant system that lets the
right call through is easy; one that also blocks every wrong call is the
feature. In particular two failure shapes are asserted explicitly because
both would be invisible in production:

  - a refusal must **raise**, never fall back to the caller's own scope. A
    silent fallback returns an empty result set that is indistinguishable
    from a correct "nothing found".
  - an unannotated or write tool must be refused **even when a grant exists**,
    because the grant is scoped to reading.
"""

from __future__ import annotations

import pytest

from app.errors import PermanentError
from app.services.vault_grants import (
    AS_USER_ARG,
    _is_read_only,
    resolve_effective_user_id,
)


READ_ONLY = {"readOnlyHint": True}
WRITE = {"readOnlyHint": False}


# ---------------------------------------------------------------------------
# read-only detection — fails closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("annotations", [
    None,
    {},
    {"readOnlyHint": False},
    {"readOnlyHint": None},
    {"title": "Search"},
    object(),
])
def test_is_read_only_fails_closed(annotations):
    """Absence of a claim is not the claim."""
    assert _is_read_only(annotations) is False


def test_is_read_only_accepts_both_spellings():
    # Built tools carry the MCP wire form; the dataclass field name is
    # accepted too so a caller passing the object's own attribute names
    # doesn't fail closed for the wrong reason.
    assert _is_read_only({"readOnlyHint": True}) is True
    assert _is_read_only({"read_only_hint": True}) is True

    class _Obj:
        read_only_hint = True

    assert _is_read_only(_Obj()) is True


# ---------------------------------------------------------------------------
# the common path touches no database at all
# ---------------------------------------------------------------------------

class _ExplodingSession:
    def execute(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("no as_user given — resolution must not query")


def test_absent_as_user_is_a_pure_passthrough():
    assert resolve_effective_user_id(
        _ExplodingSession(),
        caller_id=7, caller_name="agent", arguments={"query": "x"},
        tool_name="vault_search", integration_name="obsidian",
        annotations=READ_ONLY,
    ) == 7


def test_naming_yourself_is_allowed_and_queries_nothing():
    """A caller that always passes as_user shouldn't break when it names itself."""
    assert resolve_effective_user_id(
        _ExplodingSession(),
        caller_id=7, caller_name="Agent", arguments={AS_USER_ARG: "agent"},
        tool_name="vault_search", integration_name="obsidian",
        annotations=READ_ONLY,
    ) == 7


def test_blank_as_user_is_refused():
    with pytest.raises(PermanentError):
        resolve_effective_user_id(
            _ExplodingSession(),
            caller_id=7, caller_name="agent", arguments={AS_USER_ARG: "   "},
            tool_name="vault_search", integration_name="obsidian",
            annotations=READ_ONLY,
        )


# ---------------------------------------------------------------------------
# scope and read-only are checked BEFORE any grant lookup
# ---------------------------------------------------------------------------

def test_non_grantable_scope_refused_without_lookup():
    """gmail_search is read-only; it is still not in scope for a vault grant."""
    with pytest.raises(PermanentError, match="not a grantable scope"):
        resolve_effective_user_id(
            _ExplodingSession(),
            caller_id=7, caller_name="agent", arguments={AS_USER_ARG: "alex"},
            tool_name="gmail_search", integration_name="gmail",
            annotations=READ_ONLY,
        )


def test_write_tool_refused_without_lookup():
    with pytest.raises(PermanentError, match="read-only"):
        resolve_effective_user_id(
            _ExplodingSession(),
            caller_id=7, caller_name="agent", arguments={AS_USER_ARG: "alex"},
            tool_name="vault_transfer", integration_name="obsidian",
            annotations=WRITE,
        )


def test_unannotated_tool_refused():
    with pytest.raises(PermanentError, match="read-only"):
        resolve_effective_user_id(
            _ExplodingSession(),
            caller_id=7, caller_name="agent", arguments={AS_USER_ARG: "alex"},
            tool_name="vault_search", integration_name="obsidian",
            annotations=None,
        )


# ---------------------------------------------------------------------------
# schema advertisement is derived from the same two facts
# ---------------------------------------------------------------------------

def test_schema_advertises_as_user_only_where_it_is_accepted():
    from app.mcp.server import _with_as_user

    schema = {"type": "object", "properties": {"query": {"type": "string"}}}

    granted = _with_as_user(schema, integration_name="obsidian", annotations=READ_ONLY)
    assert AS_USER_ARG in granted["properties"]
    # original is not mutated — registration reuses these dicts
    assert AS_USER_ARG not in schema["properties"]

    assert _with_as_user(
        schema, integration_name="gmail", annotations=READ_ONLY
    ) == schema
    assert _with_as_user(
        schema, integration_name="obsidian", annotations=WRITE
    ) == schema


def test_schema_injection_is_idempotent():
    from app.mcp.server import _with_as_user

    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    once = _with_as_user(schema, integration_name="obsidian", annotations=READ_ONLY)
    twice = _with_as_user(once, integration_name="obsidian", annotations=READ_ONLY)
    assert once == twice


# ---------------------------------------------------------------------------
# grant lookup against a real database
# ---------------------------------------------------------------------------

@pytest.mark.db
class TestGrantLookup:
    def _agent(self, session):
        from app.models.users import User

        user = User(name="agent", display_name="lios Agent", is_active=True)
        session.add(user)
        session.flush()
        return user

    def test_no_grant_is_refused(self, db_session):
        agent = self._agent(db_session)
        with pytest.raises(PermanentError, match="no grant"):
            resolve_effective_user_id(
                db_session,
                caller_id=agent.id, caller_name="agent",
                arguments={AS_USER_ARG: "alex"},
                tool_name="vault_search", integration_name="obsidian",
                annotations=READ_ONLY,
            )

    def test_grant_swaps_the_effective_user(self, db_session):
        from app.services.vault_grants import grant_read

        agent = self._agent(db_session)
        grant_read(
            db_session, grantee="agent", owner="alex",
            scope="obsidian", reason="lios agent host",
        )
        assert resolve_effective_user_id(
            db_session,
            caller_id=agent.id, caller_name="agent",
            arguments={AS_USER_ARG: "alex"},
            tool_name="vault_search", integration_name="obsidian",
            annotations=READ_ONLY,
        ) == 1  # seeded alex

    def test_grant_is_one_directional(self, db_session):
        """agent may read alex; alex gains nothing over agent."""
        from app.services.vault_grants import grant_read

        agent = self._agent(db_session)
        grant_read(
            db_session, grantee="agent", owner="alex",
            scope="obsidian", reason="lios agent host",
        )
        with pytest.raises(PermanentError, match="no grant"):
            resolve_effective_user_id(
                db_session,
                caller_id=1, caller_name="alex",
                arguments={AS_USER_ARG: "agent"},
                tool_name="vault_search", integration_name="obsidian",
                annotations=READ_ONLY,
            )

    def test_grant_does_not_extend_to_a_third_user(self, db_session):
        from app.services.vault_grants import grant_read

        agent = self._agent(db_session)
        grant_read(
            db_session, grantee="agent", owner="alex",
            scope="obsidian", reason="lios agent host",
        )
        with pytest.raises(PermanentError, match="no grant"):
            resolve_effective_user_id(
                db_session,
                caller_id=agent.id, caller_name="agent",
                arguments={AS_USER_ARG: "sam"},
                tool_name="vault_search", integration_name="obsidian",
                annotations=READ_ONLY,
            )

    def test_unknown_user_is_indistinguishable_from_no_grant(self, db_session):
        """Refusal text must not let a caller enumerate usernames."""
        agent = self._agent(db_session)
        with pytest.raises(PermanentError) as unknown:
            resolve_effective_user_id(
                db_session, caller_id=agent.id, caller_name="agent",
                arguments={AS_USER_ARG: "nobody"},
                tool_name="vault_search", integration_name="obsidian",
                annotations=READ_ONLY,
            )
        with pytest.raises(PermanentError) as ungranted:
            resolve_effective_user_id(
                db_session, caller_id=agent.id, caller_name="agent",
                arguments={AS_USER_ARG: "sam"},
                tool_name="vault_search", integration_name="obsidian",
                annotations=READ_ONLY,
            )
        assert str(unknown.value).replace("nobody", "X") == \
            str(ungranted.value).replace("sam", "X")

    def test_grant_read_requires_a_reason(self, db_session):
        from app.services.vault_grants import grant_read

        self._agent(db_session)
        with pytest.raises(ValueError, match="reason is required"):
            grant_read(
                db_session, grantee="agent", owner="alex",
                scope="obsidian", reason="   ",
            )

    def test_grant_read_rejects_a_non_grantable_scope(self, db_session):
        from app.services.vault_grants import grant_read

        self._agent(db_session)
        with pytest.raises(ValueError, match="not grantable"):
            grant_read(
                db_session, grantee="agent", owner="alex",
                scope="gmail", reason="no",
            )

    def test_grant_read_is_idempotent(self, db_session):
        from app.services.vault_grants import grant_read

        self._agent(db_session)
        a = grant_read(db_session, grantee="agent", owner="alex",
                       scope="obsidian", reason="first")
        b = grant_read(db_session, grantee="agent", owner="alex",
                       scope="obsidian", reason="second")
        assert a.id == b.id
        assert a.reason == "first"  # re-granting does not rewrite the record
