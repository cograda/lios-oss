"""Tests for `app.services.text.escape_ilike` — the shared ilike-escaping
helper converting ~12 call sites that previously built
`col.ilike(f"%{q}%")` with no escaping (a literal `%`/`_` in a search term
silently became a wildcard)."""

from __future__ import annotations

from app.services.text import ILIKE_ESCAPE_CHAR, escape_ilike


class TestEscapeIlike:
    def test_percent_is_escaped(self):
        assert escape_ilike("50%") == "50\\%"

    def test_underscore_is_escaped(self):
        assert escape_ilike("foo_bar") == "foo\\_bar"

    def test_backslash_is_escaped_first(self):
        """Escaping order matters: backslash must go first, or escaping
        %/_ afterward would double-escape the backslashes just inserted."""
        assert escape_ilike("a\\b") == "a\\\\b"

    def test_plain_text_is_unchanged(self):
        assert escape_ilike("coffee") == "coffee"

    def test_combined_special_characters(self):
        assert escape_ilike("100%_off\\now") == "100\\%\\_off\\\\now"

    def test_empty_string(self):
        assert escape_ilike("") == ""

    def test_escape_char_constant_matches_helper(self):
        assert ILIKE_ESCAPE_CHAR == "\\"


class TestEscapedPatternBehavesLikeALiteral:
    """End-to-end-ish: the escaped pattern, run through SQLAlchemy's compiled
    SQL, actually carries the escape clause (real matching semantics need a
    live DB — covered by the db-tier scoping tests elsewhere)."""

    def test_ilike_with_escape_kwarg_compiles_an_escape_clause(self):
        from sqlalchemy import Column, String
        from sqlalchemy.orm import declarative_base

        Base = declarative_base()

        class _Dummy(Base):
            __tablename__ = "_dummy_escape_test"
            id = Column(String, primary_key=True)
            name = Column(String)

        term = "50% off_deal"
        pattern = f"%{escape_ilike(term)}%"
        clause = _Dummy.name.ilike(pattern, escape=ILIKE_ESCAPE_CHAR)
        compiled = str(clause.compile(compile_kwargs={"literal_binds": True}))

        assert "ESCAPE" in compiled.upper()
        # The escaped pattern itself carries backslash-escaped wildcards,
        # not raw ones — proof the helper ran before the pattern reached SQL.
        assert "50\\%" in compiled or "50\\\\%" in compiled
