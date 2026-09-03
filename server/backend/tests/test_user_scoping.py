"""User-scoping enforcement suite (db tier).

Converts per-user scoping from convention to enforcement:

  1. Parametrized over EVERY model carrying UserOwnedMixin — new models
     join the parametrization automatically via the SQLAlchemy registry.
     Each is exercised through the production DSL paths (ListTool +
     SearchTool handlers, real SQL) as user 1 with user 2's data seeded.
  2. A meta-test pins the classification: any model with a user_id column
     must be UserOwnedMixin or on the explicit nullable/admin allowlist.
  3. A canary sweep: user 2's rows in every user-owned table carry a
     marker string; every registerable read-only tool is invoked as
     user 1 and its output must never contain the marker.

EmbeddingService.search scoping (the NULL = household-shared pattern)
is covered in test_embedding_pipeline.py.
"""

import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import DateTime, String, Text
from sqlalchemy import inspect as sa_inspect

import app.models  # noqa: F401 — register every model on Base.metadata
from app.auth.context import use_user
from app.mixins import UserOwnedMixin

pytestmark = pytest.mark.db

U1_MARKER = "SCOPETEST-OWN-DATA"
U2_MARKER = "LEAK-CANARY-U2"

# Models with a user_id column that deliberately do NOT take UserOwnedMixin.
# Embedding/EmbeddingQueue: nullable user_id, NULL = household-shared,
# scoped inside EmbeddingService.search. InstallCode: admin-issued
# onboarding artefact, user_id records who the install is FOR. ToolCall:
# nullable user_id, admin/ops audit trail (tool-call dispatch log) — not
# per-user application data, ON DELETE SET NULL so it outlives the user row.
NULLABLE_OR_ADMIN_USER_ID = {
    "embeddings", "embedding_queue", "install_codes", "tool_calls",
    # auth_events: nullable user_id by design (a failed-auth attempt usually
    # has no resolved user) — admin/ops audit log, not per-user app data.
    # See app/models/auth_events.py's docstring (V4 chunk 2.5).
    "auth_events",
    # sync_cursors: nullable user_id by design — single-account integrations
    # (weather, lastfm's global sync) have no owning user for their cursor.
    # Opt-in bookkeeping table, not per-user app data. See
    # app/models/sync_cursor.py (V4 chunk 3.2).
    "sync_cursors",
    # notification_sends: nullable user_id by design (F7, hardening 2026-08) —
    # NULL means household-shared infrastructure alert, which is the NORMAL
    # case, not a backfill artifact; only user-attributable alerts (e.g. the
    # apple_health per-user data-gap) carry an owner. UserOwnedMixin's
    # NOT NULL would be wrong here. The read-side scoping (bound caller sees
    # shared + own, never another user's) is enforced directly by
    # tests/test_notifications.py::TestNotifyRecentScoping and the migration
    # docstring (2026_08_08_8b9c0d1e2f3a).
    "notification_sends",
}


def user_owned_models() -> list[type]:
    """Every currently-live `UserOwnedMixin` model registered against
    `coglib.Base`.

    Filters to mappers whose table is *currently* a member of
    `Base.metadata.tables` — not just "still has a live Python class
    object". This matters because of how
    `tests/test_drop_in_integration.py`'s throwaway packages clean up: a
    dropped-in `TemplateItem` class can remain reachable in
    `Base.registry.mappers` for a little while after its `with` block
    exits (Python has no block-level scoping — a local variable assigned
    inside a `with` statement stays alive until the *function* returns, not
    until the `with` exits, so garbage collection of the class can lag
    behind that fixture's own cleanup by one stack frame's worth of
    locals). That fixture's cleanup is authoritative and immediate about
    one specific thing regardless of GC timing: it removes the table from
    `Base.metadata` synchronously via `Base.metadata.remove(table)`. So the
    right thing to sweep here isn't "every mapper the registry happens to
    still hold a reference to" but "every mapper whose table genuinely
    still exists in the current schema" — a model that's been torn back
    out from under its table should never be seeded with test rows,
    because nothing created that table in the real database either.
    """
    from coglib import Base

    return sorted(
        (
            m.class_
            for m in Base.registry.mappers
            if issubclass(m.class_, UserOwnedMixin)
            and m.local_table is not None
            and m.local_table.name in Base.metadata.tables
        ),
        key=lambda c: c.__tablename__,
    )


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

    `fk_overrides` (column name -> id) supplies values for required foreign
    keys that point somewhere other than `users` — see `_ensure_fk_parents`.
    Without it, a generic int filler (1) would only work by accident.
    """
    mapper = sa_inspect(model)
    kwargs = {}
    for col in mapper.columns:
        if col.primary_key:
            continue
        if col.name == "user_id":
            kwargs["user_id"] = user_id
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
    fk_overrides = _ensure_fk_parents(session, model, U1_MARKER)
    session.add(make_row(model, 1, U1_MARKER, fk_overrides))
    session.add(make_row(model, 2, U2_MARKER, fk_overrides))
    session.commit()


def _row_dump(row) -> dict:
    mapper = sa_inspect(type(row))
    return {c.name: str(getattr(row, c.name)) for c in mapper.columns}


# ---------------------------------------------------------------------------
# 1. DSL enforcement, parametrized over every user-owned model
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model", user_owned_models(), ids=lambda m: m.__tablename__)
def test_list_tool_scopes(db_session, model):
    """ListTool as user 1 must return user 1's row and never user 2's."""
    from app.tools.list_tool import ListTool

    _seed_both_users(db_session, model)
    handler = ListTool(
        name="probe_list",
        description="scoping probe",
        model=model,
        timestamp_col=_timestamp_col(model),
        to_dict=_row_dump,
    ).build()["handler"]

    with use_user(1):
        out = handler(db_session, {"limit": 100})

    assert U2_MARKER not in out, f"{model.__tablename__} leaked user 2 data"
    assert U1_MARKER in out, f"{model.__tablename__} probe returned no own data"


@pytest.mark.parametrize("model", user_owned_models(), ids=lambda m: m.__tablename__)
def test_search_tool_scopes(db_session, model):
    """SearchTool matching the marker text must still stay user-scoped."""
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

    with use_user(1):
        # Search for the OTHER user's marker — the worst case.
        out = handler(db_session, {"query": U2_MARKER, "limit": 100})

    assert U2_MARKER not in out, f"{model.__tablename__} leaked via search"


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

def test_every_user_id_model_is_classified():
    """A new table with user_id must be UserOwnedMixin or explicitly listed.

    This is what makes the suite enforcement rather than convention: adding
    a per-user table without the mixin (or without consciously adding it to
    the allowlist) fails CI, and mixin models join the parametrized tests
    above automatically.
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

    assert not misclassified, (
        f"models with user_id but no UserOwnedMixin (and not allowlisted): "
        f"{misclassified}"
    )


def test_user_owned_models_discovered():
    """Guard against the discovery itself silently returning nothing."""
    names = {m.__tablename__ for m in user_owned_models()}
    # Spot-check a few that must always be present.
    assert {"client_tokens", "mail_messages", "reminders", "scrobbles"} <= names
    assert len(names) >= 10


# ---------------------------------------------------------------------------
# 3. Canary sweep over every registerable read-only tool
# ---------------------------------------------------------------------------

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


def test_readonly_tools_leak_no_cross_user_data(db_session, monkeypatch):
    """Seed user 2 canaries everywhere; no read-only tool output as user 1
    may ever contain the canary, regardless of how the tool queries."""
    from app.integrations import INTEGRATIONS, register_all

    # Live semantic search loads fastembed — stub the query embedder.
    from app.services import embedding as emb

    class _FakeModel:
        def embed(self, texts):
            import numpy as np
            return [np.zeros(emb.VECTOR_DIM, dtype=np.float32) for _ in texts]

    monkeypatch.setattr(emb, "get_model", lambda: _FakeModel())

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
            if not ann.get("readOnlyHint"):
                continue
            if ann.get("openWorldHint"):
                skipped.append(f"{name} (open-world)")
                continue
            handler = tool_def.get("handler")
            if handler is None:
                continue
            args = _synthesize_args(tool_def.get("inputSchema", {}))
            try:
                with use_user(1):
                    out = handler(db_session, args)
            except Exception as e:
                db_session.rollback()
                skipped.append(f"{name} ({type(e).__name__})")
                continue
            out_text = out if isinstance(out, str) else json.dumps(out)
            if U2_MARKER in out_text:
                leaks.append(name)
            swept.append(name)

    assert not leaks, f"tools leaked user 2 data: {leaks}"
    # The sweep must actually sweep: if registration breaks, fail loudly
    # instead of green-by-vacancy.
    assert len(swept) >= 10, (
        f"only swept {len(swept)} tools ({swept}); skipped: {skipped}"
    )
