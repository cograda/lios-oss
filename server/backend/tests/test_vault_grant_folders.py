"""Folder-scoped cross-user vault grants (db tier).

The scenario is the real one: Alex's vault holds `Household/` and `People/`
(shared context for the renovation) beside `Health/` and `Notes/` (never in
scope). Sam holds a grant for the first two. Every test binds exactly what
`app/plugin/dispatch.py` binds — `use_user(owner)` plus
`use_vault_folders(grant.folders)` — and then calls the real tool handlers.

The vectors are arranged so that the *forbidden* note ranks highest for the
query. A test where Health merely happened to rank low would pass with the
restriction deleted; this one cannot.
"""

from __future__ import annotations

import json

import pytest

from app.auth.context import use_user, use_vault_folders
from app.errors import PermanentError
from app.services import embedding as emb
from app.services.embedding import Embedding, EmbeddingQueue, EmbeddingVecBgeSmall384
from app.services.vault_grants import AS_USER_ARG, grant_read, resolve_grant

pytestmark = pytest.mark.db

READ_ONLY = {"readOnlyHint": True}
GRANTED = ("Household/", "People/")

# path -> (weight on component 0). The query vector is _vec(1.0), so a higher
# weight ranks higher: Health is deliberately the best match.
NOTES = {
    "Health/MRI.md": 1.0,
    "Notes/Greenhouse.md": 0.9,
    "Household/Renovation/Plan.md": 0.8,
    "People/Builder.md": 0.7,
}


def _vec(x: float) -> list[float]:
    v = [0.0] * emb.VECTOR_DIM
    v[0] = x
    v[1] = 1.0 - abs(x)
    return v


def _seed_vault(session, user_id: int) -> None:
    from datetime import datetime, timedelta, timezone

    from app.integrations.obsidian.models import VaultChunk

    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    for i, (path, weight) in enumerate(NOTES.items()):
        session.add(VaultChunk(
            user_id=user_id, path=path, file_hash=f"h{i}",
            modified_at=now - timedelta(hours=i), indexed_at=now,
        ))
        row = Embedding(
            source="vault", source_id=path, user_id=user_id,
            chunk_text=f"{path} body", content_hash=f"c{i}",
        )
        session.add(row)
        session.flush()
        session.add(EmbeddingVecBgeSmall384(
            embedding_id=row.id, embedding=_vec(weight), model_name=emb.MODEL_NAME,
        ))
    session.add(EmbeddingQueue(
        source="vault", source_id="Health/pending.md", user_id=user_id,
        content="x", content_hash="q1", status="pending",
    ))
    session.add(EmbeddingQueue(
        source="vault", source_id="Household/pending.md", user_id=user_id,
        content="y", content_hash="q2", status="pending",
    ))
    session.commit()


@pytest.fixture
def alex_vault(db_session, monkeypatch):
    class _FakeModel:
        def embed(self, texts):
            import numpy as np
            return [np.array(_vec(1.0), dtype=np.float32) for _ in texts]

    monkeypatch.setattr(emb, "get_model", lambda: _FakeModel())
    _seed_vault(db_session, 1)
    return db_session


def _tool(name):
    from app.integrations.obsidian.tools import get_mcp_tools

    return next(t for t in get_mcp_tools() if t["name"] == name)["handler"]


def _paths(search_output: str) -> list[str]:
    return [r["path"] for r in json.loads(search_output)]


# ---------------------------------------------------------------------------
# (a) search: granted folders come back, Health never does — even ranked first
# ---------------------------------------------------------------------------

def test_health_ranks_first_for_the_owner_himself(alex_vault):
    """Establishes the premise the next test depends on."""
    with use_user(1):
        out = _paths(_tool("vault_search")(alex_vault, {"query": "anything"}))
    assert out[0] == "Health/MRI.md"
    assert set(out) == set(NOTES)


def test_grantee_search_returns_granted_hits_and_never_health(alex_vault):
    with use_user(1), use_vault_folders(GRANTED):
        out = _paths(_tool("vault_search")(alex_vault, {"query": "anything"}))
    assert out == ["Household/Renovation/Plan.md", "People/Builder.md"]
    assert not any(p.startswith(("Health/", "Notes/")) for p in out)


def test_grantee_search_is_bounded_by_the_grant_not_by_the_user_filter(alex_vault):
    """Same owner, same rows — only the folder binding differs."""
    with use_user(1), use_vault_folders(("People/",)):
        out = _paths(_tool("vault_search")(alex_vault, {"query": "anything"}))
    assert out == ["People/Builder.md"]


# ---------------------------------------------------------------------------
# (b) a caller-supplied folder narrows, never widens
# ---------------------------------------------------------------------------

def test_folder_outside_the_grant_is_refused(alex_vault):
    with use_user(1), use_vault_folders(GRANTED):
        with pytest.raises(PermanentError, match="outside this grant"):
            _tool("vault_search")(alex_vault, {"query": "anything", "folder": "Health"})


def test_folder_inside_the_grant_narrows(alex_vault):
    with use_user(1), use_vault_folders(GRANTED):
        out = _paths(_tool("vault_search")(alex_vault, {"query": "x", "folder": "Household"}))
    assert out == ["Household/Renovation/Plan.md"]


def test_folder_broader_than_the_grant_is_anded_down_to_it(alex_vault):
    """'House' is a prefix of 'Household/' — allowed, and still bounded."""
    with use_user(1), use_vault_folders(GRANTED):
        out = _paths(_tool("vault_search")(alex_vault, {"query": "x", "folder": "House"}))
    assert out == ["Household/Renovation/Plan.md"]


def test_case_variant_of_a_private_folder_does_not_widen(alex_vault):
    with use_user(1), use_vault_folders(GRANTED):
        with pytest.raises(PermanentError):
            _tool("vault_search")(alex_vault, {"query": "x", "folder": "health"})


# ---------------------------------------------------------------------------
# (c) a NULL-folders grant behaves exactly as today
# ---------------------------------------------------------------------------

def test_whole_vault_grant_resolves_to_no_folder_scope(alex_vault):
    grant_read(alex_vault, grantee="sam", owner="alex", scope="obsidian", reason="r")
    res = resolve_grant(
        alex_vault, caller_id=2, caller_name="sam", arguments={AS_USER_ARG: "alex"},
        tool_name="vault_search", integration_name="obsidian", annotations=READ_ONLY,
    )
    assert res.user_id == 1 and res.folders is None

    with use_user(1):
        own = _tool("vault_search")(alex_vault, {"query": "anything"})
    with use_user(res.user_id), use_vault_folders(res.folders):
        via_grant = _tool("vault_search")(alex_vault, {"query": "anything"})
    assert json.loads(own) == json.loads(via_grant)
    assert _paths(via_grant)[0] == "Health/MRI.md"


def test_folder_grant_resolves_to_its_normalised_folders(alex_vault):
    grant_read(
        alex_vault, grantee="sam", owner="alex", scope="obsidian", reason="r",
        folders=["Household", "People/"],
    )
    res = resolve_grant(
        alex_vault, caller_id=2, caller_name="sam", arguments={AS_USER_ARG: "alex"},
        tool_name="vault_search", integration_name="obsidian", annotations=READ_ONLY,
    )
    assert res.user_id == 1 and res.folders == ("Household/", "People/")


def test_regrant_with_different_folders_is_refused_not_kept(alex_vault):
    grant_read(alex_vault, grantee="sam", owner="alex", scope="obsidian",
               reason="r", folders=["Household/"])
    with pytest.raises(ValueError, match="revoke it first"):
        grant_read(alex_vault, grantee="sam", owner="alex", scope="obsidian",
                   reason="r", folders=["Household/", "People/"])
    with pytest.raises(ValueError, match="revoke it first"):
        grant_read(alex_vault, grantee="sam", owner="alex", scope="obsidian", reason="r")


def test_malformed_stored_folders_refuse_rather_than_open_the_vault(alex_vault):
    from sqlalchemy import select

    from app.models.vault_grants import VaultReadGrant

    grant_read(alex_vault, grantee="sam", owner="alex", scope="obsidian",
               reason="r", folders=["Household/"])
    row = alex_vault.execute(select(VaultReadGrant)).scalar_one()
    row.folders = []  # a hand edit that must not mean "everything"
    alex_vault.flush()
    with pytest.raises(PermanentError, match="invalid folder list"):
        resolve_grant(
            alex_vault, caller_id=2, caller_name="sam", arguments={AS_USER_ARG: "alex"},
            tool_name="vault_search", integration_name="obsidian", annotations=READ_ONLY,
        )


# ---------------------------------------------------------------------------
# (d) stats and recent count only the granted folders
# ---------------------------------------------------------------------------

def test_vault_stats_as_grantee_counts_only_granted_folders(alex_vault):
    with use_user(1):
        own = json.loads(_tool("vault_stats")(alex_vault, {}))
    with use_user(1), use_vault_folders(GRANTED):
        theirs = json.loads(_tool("vault_stats")(alex_vault, {}))

    assert own["total_files"] == 4 and set(own["by_folder"]) == {"Health", "Notes", "Household", "People"}
    assert theirs["total_files"] == 2
    assert theirs["total_embedded"] == 2
    assert theirs["queue_pending"] == 1  # Household/pending.md, not Health/pending.md
    assert set(theirs["by_folder"]) == {"Household", "People"}


def test_vault_recent_as_grantee_lists_no_private_paths_without_a_folder_arg(alex_vault):
    """The ListTool's folder ExtraFilter is skipped when absent — the grant
    must apply anyway, or a bare `vault_recent` lists the whole vault."""
    with use_user(1), use_vault_folders(GRANTED):
        rows = json.loads(_tool("vault_recent")(alex_vault, {}))
    assert {r["path"] for r in rows} == {"Household/Renovation/Plan.md", "People/Builder.md"}


def test_vault_recent_folder_arg_cannot_widen(alex_vault):
    with use_user(1), use_vault_folders(GRANTED):
        with pytest.raises(PermanentError):
            _tool("vault_recent")(alex_vault, {"folder": "Health"})
        rows = json.loads(_tool("vault_recent")(alex_vault, {"folder": "People"}))
    assert [r["path"] for r in rows] == ["People/Builder.md"]


# ---------------------------------------------------------------------------
# similar / duplicates
# ---------------------------------------------------------------------------

def test_vault_similar_cannot_use_a_private_note_as_seed(alex_vault):
    with use_user(1):
        assert json.loads(_tool("vault_similar")(alex_vault, {"path": "Health/MRI.md"}))["results"]
    with use_user(1), use_vault_folders(GRANTED):
        out = json.loads(_tool("vault_similar")(alex_vault, {"path": "Health/MRI.md"}))
    assert out["results"] == []


def test_vault_similar_neighbours_stay_inside_the_grant(alex_vault):
    with use_user(1), use_vault_folders(GRANTED):
        out = json.loads(_tool("vault_similar")(alex_vault, {"path": "People/Builder.md"}))
    assert [r["path"] for r in out["results"]] == ["Household/Renovation/Plan.md"]


def test_vault_duplicates_stay_inside_the_grant(alex_vault):
    with use_user(1), use_vault_folders(GRANTED):
        out = json.loads(_tool("vault_duplicates")(alex_vault, {"threshold": 0.0, "exclude_folders": []}))
    names = {p["a"] for p in out["pairs"]} | {p["b"] for p in out["pairs"]}
    assert names == {"Household/Renovation/Plan.md", "People/Builder.md"}


# ---------------------------------------------------------------------------
# fail closed: a vault-table read that skipped the chokepoint is refused
# ---------------------------------------------------------------------------

def test_unscoped_vault_reads_are_refused_while_a_folder_grant_is_bound(alex_vault):
    from app.integrations.obsidian.models import VaultChunk

    with use_user(1), use_vault_folders(GRANTED):
        with pytest.raises(PermanentError, match="did not apply the folder restriction"):
            alex_vault.query(VaultChunk).all()
        with pytest.raises(PermanentError, match="did not apply the folder restriction"):
            alex_vault.query(Embedding.source_id).filter_by(source="vault").all()
        with pytest.raises(PermanentError, match="did not apply the folder restriction"):
            alex_vault.query(EmbeddingQueue.id).all()
        # Unrelated tables are untouched — this is not a global read lock.
        from app.models.users import User
        assert alex_vault.query(User).count() >= 2


def test_the_guard_is_inert_when_no_folder_grant_is_bound(alex_vault):
    from app.integrations.obsidian.models import VaultChunk

    with use_user(1):
        assert alex_vault.query(VaultChunk).count() == 4


def test_cross_source_search_is_refused_under_a_folder_grant(alex_vault):
    from app.services.embedding import EmbeddingService

    with use_user(1), use_vault_folders(GRANTED):
        with pytest.raises(PermanentError, match="folder-scoped"):
            EmbeddingService.search(alex_vault, "x", sources=["vault", "email"])
        with pytest.raises(PermanentError, match="folder-scoped"):
            EmbeddingService.search(alex_vault, "x")


# ---------------------------------------------------------------------------
# the admin script end to end
# ---------------------------------------------------------------------------

def test_script_grants_with_folders_and_lists_them(alex_vault, monkeypatch, capsys):
    import contextlib

    import app.scripts.grant_vault_read as script
    from sqlalchemy import select

    from app.models.vault_grants import VaultReadGrant

    class _Db:
        def session(self):
            @contextlib.contextmanager
            def _cm():
                yield alex_vault
            return _cm()

    monkeypatch.setattr(script, "get_db", lambda: _Db())
    monkeypatch.setattr(alex_vault, "commit", alex_vault.flush)  # keep it inside the test txn

    assert script.main([
        "--grantee", "sam", "--owner", "alex",
        "--folders", "Household/,People", "--reason", "shared renovation context",
    ]) == 0
    row = alex_vault.execute(select(VaultReadGrant)).scalar_one()
    assert row.folders == ["Household/", "People/"]

    assert script.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "sam -> alex" in out and "folders=Household/,People/" in out
