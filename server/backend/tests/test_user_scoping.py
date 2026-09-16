"""User-scoping enforcement suite (db tier).

Converts per-user scoping from convention to enforcement:

  1. Parametrized over EVERY model carrying UserOwnedMixin — new models
     join the parametrization automatically via the SQLAlchemy registry.
     Each is exercised through the production DSL paths (ListTool +
     SearchTool handlers, real SQL) as user 1 with user 2's data seeded,
     AND as user 2 with user 1's data seeded (N5 — the backlog's own
     phrasing, "every tool run as Sam surfaces none of Alex's ids", is
     the direction that was previously untested: the suite ran user 1
     against user 2's canary but never the reverse).
  2. A meta-test pins the classification: any model with a user_id column
     must be UserOwnedMixin or on the explicit nullable/admin allowlist —
     and `test_meta_test_flags_new_unscoped_table` is a permanent self-test
     of that guard: it defines a throwaway unscoped model in-test and
     proves the guard actually flags it, rather than trusting that it
     would.
  3. A canary sweep: seed BOTH users' rows in every user-owned table with a
     marker string, then invoke every registerable tool (not just
     read-only ones — N5 extended this to catch a write tool that echoes
     back rows, e.g. a bulk-update or transfer response) as one user and
     assert the other user's marker never appears, in both directions.
  4. The household-shared allowlist (`app.privacy.HOUSEHOLD_SHARED_TABLES`)
     is asserted against the live schema — every entry names a real,
     currently-existing table, and none of them are secretly UserOwnedMixin
     or otherwise per-user. This is the "shared by design, not silence"
     half of N5: `system_what_lios_sees` imports this exact dict, so what
     Sam is told is shared is what the test also treats as shared.

EmbeddingService.search scoping (the NULL = household-shared pattern)
is covered in test_embedding_pipeline.py.
"""

import gc
import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Mapped, mapped_column

import app.models  # noqa: F401 — register every model on Base.metadata
from app.auth.context import use_user
from app.mixins import UserOwnedMixin
from app.privacy import (
    HOUSEHOLD_SHARED_TABLES,
    NULLABLE_OR_ADMIN_USER_ID,
    USER_COLUMNS,
    user_columns_for,
    user_owned_models,
)

pytestmark = pytest.mark.db

U1_MARKER = "SCOPETEST-OWN-DATA"
U2_MARKER = "LEAK-CANARY-U2"
# Marker for shared-table FK parents seeded only to satisfy a NOT NULL
# constraint (e.g. SnagSourceMessage.snag_id -> snags, MessageAttachment.
# historical_doc_id -> historical_documents, CoffeeBrew.coffee_id ->
# coffees) — deliberately NEITHER user's marker. N5 found that seeding
# those parents with U1_MARKER made every tool that legitimately surfaces
# household-shared data (snag_list, corpus_stats, coffee_search/_current,
# and system_daily_brief which aggregates them) fail the leak check when
# called as user 2: the "leak" was real text, but it was sitting in a
# HOUSEHOLD-SHARED row, visible to both users by design (see
# app.privacy.HOUSEHOLD_SHARED_TABLES) — not a per-user leak at all. A
# neutral marker for shared parents keeps that content out of both
# U1_MARKER and U2_MARKER's way, so the sweep can no longer confuse
# "shared by design" with "leaked".
SHARED_PARENT_MARKER = "SCOPETEST-SHARED-PARENT"


def _value_for(col, marker: str):
    pytype = col.type.python_type
    if pytype is str:
        value = f"{marker}-{col.name}"
        length = getattr(col.type, "length", None)
        return value[:length] if length else value
    if pytype is int:
        return 1
    if pytype is float:
        return 1.0
    if pytype is bool:
        # is_active-style flags: False would hide the row from the very
        # queries we want to prove can't see it. Keep rows "live".
        return True
    if pytype is datetime:
        return datetime.now(timezone.utc)
    if pytype is date:
        return date.today()
    if pytype is dict:
        return {}
    if pytype is list:
        return []
    raise NotImplementedError(
        f"no generic test value for column {col.name} ({col.type})"
    )


def make_row(model: type, user_id: int, marker: str, fk_overrides: dict | None = None):
    """Build a model instance with required columns filled generically.

    Every String/Text column (required or not) carries the marker so the
    canary sweep can detect a leak through any serialized field. Unique
    columns stay unique because the marker differs per user.

    Per-user column(s) (`app.privacy.user_columns_for` — plain `user_id` for
    a UserOwnedMixin table, or a table-specific tuple like
    `vault_read_grants`'s `grantee_user_id`/`owner_user_id`) are ALL set to
    `user_id` — so a table with two such columns gets a row that is (in the
    generic seeding sense) "this user's" via either one, which is exactly
    what the OR-scoped read path (`scoped_query`) is built to recognise.

    `fk_overrides` (column name -> id) supplies values for required foreign
    keys that point somewhere other than `users` — see `_ensure_fk_parents`.
    Without it, a generic int filler (1) would only work by accident.
    """
    mapper = sa_inspect(model)
    user_cols = set(user_columns_for(model))
    kwargs = {}
    for col in mapper.columns:
        # A per-user column checked BEFORE the primary-key skip: most tables
        # have a separate surrogate `id` PK and a plain `user_id` column, but
        # `intake_markers` doubles `user_id` as its own PK (one marker per
        # caller, ever — no surrogate id needed). Checking `primary_key`
        # first would skip it entirely and insert a NULL into a NOT NULL PK.
        if col.name in user_cols:
            kwargs[col.name] = user_id
            continue
        if col.primary_key:
            continue
        if fk_overrides and col.name in fk_overrides:
            kwargs[col.name] = fk_overrides[col.name]
            continue
        if isinstance(col.type, (String, Text)):
            kwargs[col.name] = _value_for(col, marker)
            continue
        if col.nullable or col.default is not None or col.server_default is not None:
            continue
        kwargs[col.name] = _value_for(col, marker)
    return model(**kwargs)


def _minimal_row(model: type, marker: str):
    """Build the smallest valid instance of a (usually shared, non-user-owned)
    parent model — used to satisfy a required FK before seeding a user-owned
    child row. Same generic filler as `make_row`, minus the user_id notion."""
    mapper = sa_inspect(model)
    kwargs = {}
    for col in mapper.columns:
        if col.primary_key:
            continue
        if col.nullable or col.default is not None or col.server_default is not None:
            continue
        kwargs[col.name] = _value_for(col, marker)
    return model(**kwargs)


def _ensure_fk_parents(session, model: type, marker: str) -> dict[str, int]:
    """For every non-`user_id` foreign key column on `model`, seed one
    minimal parent row (shared across both test users) and return
    {column_name: parent_id}.

    Some user-owned tables carry a required FK to a *shared* table (e.g.
    `snag_source_messages.snag_id` -> `snags`, which has no owner of its
    own). Without this, the generic scoping sweep fails on a FK violation
    rather than actually exercising per-user scoping.
    """
    from coglib import Base

    mapper = sa_inspect(model)
    fk_ids: dict[str, int] = {}
    for col in mapper.columns:
        if col.name == "user_id":
            continue
        for fk in col.foreign_keys:
            target_table = fk.column.table
            if target_table.name == "users":
                continue
            target_cls = next(
                (m.class_ for m in Base.registry.mappers if m.local_table is target_table),
                None,
            )
            if target_cls is None:
                continue
            parent = _minimal_row(target_cls, marker)
            session.add(parent)
            session.flush()
            fk_ids[col.name] = parent.id
    return fk_ids


def _timestamp_col(model: type) -> str:
    mapper = sa_inspect(model)
    dt_cols = [c.name for c in mapper.columns if isinstance(c.type, DateTime)]
    for preferred in ("created_at", "received_at", "logged_at", "synced_at"):
        if preferred in dt_cols:
            return preferred
    assert dt_cols, f"{model.__name__} has no DateTime column for ListTool probe"
    return dt_cols[0]


def _seed_both_users(session, model: type) -> None:
    fk_overrides = _ensure_fk_parents(session, model, SHARED_PARENT_MARKER)
    session.add(make_row(model, 1, U1_MARKER, fk_overrides))
    session.add(make_row(model, 2, U2_MARKER, fk_overrides))
    session.commit()


def _row_dump(row) -> dict:
    mapper = sa_inspect(type(row))
    return {c.name: str(getattr(row, c.name)) for c in mapper.columns}


# ---------------------------------------------------------------------------
# 1. DSL enforcement, parametrized over every user-owned model AND direction
# ---------------------------------------------------------------------------

# (caller_uid, own_marker, other_marker) — both directions. The backlog's own
# phrasing ("every tool run as Sam surfaces none of Alex's ids") names the
# 2-vs-1 direction specifically; the suite previously only ran 1-vs-2.
_DIRECTIONS = [
    pytest.param(1, U1_MARKER, U2_MARKER, id="as_alex_vs_sam"),
    pytest.param(2, U2_MARKER, U1_MARKER, id="as_sam_vs_alex"),
]


@pytest.mark.parametrize("model", user_owned_models(), ids=lambda m: m.__tablename__)
@pytest.mark.parametrize("caller_uid, own_marker, other_marker", _DIRECTIONS)
def test_list_tool_scopes(db_session, model, caller_uid, own_marker, other_marker):
    """ListTool as either user must return that user's row and never the
    other's, in both directions."""
    from app.tools.list_tool import ListTool

    _seed_both_users(db_session, model)
    handler = ListTool(
        name="probe_list",
        description="scoping probe",
        model=model,
        timestamp_col=_timestamp_col(model),
        to_dict=_row_dump,
    ).build()["handler"]

    with use_user(caller_uid):
        out = handler(db_session, {"limit": 100})

    assert other_marker not in out, f"{model.__tablename__} leaked the other user's data (caller={caller_uid})"
    # `intake_markers` (and any future table shaped the same way) carries no
    # String/Text column at all — `user_id` doubles as its own PK, so there
    # is no marker text for "own data" to show up as. The caller's own id in
    # the output is the equivalent signal for a table with no string column;
    # everything else still proves ownership via the seeded marker text.
    mapper = sa_inspect(model)
    has_text_column = any(isinstance(c.type, (String, Text)) for c in mapper.columns)
    if has_text_column:
        assert own_marker in out, f"{model.__tablename__} probe returned no own data (caller={caller_uid})"
    else:
        assert str(caller_uid) in out, f"{model.__tablename__} probe returned no own data (caller={caller_uid})"


@pytest.mark.parametrize("model", user_owned_models(), ids=lambda m: m.__tablename__)
@pytest.mark.parametrize("caller_uid, own_marker, other_marker", _DIRECTIONS)
def test_search_tool_scopes(db_session, model, caller_uid, own_marker, other_marker):
    """SearchTool matching the OTHER user's marker text must still stay
    user-scoped, in both directions."""
    from app.tools.search_tool import SearchTool

    mapper = sa_inspect(model)
    text_cols = [
        c.name for c in mapper.columns
        if isinstance(c.type, (String, Text)) and c.name != "user_id"
    ]
    if not text_cols:
        pytest.skip(f"{model.__tablename__} has no text columns to search")

    _seed_both_users(db_session, model)
    handler = SearchTool(
        name="probe_search",
        description="scoping probe",
        model=model,
        search_columns=text_cols,
        timestamp_col=_timestamp_col(model),
        to_dict=_row_dump,
    ).build()["handler"]

    with use_user(caller_uid):
        # Search for the OTHER user's marker — the worst case.
        out = handler(db_session, {"query": other_marker, "limit": 100})

    assert other_marker not in out, f"{model.__tablename__} leaked via search (caller={caller_uid})"


def test_list_tool_refuses_unbound_user(db_session):
    """No bound user → loud RuntimeError, never an unscoped query."""
    from app.auth.context import _current_user_id
    from app.models.clients import ClientToken
    from app.tools.list_tool import ListTool

    _seed_both_users(db_session, ClientToken)
    handler = ListTool(
        name="probe_unbound",
        description="scoping probe",
        model=ClientToken,
        timestamp_col="created_at",
        to_dict=_row_dump,
    ).build()["handler"]

    token = _current_user_id.set(0)  # simulate a forgotten use_user()
    try:
        with pytest.raises(RuntimeError, match="use_user"):
            handler(db_session, {})
    finally:
        _current_user_id.reset(token)


# ---------------------------------------------------------------------------
# 2. Meta-test: classification is exhaustive
# ---------------------------------------------------------------------------

def _misclassified_user_id_models() -> list[str]:
    """Every table with a `user_id` column that is neither UserOwnedMixin
    nor on the explicit nullable/admin allowlist.

    Factored out (rather than inlined in the test below) so
    `test_meta_test_flags_new_unscoped_table` can prove this exact logic
    fires on a deliberately-broken model, instead of a reimplementation of
    it that might silently diverge from what actually runs in CI.
    """
    from coglib import Base

    misclassified = []
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if "user_id" not in mapper.columns:
            continue
        if issubclass(cls, UserOwnedMixin):
            continue
        if cls.__tablename__ in NULLABLE_OR_ADMIN_USER_ID:
            continue
        misclassified.append(cls.__tablename__)
    return misclassified


def test_every_user_id_model_is_classified():
    """A new table with user_id must be UserOwnedMixin or explicitly listed.

    This is what makes the suite enforcement rather than convention: adding
    a per-user table without the mixin (or without consciously adding it to
    the allowlist) fails CI, and mixin models join the parametrized tests
    above automatically.
    """
    misclassified = _misclassified_user_id_models()
    assert not misclassified, (
        f"models with user_id but no UserOwnedMixin (and not allowlisted): "
        f"{misclassified}"
    )


def test_meta_test_flags_new_unscoped_table():
    """N5's exit criterion, proven rather than assumed: "the test fails
    when a new unscoped table is added."

    Defines a throwaway model in-test with a `user_id` FK and no
    `UserOwnedMixin`, and asserts `_misclassified_user_id_models()` — the
    exact logic `test_every_user_id_model_is_classified` runs — actually
    flags it. Without this, the guard above could be silently vacuous (e.g.
    if `Base.registry.mappers` stopped including newly-defined classes for
    some SQLAlchemy-version reason) and nothing would notice: it would keep
    passing for the same reason a leak would pass unnoticed.

    A permanent test, not a one-off manual check — this IS the mutation
    check for the guard itself, kept in the suite so a future refactor of
    `_misclassified_user_id_models()` can't silently defang it either.
    """
    from coglib import Base

    class _N5SelfTestUnscoped(Base):
        __tablename__ = "n5_selftest_unscoped_table"

        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        # A real per-user column, deliberately with no UserOwnedMixin —
        # exactly the mistake the guard exists to catch.
        user_id: Mapped[int] = mapped_column(
            Integer, ForeignKey("users.id"), nullable=False,
        )

    table = _N5SelfTestUnscoped.__table__
    try:
        misclassified = _misclassified_user_id_models()
        assert "n5_selftest_unscoped_table" in misclassified, (
            "the guard did not flag a table with user_id and no "
            "UserOwnedMixin — it would pass silently on a real leak"
        )
    finally:
        # Same ordering as tests/test_drop_in_integration.py's
        # _release_orm_mapper_references(): drop every strong reference to
        # the mapped class BEFORE removing its table from Base.metadata.
        # SQLAlchemy's class registry is weak-referenced by design — an
        # explicit dispose call instead leaves a zombie Mapper behind whose
        # `.class_` is None, which breaks every OTHER test in this module
        # that does `issubclass(m.class_, UserOwnedMixin)` over
        # `Base.registry.mappers`.
        del _N5SelfTestUnscoped
        gc.collect()
        if table.name in Base.metadata.tables:
            Base.metadata.remove(table)


def test_user_owned_models_discovered():
    """Guard against the discovery itself silently returning nothing."""
    names = {m.__tablename__ for m in user_owned_models()}
    # Spot-check a few that must always be present.
    assert {"client_tokens", "mail_messages", "reminders", "scrobbles"} <= names
    assert len(names) >= 10
    # Wave 5.5: vault_read_grants was a documented gap (PR #81) — a table
    # scoped by `grantee_user_id`/`owner_user_id` rather than `user_id`,
    # invisible to a guard that only matched the standard column name. It
    # must now be classified, not exempted — see `app.privacy.USER_COLUMNS`.
    assert "vault_read_grants" in names


# ---------------------------------------------------------------------------
# 2b. vault_read_grants — the non-standard-column table (Wave 5.5)
# ---------------------------------------------------------------------------


def test_vault_read_grants_has_bespoke_user_columns():
    """`vault_read_grants` must be registered in `USER_COLUMNS` with BOTH its
    grantee and owner columns — the whole point of this table's entry is
    that a row is per-user in two different ways at once.
    """
    assert USER_COLUMNS.get("vault_read_grants") == (
        "grantee_user_id", "owner_user_id",
    )


def test_vault_read_grant_visible_to_grantee_and_owner_only(db_session):
    """A grant row (owner=alex, grantee=sam) must be visible to BOTH of
    them via the blessed `scoped_query` read path, and to nobody else —
    the exact invariant the backlog item names: "A grant row is visible to
    BOTH its grantee and its owner and to nobody else."
    """
    from app.models.vault_grants import VaultReadGrant
    from app.tools.helpers import scoped_query

    owner_id, grantee_id, stranger_id = 1, 2, 999999  # alex, sam, nobody

    grant = VaultReadGrant(
        grantee_user_id=grantee_id,
        owner_user_id=owner_id,
        scope="obsidian",
        reason="wave5.5 scoping test",
    )
    db_session.add(grant)
    db_session.commit()

    with use_user(owner_id):
        owner_sees = {r.id for r in scoped_query(db_session, VaultReadGrant).all()}
    with use_user(grantee_id):
        grantee_sees = {r.id for r in scoped_query(db_session, VaultReadGrant).all()}
    with use_user(stranger_id):
        stranger_sees = {r.id for r in scoped_query(db_session, VaultReadGrant).all()}

    assert grant.id in owner_sees, "the owner must see their own grant row"
    assert grant.id in grantee_sees, "the grantee must see the grant row"
    assert grant.id not in stranger_sees, "a third user must see nothing"


def test_what_lios_sees_counts_grant_rows_for_both_parties(db_session):
    """`system_what_lios_sees` must report a grant row to BOTH the owner and
    the grantee (one row, two callers, both a nonzero count) — and to a
    third caller as zero, mirroring the read-path test above through the
    actual tool handler `system` exposes.
    """
    from app.integrations.system.tools import handle_what_lios_sees
    from app.models.vault_grants import VaultReadGrant

    owner_id, grantee_id, stranger_id = 1, 2, 999999

    grant = VaultReadGrant(
        grantee_user_id=grantee_id,
        owner_user_id=owner_id,
        scope="obsidian",
        reason="wave5.5 what_lios_sees test",
    )
    db_session.add(grant)
    db_session.commit()

    def _grant_row_count(uid: int) -> int:
        with use_user(uid):
            out = json.loads(handle_what_lios_sees(db_session, {}))
        rows = [r for r in out["private"] if r["table"] == "vault_read_grants"]
        assert len(rows) == 1, "vault_read_grants must appear exactly once in `private`"
        return rows[0]["row_count"]

    assert _grant_row_count(owner_id) >= 1
    assert _grant_row_count(grantee_id) >= 1
    assert _grant_row_count(stranger_id) == 0


def test_user_columns_mapping_is_load_bearing(monkeypatch, db_session):
    """Guard self-test (mirrors `test_meta_test_flags_new_unscoped_table`'s
    pattern): prove that removing `vault_read_grants`' `USER_COLUMNS` entry
    actually reopens the PR #81 gap — the table falls out of
    `user_owned_models()` AND, independently, its read-path scoping
    collapses to unscoped (any caller sees every grant row) — rather than
    trusting that the mapping matters without checking.
    """
    import app.privacy as privacy_mod
    from app.models.vault_grants import VaultReadGrant
    from app.tools.helpers import scoped_query

    owner_id, grantee_id, stranger_id = 1, 2, 999999

    grant = VaultReadGrant(
        grantee_user_id=grantee_id,
        owner_user_id=owner_id,
        scope="obsidian",
        reason="wave5.5 mutation-check",
    )
    db_session.add(grant)
    db_session.commit()

    # With the mapping in place: a stranger sees nothing (asserted above,
    # re-checked here so the "before" half of the mutation check is real).
    with use_user(stranger_id):
        before = {r.id for r in scoped_query(db_session, VaultReadGrant).all()}
    assert grant.id not in before

    # Reintroduce the exact gap PR #81 documented: no bespoke entry.
    monkeypatch.setattr(privacy_mod, "USER_COLUMNS", {})

    assert "vault_read_grants" not in {
        m.__tablename__ for m in privacy_mod.user_owned_models()
    }, "removing the USER_COLUMNS entry should un-classify the table again"

    # And the read path: with no known user column, `hasattr(model,
    # "user_id")` is False, `scoped_query` applies no filter at all —
    # UNSCOPED, not "hidden". A stranger now sees the row too.
    with use_user(stranger_id):
        after = {r.id for r in scoped_query(db_session, VaultReadGrant).all()}
    assert grant.id in after, (
        "removing the USER_COLUMNS entry should have made vault_read_grants "
        "unscoped (visible to any caller) — if a stranger still can't see "
        "it, something else is scoping this table and the guard above is "
        "not the thing actually protecting it"
    )


def test_household_shared_tables_are_real_and_not_user_owned():
    """`app.privacy.HOUSEHOLD_SHARED_TABLES` is the single source of truth
    both this suite and `system_what_lios_sees` use for "shared by design,
    not a leak". Assert it against the live schema so it can't silently
    drift: every entry must name a table that still exists, and none of
    them may secretly be UserOwnedMixin or on the nullable/admin allowlist
    (either would mean the same table is claiming two contradictory
    classifications).
    """
    from coglib import Base

    user_owned_tablenames = {m.__tablename__ for m in user_owned_models()}
    for table_name, reason in HOUSEHOLD_SHARED_TABLES.items():
        assert reason.strip(), f"{table_name!r} has no reason string"
        assert table_name in Base.metadata.tables, (
            f"HOUSEHOLD_SHARED_TABLES lists {table_name!r}, which no longer "
            f"exists in the schema — stale entry"
        )
        assert table_name not in user_owned_tablenames, (
            f"{table_name!r} is listed as household-shared but is "
            f"UserOwnedMixin — contradicts itself"
        )
        assert table_name not in NULLABLE_OR_ADMIN_USER_ID, (
            f"{table_name!r} is in both HOUSEHOLD_SHARED_TABLES and "
            f"NULLABLE_OR_ADMIN_USER_ID — pick one classification"
        )


# ---------------------------------------------------------------------------
# 3. Canary sweep over every registerable tool — read-only AND write,
#    in both directions (N5)
# ---------------------------------------------------------------------------
#
# N5 found two real gaps in the sweep as it stood:
#
#   1. It only ran read-only tools (`if not ann.get("readOnlyHint"): continue`
#      skipped every write tool unconditionally). A write tool that echoes
#      data back — a bulk-update response, a transfer confirmation, a
#      created-row payload — can leak exactly the same way a read tool can,
#      and nothing was checking it. `_run_tool_sweep` below drops that
#      filter: every tool with a handler and without `openWorldHint` is
#      exercised, not just the read-only ones.
#   2. It only ran as user 1 against user 2's canary. The backlog's own
#      phrasing — "every tool run as Sam surfaces none of Alex's ids" —
#      names the direction that was untested. `test_tools_leak_no_cross_user_data`
#      below is parametrized over both directions.
#
# Tools named in the backlog as taking a user-identifying argument
# (`tasks_query` with `owner=`, `routines_transfer`, `vault_transfer`) are
# swept the same generic way as everything else here: they operate on
# household-shared tables (`tasks`, `routines`) or on the CALLER's own vault
# only (`vault_transfer` resolves `path` against the caller's vault and
# writes only into the recipient's `Inbox/` — it never reads the
# recipient's existing files), so there is no private-table leak path for
# the sweep to exercise beyond what it already covers generically; the
# sweep still runs them (uncaught exceptions from a nonexistent synthesized
# `routine`/`recipient` are the expected, skipped outcome, same as any other
# tool given a bogus id).


def _synthesize_args(input_schema: dict) -> dict:
    """Minimal valid arguments for a tool from its JSON Schema."""
    args = {}
    props = input_schema.get("properties", {})
    for name in input_schema.get("required", []):
        spec = props.get(name, {})
        ptype = spec.get("type", "string")
        if isinstance(ptype, list):
            ptype = ptype[0]
        if spec.get("enum"):
            args[name] = spec["enum"][0]
        elif ptype == "string":
            args[name] = "test"
        elif ptype in ("integer", "number"):
            args[name] = 1
        elif ptype == "boolean":
            args[name] = False
    return args


def _run_tool_sweep(db_session, caller_uid: int, other_marker: str, *, only_read_only: bool):
    """Seed both users' canaries, then invoke every registerable tool as
    `caller_uid` and collect (swept, skipped, leaks) tool names. Shared by
    both the original read-only-only test and the extended read+write one.
    """
    from app.integrations import INTEGRATIONS, register_all

    for model in user_owned_models():
        _seed_both_users(db_session, model)

    INTEGRATIONS.clear()
    register_all()

    swept, skipped, leaks = [], [], []
    for integration in INTEGRATIONS.values():
        try:
            tools = integration.mcp_tools()
        except Exception:
            skipped.append(f"{integration.name} (mcp_tools failed)")
            continue
        for tool_def in tools:
            name = tool_def["name"]
            # V4 chunk 1.2: every tool now carries its own inline annotations
            # (the centralized app/mcp/annotations.py fallback is gone).
            ann = tool_def.get("annotations") or {}
            if only_read_only and not ann.get("readOnlyHint"):
                continue
            if ann.get("openWorldHint"):
                skipped.append(f"{name} (open-world)")
                continue
            handler = tool_def.get("handler")
            if handler is None:
                continue
            args = _synthesize_args(tool_def.get("inputSchema", {}))
            try:
                with use_user(caller_uid):
                    out = handler(db_session, args)
            except Exception as e:
                db_session.rollback()
                skipped.append(f"{name} ({type(e).__name__})")
                continue
            out_text = out if isinstance(out, str) else json.dumps(out)
            if other_marker in out_text:
                leaks.append(name)
            swept.append(name)

    return swept, skipped, leaks


def test_readonly_tools_leak_no_cross_user_data(db_session_concurrent, monkeypatch):
    """Seed user 2 canaries everywhere; no read-only tool output as user 1
    may ever contain the canary, regardless of how the tool queries.

    Kept alongside the broader `test_tools_leak_no_cross_user_data` below
    (rather than folded into it) because this one's contract is narrower and
    stricter: read-only tools are expected to succeed cleanly against
    synthesized args far more often than write tools are, so a drop in
    `swept` here is a more specific signal than the same drop in the
    all-tools sweep.
    """
    # Live semantic search loads fastembed — stub the query embedder.
    from app.services import embedding as emb

    class _FakeModel:
        def embed(self, texts):
            import numpy as np
            return [np.zeros(emb.VECTOR_DIM, dtype=np.float32) for _ in texts]

    monkeypatch.setattr(emb, "get_model", lambda: _FakeModel())

    swept, skipped, leaks = _run_tool_sweep(db_session_concurrent, 1, U2_MARKER, only_read_only=True)

    assert not leaks, f"tools leaked user 2 data: {leaks}"
    # The sweep must actually sweep: if registration breaks, fail loudly
    # instead of green-by-vacancy.
    assert len(swept) >= 10, (
        f"only swept {len(swept)} tools ({swept}); skipped: {skipped}"
    )


_DIRECTIONS_2 = [
    pytest.param(1, U2_MARKER, id="as_alex_vs_sam"),
    pytest.param(2, U1_MARKER, id="as_sam_vs_alex"),
]


@pytest.mark.parametrize("caller_uid, other_marker", _DIRECTIONS_2)
def test_tools_leak_no_cross_user_data(db_session_concurrent, monkeypatch, caller_uid, other_marker):
    """N5: the same sweep, extended to WRITE tools too, in both directions.

    A write tool that returns the row it just created/updated/transferred
    is exactly as capable of leaking the other user's data as a read tool
    is — this was previously untested (the sweep filtered to
    `readOnlyHint` only). Both caller directions run: as Alex against
    Sam's canary, and — the direction the backlog names explicitly and
    the old suite never ran — as Sam against Alex's.
    """
    from app.services import embedding as emb

    class _FakeModel:
        def embed(self, texts):
            import numpy as np
            return [np.zeros(emb.VECTOR_DIM, dtype=np.float32) for _ in texts]

    monkeypatch.setattr(emb, "get_model", lambda: _FakeModel())

    swept, skipped, leaks = _run_tool_sweep(db_session_concurrent, caller_uid, other_marker, only_read_only=False)

    assert not leaks, f"tools leaked the other user's data (caller={caller_uid}): {leaks}"
    # Read-only tools alone already clear 10; requiring the same floor here
    # with write tools included guards against the broader sweep silently
    # degrading back to "read-only only" (e.g. a future refactor
    # reintroducing the filter this test exists to catch the absence of).
    assert len(swept) >= 10, (
        f"only swept {len(swept)} tools ({swept}); skipped: {skipped}"
)


# ---------------------------------------------------------------------------
# Folder-scoped vault grants (2026-09-06) — the leak direction is
# owner -> grantee, *within one user's rows*.
# ---------------------------------------------------------------------------
#
# Everything above asks "can user 2 see user 1's rows?". A folder-scoped
# grant deliberately lets user 2 read SOME of user 1's vault, so the question
# here is different and the per-user canary cannot ask it: with the grant
# bound exactly as dispatch binds it (`use_user(owner)` + the grant's
# folders), can any obsidian read tool surface a path or text from a folder
# the grant did not open? The canary sits in `Health/` — the folder the
# 2026-09-06 decision names as never in scope — and in the note's text as
# well as its path, so a tool that leaks either is caught.

HEALTH_CANARY = "LEAK-CANARY-HEALTH"
FOLDER_GRANT = ("Household/",)


def _seed_folder_grant_vault(session) -> None:
    from app.integrations.obsidian.models import VaultChunk
    from app.services import embedding as emb
    from app.services.embedding import Embedding, EmbeddingVecBgeSmall384

    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    notes = [
        # (path, text, weight on component 0 — the query vector is [1, 0, ...],
        # so the Health note is the BEST match and would rank first unscoped)
        (f"Health/{HEALTH_CANARY}.md", f"{HEALTH_CANARY} scan results", 1.0),
        ("Household/Renovation/Plan.md", "kitchen plan", 0.8),
    ]
    for i, (path, text, weight) in enumerate(notes):
        session.add(VaultChunk(
            user_id=1, path=path, file_hash=f"fg{i}", modified_at=now, indexed_at=now,
        ))
        row = Embedding(
            source="vault", source_id=path, user_id=1,
            chunk_text=text, content_hash=f"fgc{i}",
        )
        session.add(row)
        session.flush()
        vec = [0.0] * emb.VECTOR_DIM
        vec[0], vec[1] = weight, 1.0 - weight
        session.add(EmbeddingVecBgeSmall384(
            embedding_id=row.id, embedding=vec, model_name=emb.MODEL_NAME,
        ))
    session.commit()


def _run_folder_grant_sweep(db_session, monkeypatch):
    """Every obsidian read-only tool, run as the owner under a Household/-only
    grant binding. Returns (swept, skipped, leaks)."""
    from app.auth.context import use_vault_folders
    from app.integrations import INTEGRATIONS, register_all
    from app.services import embedding as emb

    class _HealthQuery:
        def embed(self, texts):
            import numpy as np
            v = np.zeros(emb.VECTOR_DIM, dtype=np.float32)
            v[0] = 1.0
            return [v for _ in texts]

    monkeypatch.setattr(emb, "get_model", lambda: _HealthQuery())
    _seed_folder_grant_vault(db_session)

    INTEGRATIONS.clear()
    register_all()
    obsidian = INTEGRATIONS["obsidian"]

    swept, skipped, leaks = [], [], []
    for tool_def in obsidian.mcp_tools():
        name = tool_def["name"]
        if not (tool_def.get("annotations") or {}).get("readOnlyHint"):
            continue
        args = _synthesize_args(tool_def.get("inputSchema", {}))
        try:
            with use_user(1), use_vault_folders(FOLDER_GRANT):
                out = tool_def["handler"](db_session, args)
        except Exception as e:  # noqa: BLE001 — a refusal is not a leak
            db_session.rollback()
            skipped.append(f"{name} ({type(e).__name__})")
            continue
        out_text = out if isinstance(out, str) else json.dumps(out)
        if HEALTH_CANARY in out_text:
            leaks.append(name)
        swept.append(name)
    return swept, skipped, leaks


def test_folder_scoped_grant_surfaces_nothing_outside_the_grant(db_session, monkeypatch):
    swept, skipped, leaks = _run_folder_grant_sweep(db_session, monkeypatch)
    assert not leaks, f"tools surfaced a folder outside the grant: {leaks}"
    # The sweep must actually sweep the three tools that render paths/text.
    assert {"vault_search", "vault_recent", "vault_stats"} <= set(swept), (
        f"swept {swept}; skipped {skipped}"
    )


def test_folder_scope_guard_fires_when_the_restriction_is_dropped(db_session, monkeypatch):
    """Permanent self-test of the test above: put the bug back and prove the
    sweep sees it. `restrict` is replaced by a version that drops the folder
    clause but keeps the execution-option stamp, so the SQL-level guard stays
    quiet and the only thing standing between the canary and the output is
    this sweep. If this test ever passes vacuously, the sweep is not looking
    at what it claims to."""
    from app.services import vault_scope

    monkeypatch.setattr(
        vault_scope, "restrict",
        lambda query, column, requested=None: query.execution_options(vault_scope_applied=True),
    )
    swept, skipped, leaks = _run_folder_grant_sweep(db_session, monkeypatch)
    assert "vault_search" in leaks and "vault_recent" in leaks, (
        f"guard did not fire: swept={swept} skipped={skipped} leaks={leaks}"
    )


def test_folder_scope_sql_guard_refuses_a_read_that_bypasses_restrict(db_session):
    """The second, independent line: a vault-table statement that never went
    through `restrict()` is refused before it executes while a folder grant
    is bound — so a new read tool that forgets the chokepoint fails loudly
    rather than leaking."""
    from app.auth.context import use_vault_folders
    from app.errors import PermanentError
    from app.integrations.obsidian.models import VaultChunk

    _seed_folder_grant_vault(db_session)
    with use_user(1), use_vault_folders(FOLDER_GRANT):
        with pytest.raises(PermanentError, match="did not apply the folder restriction"):
            db_session.query(VaultChunk.path).all()
