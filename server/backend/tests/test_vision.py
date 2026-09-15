"""vision capability — parser tolerance, format gating, and cost guards.

The expensive failures here are billing ones: a sweep that re-sends the same
image every 15 minutes, or a paid call against a file that could never work.
Those get tests before the happy path does.
"""

from pathlib import Path

import pytest

from app.errors import PermanentError
from app.integrations.vision import client
from app.integrations.vision.facade import FACADE, VisionResult, parse_response


# --- parser -----------------------------------------------------------------

def test_parses_the_three_fields():
    raw = (
        "SUMMARY: Electricity bill from Energia dated 3 September 2026.\n"
        "KIND: receipt\n"
        "TEXT: Energia\nAccount 12345678\nTotal due EUR 184.20\n"
    )
    summary, kind, text = parse_response(raw)

    assert summary == "Electricity bill from Energia dated 3 September 2026."
    assert kind == "receipt"
    assert "Account 12345678" in text
    assert "EUR 184.20" in text


def test_text_preserves_line_breaks():
    """Line structure is what makes a transcribed letter readable later."""
    _, _, text = parse_response("SUMMARY: A letter.\nKIND: letter\nTEXT: Line one\nLine two")
    assert text == "Line one\nLine two"


def test_summary_is_whitespace_collapsed():
    """It becomes a one-line inbox listing, so a wrapped model reply must flatten."""
    summary, _, _ = parse_response("SUMMARY: A long\n  summary that wrapped.\nKIND: photo\nTEXT: NONE")
    assert summary == "A long summary that wrapped."


def test_none_text_becomes_empty_not_the_literal_word():
    """Otherwise 'NONE' gets indexed as the document's contents."""
    _, _, text = parse_response("SUMMARY: A dog.\nKIND: photo\nTEXT: NONE")
    assert text == ""


def test_unknown_kind_falls_back_to_other():
    _, kind, _ = parse_response("SUMMARY: x\nKIND: invoice-ish\nTEXT: NONE")
    assert kind == "other"


@pytest.mark.parametrize("kind", ["document", "receipt", "label", "screenshot", "photo", "other"])
def test_every_prompted_kind_is_accepted(kind):
    """The enum in the prompt and the validation set must not drift apart —
    a valid answer silently downgraded to `other` loses the routing signal."""
    _, got, _ = parse_response(f"SUMMARY: x\nKIND: {kind}\nTEXT: NONE")
    assert got == kind


def test_retired_kinds_are_no_longer_accepted():
    """`letter`/`form` routed identically to `document` and only gave the model
    more ways to disagree with itself; they should fall through now."""
    for retired in ("letter", "form"):
        _, kind, _ = parse_response(f"SUMMARY: x\nKIND: {retired}\nTEXT: NONE")
        assert kind == "other"


def test_struck_through_markers_survive_transcription():
    """The whole point of the prompt change: a crossed-out line records a
    decision, so the marker has to reach the sidecar intact."""
    _, _, text = parse_response(
        "SUMMARY: A list.\nKIND: document\n"
        "TEXT: ham x 2\n[struck through]\ngranola x [struck through: 3] 4"
    )
    assert "[struck through]" in text
    assert "[struck through: 3]" in text


def test_illegible_is_not_collapsed_to_empty():
    """`[illegible]` means 'text present, unreadable'; NONE means 'no text'.
    Conflating them turns a known gap into a false negative in the archive."""
    _, _, text = parse_response("SUMMARY: A blurred sign.\nKIND: photo\nTEXT: [illegible]")
    assert text == "[illegible]"
    assert text != ""


def test_missing_kind_falls_back_to_other():
    _, kind, _ = parse_response("SUMMARY: x\nTEXT: NONE")
    assert kind == "other"


def test_unformatted_reply_is_kept_as_the_summary():
    """A usable description shouldn't be discarded over its formatting."""
    summary, kind, text = parse_response("This is a photo of a boiler pressure gauge.")
    assert summary == "This is a photo of a boiler pressure gauge."
    assert kind == "other"
    assert text == ""


def test_empty_reply_yields_nothing():
    assert parse_response("") == ("", "other", "")
    assert parse_response("   \n  ") == ("", "other", "")


# --- format gating ----------------------------------------------------------

@pytest.mark.parametrize(
    "name,expected",
    [
        ("scan.png", "image/png"),
        ("photo.JPG", "image/jpeg"),
        ("photo.jpeg", "image/jpeg"),
        ("shot.webp", "image/webp"),
        ("IMG_0001.HEIC", "image/heic"),   # iPhone default — no decoding needed
        ("frame.heif", "image/heif"),
        ("anim.gif", None),               # sniffs as image upstream, unsupported here
        ("notes.pdf", None),
    ],
)
def test_mime_gating(name, expected):
    assert client.mime_for(Path(name)) == expected


class TestExtensionlessSniffing:
    """Filename-blind format detection, because the real inputs have no filename.

    Every image here comes from the inbox, and the inbox's main producer posts a
    bare UUID with no extension — so the two filename routes in `mime_for` return
    None for the *normal* case, not an edge case. That made every phone-captured
    image fail as "not a Gemini-supported image format", and because
    `describe_pending` records `described_at` even on failure (deliberately —
    retrying is billable), each one was then permanently marked done.

    Found live 2026-08-17: exactly one image had ever reached vision, and it had
    failed this way. The production success rate was zero and nothing reported it.
    """

    @pytest.mark.parametrize(
        "head,expected",
        [
            (b"\x89PNG\r\n\x1a\n", "image/png"),
            (b"\xff\xd8\xff\xe0", "image/jpeg"),
            (b"RIFF\x00\x00\x00\x00WEBP", "image/webp"),
            (b"\x00\x00\x00\x18ftypheic", "image/heic"),
            (b"\x00\x00\x00\x18ftypmif1", "image/heif"),
        ],
    )
    def test_supported_formats_are_recognised_without_an_extension(
        self, tmp_path, head, expected
    ):
        p = tmp_path / "20260817-110133-10f7f27e"  # a real inbox filename
        p.write_bytes(head + b"\x00" * 32)
        assert client.mime_for(p) == expected

    @pytest.mark.parametrize(
        "head,why",
        [
            (b"RIFF\x00\x00\x00\x00WAVE", "a WAV is RIFF too — fourcc must be checked"),
            (b"\x00\x00\x00\x18ftypmp42", "an MP4 is ftyp too — brand must be checked"),
            (b"GIF89a", "gif sniffs as an image upstream but Gemini rejects it"),
            (b"%PDF-1.4", "a pdf is not an image"),
        ],
    )
    def test_lookalikes_are_still_refused(self, tmp_path, head, why):
        """Sniffing must not become permissive: these share a prefix with a
        supported format and sending one would be a paid call that fails."""
        p = tmp_path / "20260817-110133-10f7f27e"
        p.write_bytes(head + b"\x00" * 32)
        assert client.mime_for(p) is None, why

    def test_an_extension_still_wins_and_costs_no_read(self, tmp_path):
        """The suffix path stays first — a named file needs no disk read at all,
        so this is not a behaviour change for the Mac watcher's uploads."""
        assert client.mime_for(Path("never-created.png")) == "image/png"

    def test_a_missing_file_is_refused_not_raised(self, tmp_path):
        assert client.mime_for(tmp_path / "gone") is None


def test_unsupported_format_is_permanent_not_transient(tmp_path):
    """A .gif will never work — retrying it forever is the failure mode."""
    gif = tmp_path / "anim.gif"
    gif.write_bytes(b"GIF89a")

    with pytest.raises(PermanentError, match="not a Gemini-supported image format"):
        client.describe(gif, api_key="k", model="gemini-3.5-flash-lite")


def test_oversized_image_is_rejected_before_the_call(tmp_path):
    """Guards the 20MB inline ceiling — and never spends a call to learn it."""
    big = tmp_path / "huge.jpg"
    big.write_bytes(b"\xff\xd8\xff" + b"0" * (2 * 1024 * 1024))

    with pytest.raises(PermanentError, match="exceeds max_image_mb"):
        client.describe(big, api_key="k", model="gemini-3.5-flash-lite", max_image_mb=1)


# --- facade ----------------------------------------------------------------

def test_unavailable_without_a_key(monkeypatch):
    """`available()` gating is what keeps an unconfigured deployment quiet."""
    monkeypatch.setattr(
        "app.plugin.config_store.plugin_config",
        lambda name: type("C", (), {"gemini_api_key": ""})(),
    )
    assert FACADE.available() is False


def test_available_with_a_key(monkeypatch):
    monkeypatch.setattr(
        "app.plugin.config_store.plugin_config",
        lambda name: type("C", (), {"gemini_api_key": "test-key"})(),
    )
    assert FACADE.available() is True


def test_config_failure_reports_unavailable_rather_than_raising(monkeypatch):
    """A queue walker must not die because the config table blipped."""
    def boom(name):
        raise RuntimeError("config table unreachable")

    monkeypatch.setattr("app.plugin.config_store.plugin_config", boom)
    assert FACADE.available() is False


def test_describe_without_a_key_returns_a_result_not_an_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.plugin.config_store.plugin_config",
        lambda name: type("C", (), {"gemini_api_key": "", "model": "", "max_image_mb": 18})(),
    )
    result = FACADE.describe(tmp_path / "x.png")
    assert isinstance(result, VisionResult)
    assert not result.ok
    assert "gemini_api_key" in result.error


def test_client_error_becomes_a_result_not_an_exception(monkeypatch, tmp_path):
    """One bad image must not abort the batch — the reason rides on the file."""
    monkeypatch.setattr(
        "app.plugin.config_store.plugin_config",
        lambda name: type(
            "C", (), {"gemini_api_key": "k", "model": "gemini-3.5-flash-lite", "max_image_mb": 18}
        )(),
    )
    img = tmp_path / "anim.gif"
    img.write_bytes(b"GIF89a")

    result = FACADE.describe(img)
    assert not result.ok
    assert "not a Gemini-supported image format" in result.error
    assert result.model == "gemini-3.5-flash-lite"


def test_manifest_declares_the_capability_and_no_required_key():
    """`required=True` would gate the integration — and all of vision — off."""
    from app.integrations.vision.manifest import MANIFEST

    assert MANIFEST.provides == ["vision.image"]
    assert MANIFEST.config_schema["gemini_api_key"].required is False
    assert MANIFEST.config_schema["gemini_api_key"].secret is True
    assert MANIFEST.config_schema["model"].default == "gemini-3.5-flash-lite"
    assert MANIFEST.schedule is None  # driven by inbox's cron, not its own


def test_facade_reads_the_default_model_from_the_manifest(monkeypatch, tmp_path):
    """The facade's fallback and the manifest default must be one fact, not two
    copies. Hardcoded, they drift — and then an unconfigured deployment silently
    runs a different model from a configured one with nothing reporting it."""
    from app.integrations.vision.manifest import MANIFEST

    monkeypatch.setattr(
        "app.plugin.config_store.plugin_config",
        # model deliberately blank, so the fallback is what gets used
        lambda name: type(
            "C", (), {"gemini_api_key": "k", "model": "", "max_image_mb": 18}
        )(),
    )
    img = tmp_path / "anim.gif"  # rejected before any call, but after model resolution
    img.write_bytes(b"GIF89a")

    assert FACADE.describe(img).model == MANIFEST.config_schema["model"].default


def test_inbox_declares_the_dependency():
    """`validate.py` fails boot on an unresolvable depends_on — pin it here too."""
    from app.integrations.inbox.manifest import MANIFEST

    assert "vision.image" in MANIFEST.depends_on
    assert any(t.name == "inbox_describe_pending" for t in MANIFEST.background_tasks)


# --- role registry: vision.inbox (W2 chunk 2) -------------------------------

def test_vision_inbox_role_resolves_the_configured_model(monkeypatch):
    from app.services import ai_roles

    monkeypatch.setattr(
        "app.plugin.config_store.plugin_config",
        lambda name: type("C", (), {"model": "gemini-3.7-flash"})(),
    )
    binding = ai_roles.resolve_vision_inbox()
    assert binding.role == "vision.inbox"
    assert binding.provider == "google"
    assert binding.model == "gemini-3.7-flash"


def test_vision_inbox_role_uses_the_fallback_the_facade_supplies(monkeypatch):
    """`ai_roles.py` can't import vision's manifest itself (kernel/integration
    import boundary — `tests/test_kernel_import_guard.py`), so `facade.py`
    passes its own manifest default in as `fallback`. This proves the
    fallback path works when the caller supplies one."""
    from app.integrations.vision.manifest import MANIFEST
    from app.services import ai_roles

    monkeypatch.setattr(
        "app.plugin.config_store.plugin_config",
        lambda name: type("C", (), {"model": ""})(),
    )
    binding = ai_roles.resolve_vision_inbox(
        fallback=MANIFEST.config_schema["model"].default
    )
    assert binding.model == MANIFEST.config_schema["model"].default


def test_vision_inbox_role_with_no_binding_anywhere_is_a_loud_error(monkeypatch):
    """Empty config AND no fallback would leave nothing to bind —
    RoleNotBoundError, never a silent default."""
    from app.services import ai_roles

    monkeypatch.setattr(
        "app.plugin.config_store.plugin_config",
        lambda name: type("C", (), {"model": ""})(),
    )
    with pytest.raises(ai_roles.RoleNotBoundError):
        ai_roles.resolve_vision_inbox(fallback="")


def test_describe_never_raises_when_the_role_is_unbound(monkeypatch, tmp_path):
    """`describe()`'s contract is never to raise — a misconfigured role comes
    back as a `VisionResult` with an error, loudly logged, not an exception
    that would abort a queue walker's whole batch."""
    from app.integrations.vision.manifest import MANIFEST

    monkeypatch.setattr(
        "app.plugin.config_store.plugin_config",
        lambda name: type("C", (), {"gemini_api_key": "k", "model": "", "max_image_mb": 18})(),
    )
    monkeypatch.setitem(
        MANIFEST.config_schema, "model",
        type("Spec", (), {"default": ""})(),
    )
    result = FACADE.describe(tmp_path / "x.png")
    assert isinstance(result, VisionResult)
    assert not result.ok
    assert "vision.inbox" in result.error


def test_ai_roles_resolve_dispatches_vision_inbox(monkeypatch):
    from app.services import ai_roles

    monkeypatch.setattr(
        "app.plugin.config_store.plugin_config",
        lambda name: type("C", (), {"model": "gemini-3.5-flash-lite"})(),
    )
    assert ai_roles.resolve("vision.inbox") == ai_roles.resolve_vision_inbox()
