"""The AI role registry (W2 chunk 2) — role -> binding resolution.

Per-role resolution tests live alongside their integration (`test_vision.py`
has `vision.inbox`, `test_transcription.py` has `stt.memo`) since they need
that integration's own config-store fixtures. This file covers what's
genuinely shared: the multi-binding `embed.corpus` role (the one thin
wrapper over `embedding_provider.get_providers()`), the "unknown role"
error, and the rate-warning divergence from the plan's letter — see
`app/services/ai_roles.py`'s module docstring for why a missing rate row is
a warning here, not the hard failure the plan's text describes.
"""

from __future__ import annotations

import logging

import pytest

from app.services import ai_roles

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_warnings():
    ai_roles._reset_warnings_for_tests()
    yield
    ai_roles._reset_warnings_for_tests()


# ---------------------------------------------------------------------------
# embed.corpus — the one multi-binding role
# ---------------------------------------------------------------------------

class TestEmbedCorpusRole:
    def test_single_provider_resolves_to_one_binding(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "embedding_provider", "fastembed-bge-small")
        bindings = ai_roles.resolve_embed_corpus()
        assert len(bindings) == 1
        assert bindings[0].role == "embed.corpus"
        assert bindings[0].provider == "local"
        assert bindings[0].model == "BAAI/bge-small-en-v1.5"

    def test_ordered_multi_binding_is_preserved(self, monkeypatch):
        """The plan's requirement: model the role value as the same ordered
        list embedding already uses. Order must survive the wrapper."""
        from app.config import settings

        monkeypatch.setattr(
            settings, "embedding_provider", "gemini-embedding-2,fastembed-bge-small"
        )
        bindings = ai_roles.resolve_embed_corpus()
        assert [b.provider for b in bindings] == ["google", "local"]
        assert [b.model for b in bindings] == ["gemini-embedding-2", "BAAI/bge-small-en-v1.5"]

    def test_empty_setting_is_a_loud_error(self, monkeypatch):
        """Delegates to `embedding_provider._configured_ids()`'s own
        ValueError — this role's "no binding" failure, unchanged by this
        chunk."""
        from app.config import settings

        monkeypatch.setattr(settings, "embedding_provider", "")
        with pytest.raises(ValueError):
            ai_roles.resolve_embed_corpus()

    def test_unknown_provider_id_is_a_loud_error(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "embedding_provider", "not-a-real-provider")
        with pytest.raises(ValueError):
            ai_roles.resolve_embed_corpus()

    def test_does_not_break_active_spaces_semantics(self, monkeypatch):
        """The hard constraint: this role is a thin wrapper, not a
        reimplementation. `get_providers()` itself is what `_active_spaces()`
        calls, so proving the wrapper returns the same provider objects (by
        provider_id) is what "does not change embedding semantics" means."""
        from app.config import settings
        from app.plugin.embedding_provider import get_providers

        monkeypatch.setattr(
            settings, "embedding_provider", "gemini-embedding-2,fastembed-bge-small"
        )
        direct = [p.provider_id for p in get_providers()]
        via_role = [
            {"local": "fastembed-bge-small", "google": "gemini-embedding-2"}[b.provider]
            for b in ai_roles.resolve_embed_corpus()
        ]
        assert direct == via_role


# ---------------------------------------------------------------------------
# resolve() dispatch and unknown roles
# ---------------------------------------------------------------------------

class TestDispatch:
    def test_unknown_role_raises_value_error(self):
        with pytest.raises(ValueError, match="unknown role"):
            ai_roles.resolve("face.identify")

    def test_multi_binding_role_refuses_the_single_resolver(self):
        """embed.corpus must be resolved via resolve_embed_corpus(), not
        resolve() — a caller reaching for the wrong one gets told why,
        not a wrong-shaped return value."""
        with pytest.raises(ValueError, match="multi-binding"):
            ai_roles.resolve("embed.corpus")

    def test_roles_set_only_lists_live_callers(self):
        """Panel/face roles from the plan's table arrive with their
        consumers — registering them with no caller would be dead config."""
        assert ai_roles.ROLES == {"stt.memo", "vision.inbox", "embed.corpus"}


# ---------------------------------------------------------------------------
# The rate-warning divergence (flagged in the PR body)
# ---------------------------------------------------------------------------

class TestRateWarning:
    def test_unrated_model_warns_once_and_does_not_raise(self, monkeypatch, caplog):
        """embed.corpus's bindings (fastembed, gemini-embedding-2) have no
        row in coglib.llm.MODELS, which prices chat/vision models only. The
        plan's letter says this should hard-fail; this chunk logs one
        warning instead and lets the call proceed — see ai_roles.py's module
        docstring for the justification, flagged in the PR body for review.
        """
        from app.config import settings

        monkeypatch.setattr(settings, "embedding_provider", "fastembed-bge-small")
        with caplog.at_level(logging.WARNING, logger="app.services.ai_roles"):
            ai_roles.resolve_embed_corpus()
            ai_roles.resolve_embed_corpus()  # second call must not warn again

        warnings = [r for r in caplog.records if "no rate row" in r.message]
        assert len(warnings) == 1
        assert "BAAI/bge-small-en-v1.5" in warnings[0].message

    def test_rated_model_never_warns(self, monkeypatch, caplog):
        """stt.memo's default (`gemini-3.6-flash`) and vision.inbox's default
        (`gemini-3.5-flash-lite`) both have rows in coglib.llm.MODELS today —
        exercised via the shared helper directly so this test doesn't depend
        on either integration's own config-store fixtures."""
        with caplog.at_level(logging.WARNING, logger="app.services.ai_roles"):
            ai_roles._warn_if_unrated("stt.memo", "gemini-3.6-flash")

        assert not [r for r in caplog.records if "no rate row" in r.message]
