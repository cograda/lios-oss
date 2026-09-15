"""Unit tier: the folder-scope chokepoint's pure parts.

`app.services.vault_scope.restrict` is exercised against real SQL in
tests/test_vault_grant_folders.py (db tier). Here: folder normalisation (what
the admin script and the service both refuse), the narrow-never-widen rule
for a caller-supplied `folder`, the vault-only requirement, and that an
unbound call is a pure passthrough — the common path must not change shape.
"""

from __future__ import annotations

import pytest

from app.auth.context import use_vault_folders
from app.errors import PermanentError
from app.services import vault_scope
from app.services.vault_scope import normalise_folder, normalise_folders

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, want", [
    ("Household", "Household/"),
    ("Household/", "Household/"),
    ("  People  ", "People/"),
    ("Household/Renovation", "Household/Renovation/"),
    ("Household/Renovation//", "Household/Renovation/"),
    ("Daily Notes/Alex", "Daily Notes/Alex/"),
])
def test_normalise_folder_adds_one_trailing_slash(raw, want):
    assert normalise_folder(raw) == want


@pytest.mark.parametrize("raw", [
    "", "   ", "/", "/Household", "Household/../Health", "..", ".", "a/./b",
    "Household//Health" , "a\\b", None, 3,
])
def test_normalise_folder_rejects_unsafe_values(raw):
    """`..` is the one that matters: a prefix filter is a string comparison."""
    with pytest.raises(ValueError):
        normalise_folder(raw)


def test_normalise_folders_dedupes_and_keeps_order():
    assert normalise_folders(["People", "Household/", "People/"]) == ["People/", "Household/"]


def test_normalise_folders_refuses_an_empty_list():
    """`[]` must never quietly mean 'the whole vault'."""
    with pytest.raises(ValueError, match="at least one"):
        normalise_folders([])


# ---------------------------------------------------------------------------
# narrow, never widen
# ---------------------------------------------------------------------------

class _Q:
    """Records what restrict() did to it, without SQLAlchemy."""

    def __init__(self):
        self.filters = []
        self.options = {}

    def filter(self, clause):
        self.filters.append(str(clause))
        return self

    def execution_options(self, **kw):
        self.options.update(kw)
        return self


def _col():
    from sqlalchemy import Column, String
    return Column("path", String)


def test_unbound_call_with_no_folder_is_a_passthrough():
    q = _Q()
    assert vault_scope.restrict(q, _col()) is q
    assert q.filters == [] and q.options == {}


def test_unbound_call_with_a_folder_is_the_historical_ilike():
    q = _Q()
    vault_scope.restrict(q, _col(), "Household")
    # Case-insensitive prefix — the generic dialect renders ILIKE as lower()/lower().
    assert len(q.filters) == 1 and "lower(" in q.filters[0]
    assert q.options == {}  # nothing to stamp — no guard is watching


def test_bound_call_adds_the_grant_clause_and_stamps_the_query():
    q = _Q()
    with use_vault_folders(("Household/", "People/")):
        vault_scope.restrict(q, _col())
    assert len(q.filters) == 1
    # Case-SENSITIVE: a grant on Household/ must not admit household/.
    assert "LIKE" in q.filters[0] and "lower(" not in q.filters[0]
    assert q.options == {"vault_scope_applied": True}


@pytest.mark.parametrize("requested", ["Health", "Health/", "Notes/x", "Blog"])
def test_folder_outside_the_grant_is_refused_not_emptied(requested):
    with use_vault_folders(("Household/", "People/")):
        with pytest.raises(PermanentError, match="outside this grant"):
            vault_scope.restrict(_Q(), _col(), requested)


@pytest.mark.parametrize("requested", [
    "Household", "Household/", "Household/Renovation", "household/renovation",
    "People/Builders", "House",  # broader than the grant: ANDed down to it
])
def test_folder_inside_or_broader_than_the_grant_narrows(requested):
    q = _Q()
    with use_vault_folders(("Household/", "People/")):
        vault_scope.restrict(q, _col(), requested)
    assert len(q.filters) == 2  # caller's prefix AND the grant's
    assert q.options == {"vault_scope_applied": True}


# ---------------------------------------------------------------------------
# vault-only
# ---------------------------------------------------------------------------

def test_require_vault_only_is_a_noop_when_unbound():
    vault_scope.require_vault_only(None, what="x")
    vault_scope.require_vault_only(["email", "vault"], what="x")


@pytest.mark.parametrize("sources", [None, ["email"], ["vault", "whatsapp"]])
def test_require_vault_only_refuses_cross_source_reads_under_a_folder_grant(sources):
    with use_vault_folders(("Household/",)):
        with pytest.raises(PermanentError, match="folder-scoped"):
            vault_scope.require_vault_only(sources, what="semantic search")


def test_require_vault_only_admits_vault_under_a_folder_grant():
    with use_vault_folders(("Household/",)):
        vault_scope.require_vault_only(["vault"], what="semantic search")


# ---------------------------------------------------------------------------
# the admin script's --folders parsing shares the same rules
# ---------------------------------------------------------------------------

def test_script_parse_folders():
    from app.scripts.grant_vault_read import parse_folders

    assert parse_folders(None) is None
    assert parse_folders("Household/,People") == ["Household/", "People/"]
    for bad in ["", ",", "Household/,", "../x", "/abs"]:
        with pytest.raises(ValueError):
            parse_folders(bad)


def test_script_rejects_bad_folders_before_touching_the_database(monkeypatch):
    import app.scripts.grant_vault_read as script

    def _no_db():  # pragma: no cover - must never be called
        raise AssertionError("bad --folders must be refused before get_db()")

    monkeypatch.setattr(script, "get_db", _no_db)
    assert script.main([
        "--grantee", "sam", "--owner", "alex",
        "--folders", "Household/../Health", "--reason", "x",
    ]) == 2
