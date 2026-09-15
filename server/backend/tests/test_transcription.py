"""Tests for the `transcription` capability and the inbox queue step that drives it.

Priorities here, in order:

  1. **Idempotency**, because retrying is billable. A daemon restart or a
     re-synced mtime must not re-transcribe a file that's already done.
  2. **The embedded-transcript path**, because it's the difference between
     paying for ~70 files and paying for ~342 on a backfill.
  3. **Gemini-only resolution through the role registry** (W2 chunk 2):
     `stt.memo` is the one place "what model transcribes memos" is answered.
     The OpenAI path (`client.py`, `DIARIZING_MODELS`, `_resolve_provider`'s
     branching) is gone — see `facade.py`'s module docstring — so the request
     shaping / error classification / speaker-markdown tests that used to
     live here (all OpenAI-specific) are gone with it. Gemini's own request
     shaping has no equivalent branching to test: one model family, one shape.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.errors import PermanentError
from app.integrations.transcription import dictionary, embedded
from app.integrations.transcription import gemini as _gemini
from app.integrations.transcription.facade import FACADE, TranscriptResult
from app.services import ai_roles


# ---------------------------------------------------------------------------
# Synthetic ISO-BMFF with a tsrp atom
# ---------------------------------------------------------------------------

def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + box_type + payload


def _memo(tmp_path, tsrp_json: dict | None, name: str = "memo.m4a") -> Path:
    """A minimal moov>trak>udta[>tsrp] file, mirroring Voice Memos' layout."""
    udta_payload = b""
    if tsrp_json is not None:
        udta_payload = _box(b"tsrp", json.dumps(tsrp_json).encode("utf-8"))
    body = _box(b"ftyp", b"M4A " + struct.pack(">I", 0)) + _box(
        b"moov", _box(b"trak", _box(b"udta", udta_payload))
    )
    path = tmp_path / name
    path.write_bytes(body)
    return path


class TestEmbeddedTranscript:
    def test_separated_runs_shape(self, tmp_path):
        path = _memo(tmp_path, {
            "attributedString": {"runs": ["Hello ", 0, "world", 1]},
            "locale": {"identifier": "en_IE"},
        })
        text, locale = embedded.read_embedded_transcript(path)
        assert text == "Hello world"
        assert locale == "en_IE"

    def test_interleaved_runs_shape(self, tmp_path):
        """Apple ships two shapes; both must parse."""
        path = _memo(tmp_path, {
            "attributedString": ["Pick up ", {"b": 1}, "the brackets"],
        })
        text, _ = embedded.read_embedded_transcript(path)
        assert text == "Pick up the brackets"

    def test_no_tsrp_atom_returns_none(self, tmp_path):
        """Memos synced from an iPhone usually have no atom at all."""
        text, _ = embedded.read_embedded_transcript(_memo(tmp_path, None))
        assert text is None

    def test_empty_placeholder_returns_empty_not_none(self, tmp_path):
        """Distinct from absent: the device tried and produced nothing. Callers
        treat both as unusable, but the distinction is real."""
        path = _memo(tmp_path, {"attributedString": {"runs": []}})
        text, _ = embedded.read_embedded_transcript(path)
        assert text == ""
        assert text is not None

    def test_malformed_json_does_not_raise(self, tmp_path):
        body = _box(b"moov", _box(b"trak", _box(b"udta", _box(b"tsrp", b"{not json"))))
        path = tmp_path / "bad.m4a"
        path.write_bytes(body)
        assert embedded.read_embedded_transcript(path) == (None, None)

    def test_malformed_box_size_terminates(self, tmp_path):
        """A size smaller than its own header must not spin forever."""
        path = tmp_path / "bad2.m4a"
        path.write_bytes(struct.pack(">I", 2) + b"moov" + b"\x00" * 8)
        assert embedded.read_embedded_transcript(path) == (None, None)

    def test_missing_file_does_not_raise(self, tmp_path):
        assert embedded.read_embedded_transcript(tmp_path / "nope.m4a") == (None, None)


# ---------------------------------------------------------------------------
# Vault-derived proper-noun dictionary
# ---------------------------------------------------------------------------

def _person(vault: Path, filename: str, body: str) -> None:
    people = vault / "People"
    people.mkdir(parents=True, exist_ok=True)
    (people / filename).write_text(body, encoding="utf-8")


class TestDictionary:
    def test_collects_title_and_block_aliases(self, tmp_path):
        _person(tmp_path, "Kev O Sullivan.md", """---
title: Kev O Sullivan
type: note
aliases:
  - Kev
  - Kev O'Sullivan
tags: [person]
---

Body text.
""")
        terms = dictionary.collect_vault_terms(tmp_path)
        assert "Kev O Sullivan" in terms
        assert "Kev O'Sullivan" in terms

    def test_collects_inline_list_aliases(self, tmp_path):
        _person(tmp_path, "Crannarc.md", '---\ntitle: Crannarc\naliases: ["CanArk", "Cranarch"]\n---\n')
        terms = dictionary.collect_vault_terms(tmp_path)
        assert "CanArk" in terms and "Cranarch" in terms

    def test_known_mistranscription_reaches_the_prompt(self, tmp_path):
        """The whole point: aliases record what the transcriber gets wrong, so
        feeding them back is what stops it happening again."""
        _person(tmp_path, "Crannarc.md", '---\ntitle: Crannarc\naliases: ["CanArk"]\n---\n')
        prompt = dictionary.build_prompt(tmp_path)
        assert "CanArk" in prompt and "Crannarc" in prompt

    def test_filename_counts_even_without_frontmatter(self, tmp_path):
        """Vault convention is filename == canonical name."""
        _person(tmp_path, "Vinny Zbynek Novotny.md", "no frontmatter here\n")
        assert "Vinny Zbynek Novotny" in dictionary.collect_vault_terms(tmp_path)

    def test_deduplicates_case_insensitively(self, tmp_path):
        _person(tmp_path, "Naoise.md", '---\ntitle: Naoise\naliases: ["naoise", "NAOISE"]\n---\n')
        terms = dictionary.collect_vault_terms(tmp_path)
        assert len([t for t in dictionary._clean(terms) if t.casefold() == "naoise"]) == 1

    def test_drops_short_single_tokens(self, tmp_path):
        """Two-letter aliases collide with ordinary words and hurt decoding."""
        _person(tmp_path, "Jo.md", '---\ntitle: Jo\naliases: ["J"]\n---\n')
        cleaned = dictionary._clean(dictionary.collect_vault_terms(tmp_path))
        assert "J" not in cleaned

    def test_keeps_short_multiword_terms(self, tmp_path):
        _person(tmp_path, "Ó Sé.md", "---\ntitle: Ó Sé\n---\n")
        assert "Ó Sé" in dictionary._clean(dictionary.collect_vault_terms(tmp_path))

    def test_caps_term_count(self, tmp_path):
        for i in range(dictionary.MAX_TERMS + 50):
            _person(tmp_path, f"Person{i:04d}.md", f"---\ntitle: Person{i:04d}\n---\n")
        prompt = dictionary.build_prompt(tmp_path)
        assert prompt.count(",") < dictionary.MAX_TERMS + 5

    def test_no_vault_gives_a_neutral_prompt(self):
        prompt = dictionary.build_prompt(None)
        assert "proper nouns" not in prompt
        assert prompt.strip()

    def test_missing_people_dir_is_not_an_error(self, tmp_path):
        assert dictionary.collect_vault_terms(tmp_path) == []

    def test_extra_terms_are_appended(self, tmp_path):
        prompt = dictionary.build_prompt(tmp_path, ["Poolbeg", "Riverside"])
        assert "Poolbeg" in prompt and "Riverside" in prompt


# ---------------------------------------------------------------------------
# Facade: which source wins, and what happens when things fail
# ---------------------------------------------------------------------------

def _cfg(**overrides):
    base = {
        "gemini_api_key": "g-test",
        "gemini_model": "gemini-3.6-flash",
        "gemini_max_file_mb": 18,
        "dictionary_from_vault": True,
        "extra_dictionary_terms": [],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def facade_cfg(monkeypatch):
    def setter(**overrides):
        cfg = _cfg(**overrides)
        # facade.py binds `plugin_config` at import time (`from ... import
        # plugin_config`), so its own name has to be patched directly.
        # ai_roles.py imports it inside the function body instead (a fresh
        # lookup on every call), so patching the module attribute it reads
        # from is enough — see ai_roles.py's module docstring on why storage
        # for stt.memo/vision.inbox is integration_config, read this way.
        monkeypatch.setattr("app.integrations.transcription.facade.plugin_config", lambda name: cfg)
        monkeypatch.setattr("app.plugin.config_store.plugin_config", lambda name: cfg)
        return cfg
    return setter


class TestFacade:
    def test_available_reflects_the_key(self, facade_cfg):
        facade_cfg()
        assert FACADE.available() is True
        facade_cfg(gemini_api_key="")
        assert FACADE.available() is False

    def test_prefer_embedded_avoids_paying(self, tmp_path, facade_cfg, monkeypatch):
        """The backfill path: use the free transcript wherever one exists."""
        facade_cfg()
        called = []
        monkeypatch.setattr(_gemini, "transcribe", lambda *a, **k: called.append(1) or _gemini.GeminiTranscript(text="paid"))

        path = _memo(tmp_path, {"attributedString": {"runs": ["free text"]}})
        result = FACADE.transcribe(path, prefer="embedded")

        assert result.source == "embedded"
        assert result.text == "free text"
        assert called == [], "must not call the paid provider when a free transcript exists"

    def test_prefer_embedded_still_pays_when_there_is_no_transcript(self, tmp_path, facade_cfg, monkeypatch):
        """Which is how 'the 70 untranscribed' falls out without a client-side list."""
        facade_cfg()
        monkeypatch.setattr(_gemini, "transcribe", lambda *a, **k: _gemini.GeminiTranscript(text="paid text"))
        result = FACADE.transcribe(_memo(tmp_path, None), prefer="embedded")
        assert result.source == "gemini"
        assert result.text == "paid text"

    def test_default_prefer_ignores_an_existing_embedded_transcript(self, tmp_path, facade_cfg, monkeypatch):
        facade_cfg()
        monkeypatch.setattr(_gemini, "transcribe", lambda *a, **k: _gemini.GeminiTranscript(text="better text"))
        path = _memo(tmp_path, {"attributedString": {"runs": ["rougher text"]}})
        result = FACADE.transcribe(path)
        assert result.source == "gemini"
        assert result.text == "better text"

    def test_prefer_openai_behaves_like_the_default(self, tmp_path, facade_cfg, monkeypatch):
        """`prefer='openai'` is still accepted from old callers — Gemini is
        the only paid provider now, so it behaves exactly like the default."""
        facade_cfg()
        monkeypatch.setattr(_gemini, "transcribe", lambda *a, **k: _gemini.GeminiTranscript(text="better text"))
        path = _memo(tmp_path, {"attributedString": {"runs": ["rougher text"]}})
        result = FACADE.transcribe(path, prefer="openai")
        assert result.source == "gemini"
        assert result.text == "better text"

    def test_provider_failure_falls_back_to_embedded(self, tmp_path, facade_cfg, monkeypatch):
        """A network blip should still yield something rather than nothing."""
        from app.errors import TransientError

        facade_cfg()

        def boom(*a, **k):
            raise TransientError("down")

        monkeypatch.setattr(_gemini, "transcribe", boom)
        path = _memo(tmp_path, {"attributedString": {"runs": ["rougher text"]}})
        result = FACADE.transcribe(path)

        assert result.source == "embedded"
        assert result.text == "rougher text"
        assert result.error and "down" in result.error

    def test_no_key_uses_embedded(self, tmp_path, facade_cfg):
        facade_cfg(gemini_api_key="")
        path = _memo(tmp_path, {"attributedString": {"runs": ["on-device"]}})
        result = FACADE.transcribe(path)
        assert result.source == "embedded"
        assert "no transcription provider configured" in (result.error or "")

    def test_nothing_available_reports_source_none(self, tmp_path, facade_cfg):
        facade_cfg(gemini_api_key="")
        result = FACADE.transcribe(_memo(tmp_path, None))
        assert result.source == "none"
        assert not result.ok

    def test_unbound_role_falls_back_to_embedded_without_raising(self, tmp_path, facade_cfg):
        """A key is set but the model string is empty — `RoleNotBoundError`,
        caught inside `transcribe()` (never raises), same shape as no key at
        all except the message names the role."""
        facade_cfg(gemini_model="")
        path = _memo(tmp_path, {"attributedString": {"runs": ["on-device"]}})
        result = FACADE.transcribe(path)
        assert result.source == "embedded"
        assert "stt.memo" in (result.error or "")

    def test_never_raises_on_an_unexpected_error(self, tmp_path, facade_cfg, monkeypatch):
        """Callers are queue walkers; one bad file must not abort the batch."""
        facade_cfg()

        def boom(*a, **k):
            raise RuntimeError("something odd")

        monkeypatch.setattr(_gemini, "transcribe", boom)
        result = FACADE.transcribe(_memo(tmp_path, None))
        assert result.source == "none"
        assert "something odd" in (result.error or "")

    def test_dictionary_can_be_switched_off(self, tmp_path, facade_cfg, monkeypatch):
        facade_cfg(dictionary_from_vault=False)
        seen = {}
        monkeypatch.setattr(
            _gemini, "transcribe",
            lambda path, **kw: seen.update(kw) or _gemini.GeminiTranscript(text="x"),
        )
        _person(tmp_path, "Crannarc.md", "---\ntitle: Crannarc\n---\n")
        FACADE.transcribe(_memo(tmp_path, None), vault_root=tmp_path)
        assert "Crannarc" not in seen["prompt"]

    def test_resolved_model_is_passed_through(self, tmp_path, facade_cfg, monkeypatch):
        """The model the paid call actually uses comes from the role
        registry, not straight off cfg — proves the one-place-to-look
        property end to end."""
        facade_cfg(gemini_model="gemini-3.7-flash")
        seen = {}
        monkeypatch.setattr(
            _gemini, "transcribe",
            lambda path, **kw: seen.update(kw) or _gemini.GeminiTranscript(text="x"),
        )
        FACADE.transcribe(_memo(tmp_path, None))
        assert seen["model"] == "gemini-3.7-flash"


class TestResultSemantics:
    def test_empty_text_with_no_error_is_silence_not_failure(self):
        assert TranscriptResult(text="", source="none").ok is False

    def test_ok_requires_text(self):
        assert TranscriptResult(text="hi", source="gemini").ok is True


class TestManifestWiring:
    def test_provides_the_capability_inbox_depends_on(self):
        from app.integrations.inbox.manifest import MANIFEST as INBOX
        from app.integrations.transcription.manifest import MANIFEST as TR

        assert TR.provides == ["transcription.audio"]
        assert "transcription.audio" in INBOX.depends_on

    def test_transcription_owns_no_tables_and_no_schedule(self):
        """It never decides *when* to transcribe — that's the caller's cost."""
        from app.integrations.transcription.manifest import MANIFEST as TR

        assert TR.models == []
        assert TR.schedule is None
        assert TR.background_tasks == []

    def test_inbox_transcription_cron_is_separate_from_its_sync(self):
        from app.integrations.inbox.manifest import MANIFEST as INBOX

        task = next(t for t in INBOX.background_tasks if t.name == "inbox_transcribe_pending")
        assert task.kind == "cron"
        assert task.cron != INBOX.schedule, (
            "transcription must not share the hourly sync cadence — it's the "
            "difference between a voice note being readable in 5 minutes or 60"
        )

    def test_gemini_key_is_secret_with_no_default(self):
        from app.integrations.transcription.manifest import MANIFEST as TR

        spec = TR.config_schema["gemini_api_key"]
        assert spec.secret is True
        assert spec.default is None

    def test_openai_only_fields_are_gone(self):
        """W2 chunk 2: transcription is Gemini-only. Grepped repo-wide first
        (see manifest.py's module docstring) — nothing outside transcription
        itself and its own tests read `openai_api_key`."""
        from app.integrations.transcription.manifest import MANIFEST as TR

        for gone in ("openai_api_key", "provider", "model", "max_file_mb"):
            assert gone not in TR.config_schema

    def test_client_module_is_gone(self):
        with pytest.raises(ModuleNotFoundError):
            import app.integrations.transcription.client  # noqa: F401


class TestGeminiProvider:
    def test_m4a_maps_to_a_supported_mime(self):
        """Apple Voice Memos are .m4a, which Google does not document as
        supported — verified working 2026-08-07, so the mapping is explicit
        rather than left to mimetypes.guess_type."""
        assert _gemini.mime_for(Path("memo.m4a")) == "audio/aac"
        assert _gemini.mime_for(Path("partial.qta")) == "audio/aac"

    def test_video_is_accepted(self):
        assert _gemini.mime_for(Path("clip.mp4")) == "video/mp4"

    def test_unsupported_extension_returns_none(self):
        assert _gemini.mime_for(Path("notes.txt")) is None

    def test_missing_key_is_permanent(self, tmp_path):
        path = tmp_path / "a.m4a"
        path.write_bytes(b"x")
        with pytest.raises(PermanentError):
            _gemini.transcribe(path, api_key="", model="m")

    def test_unsupported_container_is_permanent(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_bytes(b"x")
        with pytest.raises(PermanentError):
            _gemini.transcribe(path, api_key="k", model="m")

    def test_oversize_is_uploaded_not_refused(self, tmp_path, monkeypatch):
        """Until 2026-09-02 this test asserted the opposite — that anything over
        the inline cap was a PermanentError before any call. That is the rule
        that buried a 54-minute memo. Over the cap now means the Files API;
        only an absurd size (MAX_UPLOAD_MB) is refused up front, and that
        refusal happens before the client is even constructed."""
        path = tmp_path / "big.m4a"
        path.write_bytes(b"0" * (2 * 1024 * 1024))
        monkeypatch.setattr(_gemini, "MAX_UPLOAD_MB", 1)
        monkeypatch.setattr("google.genai.Client", lambda **k: pytest.fail("client built for a refused file"))
        with pytest.raises(PermanentError, match="upload ceiling"):
            _gemini.transcribe(path, api_key="k", model="m", max_file_mb=1)

    def test_prompt_includes_dictionary_terms(self):
        prompt = _gemini.build_prompt("Crannarc, Naoise")
        assert "Crannarc" in prompt
        assert "transcript" in prompt

    def test_prompt_without_dictionary_is_just_the_rules(self):
        assert _gemini.build_prompt("") == _gemini.build_prompt("   ")


# ---------------------------------------------------------------------------
# Structured output — {title, speakers, transcript}, ported from Tines
# ---------------------------------------------------------------------------

class _FakeGenaiResponse:
    def __init__(self, text, usage_metadata=None):
        self.text = text
        self.usage_metadata = usage_metadata


class _FakeGenaiModels:
    """Records the call so tests can assert on request shape, and returns
    whatever text the test hands it."""

    def __init__(self, response_text):
        self.response_text = response_text
        self.calls = []

    def generate_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return _FakeGenaiResponse(self.response_text)


class _FakeGenaiClient:
    def __init__(self, models):
        self.models = models

    def __call__(self, *, api_key):
        # `genai.Client(api_key=...)` is called fresh inside transcribe();
        # this makes the stub usable as a drop-in for `genai.Client` itself.
        return self


@pytest.fixture
def gemini_call(monkeypatch, tmp_path):
    """Stub `google.genai.Client` so `_gemini.transcribe()` runs its real
    request-building and response-parsing code against a canned response,
    with no network call. `google-genai` is a real installed dependency here
    (unlike `test_gemini_embedding_provider.py`'s sys.modules stub), so only
    `Client` needs replacing — `types.Part`/`types.Schema`/
    `types.GenerateContentConfig` are the genuine SDK classes."""

    def _setup(response_text: str):
        models = _FakeGenaiModels(response_text)
        monkeypatch.setattr("google.genai.Client", _FakeGenaiClient(models))
        # Also silence the usage-ledger write — not under test here, and it
        # touches the real DB layer via app.services.ai_ledger.
        monkeypatch.setattr(_gemini, "_record_usage", lambda *a, **k: None)
        path = tmp_path / "memo.m4a"
        path.write_bytes(b"fake-audio-bytes")
        return path, models

    return _setup


class TestStructuredOutput:
    def test_well_formed_response_populates_title_and_speakers(self, gemini_call):
        path, models = gemini_call(json.dumps({
            "title": "Weekend plans discussion",
            "speakers": 2,
            "transcript": "A: Are we still on for Saturday?\nB: Yep.",
        }))

        result = _gemini.transcribe(path, api_key="k", model="gemini-3.7-flash")

        assert result.text == "A: Are we still on for Saturday?\nB: Yep."
        assert result.title == "Weekend plans discussion"
        assert result.speakers == 2

    def test_request_asks_for_the_json_schema(self, gemini_call):
        """The whole point of the port: the request must actually ask for
        structured output, not just hope the model volunteers JSON."""
        path, models = gemini_call(json.dumps({
            "title": "t", "speakers": 1, "transcript": "hello",
        }))

        _gemini.transcribe(path, api_key="k", model="gemini-3.7-flash")

        config = models.calls[0]["config"]
        assert config.response_mime_type == "application/json"
        assert set(config.response_schema.required) == {"title", "speakers", "transcript"}
        assert config.max_output_tokens == 65536

    def test_malformed_json_degrades_to_text_only(self, gemini_call):
        """Not valid JSON at all — must not raise, must not lose the words."""
        path, _ = gemini_call("A: this never closed its brace {")

        result = _gemini.transcribe(path, api_key="k", model="gemini-3.7-flash")

        assert result.text == "A: this never closed its brace {"
        assert result.title is None
        assert result.speakers is None

    def test_missing_transcript_field_degrades_to_raw_text(self, gemini_call):
        """Valid JSON, but not the shape asked for — the transcript itself
        must not be dropped just because the wrapper is wrong."""
        raw = json.dumps({"title": "t", "speakers": 1})
        path, _ = gemini_call(raw)

        result = _gemini.transcribe(path, api_key="k", model="gemini-3.7-flash")

        assert result.text == raw
        assert result.title is None
        assert result.speakers is None

    def test_non_object_json_degrades_to_raw_text(self, gemini_call):
        path, _ = gemini_call(json.dumps(["not", "an", "object"]))

        result = _gemini.transcribe(path, api_key="k", model="gemini-3.7-flash")

        assert result.text == json.dumps(["not", "an", "object"])
        assert result.title is None

    def test_blank_title_is_treated_as_missing(self, gemini_call):
        path, _ = gemini_call(json.dumps({
            "title": "   ", "speakers": 1, "transcript": "hi",
        }))

        result = _gemini.transcribe(path, api_key="k", model="gemini-3.7-flash")
        assert result.title is None

    def test_boolean_speakers_is_not_mistaken_for_a_count(self, gemini_call):
        """`bool` is a subclass of `int` in Python — a stray `true` in this
        field must not silently become `speakers=1`."""
        path, _ = gemini_call(json.dumps({
            "title": "t", "speakers": True, "transcript": "hi",
        }))

        result = _gemini.transcribe(path, api_key="k", model="gemini-3.7-flash")
        assert result.speakers is None

    def test_empty_response_is_silence_not_an_exception(self, gemini_call):
        path, _ = gemini_call("")
        result = _gemini.transcribe(path, api_key="k", model="gemini-3.7-flash")
        assert result.text == ""
        assert result.title is None
        assert result.speakers is None

    def test_facade_carries_title_and_speakers_through(self, tmp_path, facade_cfg, monkeypatch):
        """End to end: the facade's `TranscriptResult` exposes what the
        gemini layer parsed, not just the bare text."""
        facade_cfg()
        monkeypatch.setattr(
            _gemini, "transcribe",
            lambda *a, **k: _gemini.GeminiTranscript(
                text="A: hi\nB: hi", title="A quick hello", speakers=2,
            ),
        )
        result = FACADE.transcribe(_memo(tmp_path, None))
        assert result.source == "gemini"
        assert result.title == "A quick hello"
        assert result.speakers == 2

    def test_facade_defaults_title_and_speakers_to_none_for_other_sources(self, tmp_path, facade_cfg):
        """The embedded (on-device) path never had structured output at all
        — its result must not accidentally inherit stale values."""
        facade_cfg()
        path = _memo(tmp_path, {"attributedString": {"runs": ["on device"]}})
        result = FACADE.transcribe(path, prefer="embedded")
        assert result.title is None
        assert result.speakers is None


# ---------------------------------------------------------------------------
# Role registry — stt.memo
# ---------------------------------------------------------------------------

class TestSttMemoRole:
    def test_resolves_the_configured_model(self, monkeypatch):
        cfg = _cfg(gemini_model="gemini-3.7-flash")
        monkeypatch.setattr("app.plugin.config_store.plugin_config", lambda name: cfg)
        binding = ai_roles.resolve_stt_memo()
        assert binding.role == "stt.memo"
        assert binding.provider == "google"
        assert binding.model == "gemini-3.7-flash"

    def test_empty_binding_is_a_loud_error(self, monkeypatch):
        cfg = _cfg(gemini_model="")
        monkeypatch.setattr("app.plugin.config_store.plugin_config", lambda name: cfg)
        with pytest.raises(ai_roles.RoleNotBoundError):
            ai_roles.resolve_stt_memo()

    def test_resolve_dispatches_to_the_same_binding(self, monkeypatch):
        cfg = _cfg()
        monkeypatch.setattr("app.plugin.config_store.plugin_config", lambda name: cfg)
        assert ai_roles.resolve("stt.memo") == ai_roles.resolve_stt_memo()


class TestMimeSniffingWithoutExtension:
    """An iOS voice memo arrives named after the memo, with no extension.

    Regression guard for a real failure on 2026-08-29: a 10m53s recording
    ingested as `Riverside 16` (the memo's title — a Shortcut's filename field is
    the title, not a filename) was rejected as "not a Gemini-supported
    container", despite `inbox/scan.py` having already sniffed it as audio and
    read its duration. Name-based typing was the only thing consulted here.
    """

    def _bmff(self, tmp_path, brand: bytes, name: str):
        p = tmp_path / name
        # First four bytes are the ftyp BOX LENGTH, not a constant — a sniffer
        # that matches on them only works for one particular box size.
        p.write_bytes((32).to_bytes(4, "big") + b"ftyp" + brand + b"\x00" * 24)
        return p

    def test_extensionless_m4a_is_recognised(self, tmp_path):
        from app.integrations.transcription.gemini import mime_for
        assert mime_for(self._bmff(tmp_path, b"M4A ", "Riverside 16")) == "audio/aac"

    def test_box_length_is_not_treated_as_magic(self, tmp_path):
        from app.integrations.transcription.gemini import mime_for
        for size in (24, 32, 40, 512):
            p = tmp_path / f"memo-{size}"
            p.write_bytes(size.to_bytes(4, "big") + b"ftyp" + b"M4A " + b"\x00" * 24)
            assert mime_for(p) == "audio/aac", f"failed at box length {size}"

    def test_extension_still_wins_when_present(self, tmp_path):
        from app.integrations.transcription.gemini import mime_for
        assert mime_for(self._bmff(tmp_path, b"M4A ", "memo.wav")) == "audio/wav"

    def test_other_containers(self, tmp_path):
        from app.integrations.transcription.gemini import mime_for
        cases = {
            b"RIFF" + b"\x00" * 4 + b"WAVE": "audio/wav",
            b"OggS" + b"\x00" * 8: "audio/ogg",
            b"ID3" + b"\x00" * 9: "audio/mp3",
            b"fLaC" + b"\x00" * 8: "audio/flac",
        }
        for magic, expected in cases.items():
            p = tmp_path / f"x{len(magic)}{expected[-3:]}"
            p.write_bytes(magic + b"\x00" * 16)
            assert mime_for(p) == expected

    def test_a_text_file_is_still_rejected(self, tmp_path):
        from app.integrations.transcription.gemini import mime_for
        p = tmp_path / "notes"
        p.write_bytes(b"just some plain text here")
        assert mime_for(p) is None


class TestWiderVaultDictionary:
    """Proper nouns mined from the whole vault, not just People notes.

    Measured on a real 10m53s memo (2026-08-29): of 47 proper nouns in its
    transcript, People notes alone covered 9. `Wicklow` occurred in 51 vault
    notes, `UniFi` in 42 — recurring household vocabulary with no People note,
    so unreachable by the original source.
    """

    @pytest.fixture(autouse=True)
    def _wordlist(self, monkeypatch):
        """Pin the English wordlist.

        These tests used to read the host's `/usr/share/dict/words`, which
        passed on macOS and failed on CI's Linux runner where the file does not
        exist — the same absence that would have made the whole feature a
        silent no-op in the `python:3.12-slim` image. The behaviour under test
        is the *filtering*, not the host's dictionary, so it is supplied here
        and the real-wordlist question belongs to the Dockerfile.
        """
        from app.integrations.transcription import dictionary as d
        d._english_words.cache_clear()
        words = frozenset({
            "focus", "backlog", "sleep", "coffee", "meeting", "note", "task",
            "aisle", "niall", "tiff", "polestar", "aisling", "roast",
        })
        monkeypatch.setattr(d, "_english_words", lambda: words)

    def _vault(self, tmp_path, notes: dict[str, str]):
        for name, body in notes.items():
            p = tmp_path / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        return tmp_path

    def test_recurring_nonenglish_term_is_mined(self, tmp_path):
        from app.integrations.transcription import dictionary as d
        d._corpus_cache.clear()
        notes = {f"n{i}.md": "We drove to Malahide today." for i in range(6)}
        terms = d.collect_corpus_terms(self._vault(tmp_path, notes), min_notes=5)
        assert "Malahide" in terms

    def test_ordinary_english_is_not_mined(self, tmp_path):
        """The filter that separates a name from this vault's own headings."""
        from app.integrations.transcription import dictionary as d
        d._corpus_cache.clear()
        notes = {f"n{i}.md": "Focus\nBacklog\nSleep\nCoffee\nMeetings\nNotes\nTasks" for i in range(9)}
        terms = d.collect_corpus_terms(self._vault(tmp_path, notes), min_notes=5)
        assert terms == [], f"expected no terms, got {terms}"

    def test_inflections_are_recognised_as_english(self, tmp_path):
        from app.integrations.transcription import dictionary as d
        assert d._is_ordinary_english("Meetings") is True   # plural of a word
        assert d._is_ordinary_english("Malahide") is False  # a real place

    def test_a_name_that_is_also_a_dictionary_word_is_filtered(self, tmp_path):
        """The known, unavoidable cost of the English filter — pinned, not hidden.

        `/usr/share/dict/words` is web2: old and very inclusive. `niall`,
        `tiff`, `aisling` and `polestar` are all genuinely in it, so mining
        cannot distinguish them from ordinary words and drops them. There is no
        threshold that fixes this.

        The escape hatch is the design, not a workaround: People notes and
        `extra_dictionary_terms` are never passed through this filter, so a
        name it eats is recovered by putting the person where they belong.
        """
        from app.integrations.transcription import dictionary as d
        for name in ("Niall", "Tiff", "Aisling", "Polestar"):
            assert d._is_ordinary_english(name) is True, f"{name} unexpectedly passed"

        # ...and the escape hatch actually works.
        (tmp_path / "People").mkdir()
        (tmp_path / "People" / "Niall.md").write_text("---\ntitle: Niall\n---\n")
        d._corpus_cache.clear()
        assert "Niall" in d.build_prompt(tmp_path)
        d._corpus_cache.clear()
        assert "Polestar" in d.build_prompt(tmp_path, ["Polestar"])

    def test_one_note_jargon_is_ignored(self, tmp_path):
        """Document frequency, not raw count: forty mentions in one note is
        that note's jargon, not household vocabulary."""
        from app.integrations.transcription import dictionary as d
        d._corpus_cache.clear()
        notes = {"one.md": "Zorblatt " * 40}
        assert d.collect_corpus_terms(self._vault(tmp_path, notes), min_notes=5) == []

    def test_stversions_snapshots_are_skipped(self, tmp_path):
        """Syncthing keeps a snapshot per revision; counting them would reward
        notes that churn (and once outranked live files in the vault index)."""
        from app.integrations.transcription import dictionary as d
        d._corpus_cache.clear()
        notes = {f".stversions/n{i}.md": "Malahide again" for i in range(9)}
        assert d.collect_corpus_terms(self._vault(tmp_path, notes), min_notes=5) == []

    def test_people_notes_win_on_truncation(self, tmp_path):
        """People notes are authoritative — their aliases are actual recorded
        mis-transcriptions, so they must not be crowded out by mined guesses."""
        from app.integrations.transcription import dictionary as d
        d._corpus_cache.clear()
        (tmp_path / "People").mkdir()
        (tmp_path / "People" / "Grainne Ui Mhaolagain.md").write_text("---\ntitle: Grainne Ui Mhaolagain\n---\n")
        for i in range(9):
            (tmp_path / f"n{i}.md").write_text("Malahide " * 3)
        prompt = d.build_prompt(tmp_path)
        assert "Grainne Ui Mhaolagain" in prompt


# ─── Gemini transport: inline below the cap, Files API above it ────────────


class _FakeUpload:
    def __init__(self, name="files/abc", state="ACTIVE"):
        self.name, self.uri = name, f"https://generativelanguage.googleapis.com/v1beta/{name}"
        self.state = type("S", (), {"name": state})()


class _FakeGenai:
    """Just enough of google.genai.Client to see which transport was used."""

    def __init__(self, api_key=None):
        self.calls = _FakeGenai.calls
        self.files = self
        self.models = self

    calls: dict = {}

    def upload(self, file, config):
        self.calls.setdefault("upload", []).append(file)
        return _FakeUpload(state=self.calls.get("first_state", "ACTIVE"))

    def get(self, name):
        return _FakeUpload(name=name, state="ACTIVE")

    def delete(self, name):
        self.calls.setdefault("delete", []).append(name)

    def generate_content(self, model, contents, config):
        self.calls["contents"] = contents
        return type("R", (), {"text": '{"text": "hello", "title": "t", "speakers": 1}', "usage_metadata": None})()


@pytest.fixture
def fake_genai(monkeypatch):
    _FakeGenai.calls = {}
    monkeypatch.setattr("google.genai.Client", _FakeGenai)
    monkeypatch.setattr("app.integrations.transcription.gemini._record_usage", lambda *a, **k: None)
    monkeypatch.setattr("app.integrations.transcription.gemini._UPLOAD_POLL_S", 0)
    return _FakeGenai.calls


def _audio(tmp_path, size):
    p = tmp_path / "memo.m4a"
    p.write_bytes(b"\x00" * size)
    return p


class TestGeminiTransport:
    """A 54-minute, 25 MB memo was refused as PermanentError on 2026-09-02 and
    buried: the inline path was the only path. Above the inline cap the bytes
    now go through the Files API; the caller sees the same transcript."""

    def test_small_file_rides_inline(self, tmp_path, fake_genai):
        from app.integrations.transcription import gemini
        out = gemini.transcribe(_audio(tmp_path, 1024), api_key="k", model="m", max_file_mb=1)
        assert "hello" in out.text
        assert "upload" not in fake_genai
        assert fake_genai["contents"][0].inline_data is not None

    def test_large_file_goes_through_the_files_api(self, tmp_path, fake_genai):
        from app.integrations.transcription import gemini
        big = _audio(tmp_path, 3 * 1024 * 1024)
        out = gemini.transcribe(big, api_key="k", model="m", max_file_mb=2)
        assert "hello" in out.text
        assert fake_genai["upload"] == [str(big)]
        part = fake_genai["contents"][0]
        assert part.file_data is not None and part.file_data.file_uri.endswith("files/abc")
        assert fake_genai["delete"] == ["files/abc"], "the upload is cleaned up afterwards"

    def test_processing_is_polled_until_active(self, tmp_path, fake_genai):
        from app.integrations.transcription import gemini
        fake_genai["first_state"] = "PROCESSING"
        out = gemini.transcribe(_audio(tmp_path, 3 * 1024 * 1024), api_key="k", model="m", max_file_mb=2)
        assert "hello" in out.text

    def test_absurd_size_is_still_refused(self, tmp_path, fake_genai, monkeypatch):
        from app.errors import PermanentError
        from app.integrations.transcription import gemini
        monkeypatch.setattr(gemini, "MAX_UPLOAD_MB", 1)
        with pytest.raises(PermanentError, match="upload ceiling"):
            gemini.transcribe(_audio(tmp_path, 3 * 1024 * 1024), api_key="k", model="m", max_file_mb=2)
        assert "upload" not in fake_genai
