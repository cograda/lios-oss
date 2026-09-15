"""Vision's output must be reachable through the inbox tools.

The vision sweep already transcribes every word off an inbox image into the
sidecar's `image_text` (see `vision/client.py::PROMPT`, whose TEXT field asks
for a verbatim reading at any orientation). Until 2026-08-31 nothing could get
at it: `list_pending` returned only the one-line `note`, and `inbox_preview`
refused images outright with "preview not supported".

That was measured, not theorised. Three photographed coffee-bag labels were
triaged from a daily note; the varieties, altitude and roast date printed on
each one had been read correctly by vision and were sitting in `image_text`,
while the caller saw only "a coffee bag label for a washed espresso from
Mercedes Ocotepeque, Honduras" and had to copy the files into the vault to
read them. The data was never missing — only unreachable.

The second half of the suite covers a subtler one: `enriched` reports the
*ingest* pass, and vision is a later sweep. Reporting one as the other made
"vision has not run yet", "vision failed" and "vision found no text"
indistinguishable — all three surfaced as `enriched: true, note: null`.
"""

from __future__ import annotations

import json

import pytest

from app.auth.context import use_user
from app.integrations.inbox import scan
from app.integrations.inbox.tools import handle_preview

# A real coffee-bag label, transcribed the way vision actually returns it.
LABEL_TEXT = (
    "FILTER\nMWIRUA GETUYA AB\nKENYA\nwashed\n"
    "BLACKCURRANT, BLUEBERRY & BROWN SUGAR | MASL 1400 - 1600\n"
    "SL28, SL34 & BATIAN | 23.07.2026"
)
LABEL_SUMMARY = "A coffee bag label for a washed filter coffee from Kenya."


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    root = tmp_path / "inbox"
    monkeypatch.setattr(scan.settings, "inbox_path", str(root))
    user_root = scan.user_root(1)
    (user_root / "incoming").mkdir(parents=True)
    return user_root


def _image(inbox, name="20260831-090024-8bbdf31c", **meta):
    """A pending image with a sidecar. `kind` is set explicitly so the test
    needs no real JPEG — sniffing is covered by test_inbox.py."""
    path = inbox / "incoming" / name
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    scan.write_sidecar(path, {"kind": "image", "enriched_at": "2026-08-31T09:00:24+00:00", **meta})
    return path


class TestListPendingSurfacesVisionOutput:
    def test_image_text_is_returned(self, inbox):
        """The transcription must reach the caller, not just the sidecar."""
        _image(inbox, note=LABEL_SUMMARY, image_text=LABEL_TEXT,
               image_kind="label", described_at="2026-08-31T09:05:00+00:00")

        item = scan.list_pending(1)[0]

        assert item["image_text"] == LABEL_TEXT
        assert item["image_kind"] == "label"
        assert item["described"] is True
        assert item["vision_error"] is None
        assert item["image_text_truncated"] is False
        # The fields that made this worth fixing: none of them are in `note`.
        for printed in ("SL28", "MASL 1400 - 1600", "23.07.2026"):
            assert printed in item["image_text"]
            assert printed not in (item["note"] or "")

    def test_long_image_text_is_capped_and_flagged(self, inbox):
        """A photographed letter must not dump 40k chars into a 20-item listing
        — but the caller has to know it was cut, or it will read a truncated
        transcription as the whole document."""
        _image(inbox, image_text="x" * (scan.PREVIEW_CHARS + 500),
               described_at="2026-08-31T09:05:00+00:00")

        item = scan.list_pending(1)[0]

        assert len(item["image_text"]) == scan.PREVIEW_CHARS
        assert item["image_text_truncated"] is True

    def test_no_image_text_reports_none_not_empty_string(self, inbox):
        """An image with no text and an image never looked at must not both
        collapse to a falsy blank that reads as "no text present"."""
        _image(inbox, described_at="2026-08-31T09:05:00+00:00")

        item = scan.list_pending(1)[0]

        assert item["image_text"] is None
        assert item["described"] is True


class TestDescribedIsSeparateFromEnriched:
    """`enriched` is the ingest pass; `described` is the vision sweep. Three
    situations that used to be one indistinguishable shape."""

    def test_not_yet_swept(self, inbox):
        _image(inbox)
        item = scan.list_pending(1)[0]
        assert item["enriched"] is True     # ingest ran
        assert item["described"] is False   # vision has not
        assert item["vision_error"] is None

    def test_vision_failed(self, inbox):
        """A genuine permanent give-up (post-#147/#157): `vision_failed` is
        set once `MAX_VISION_ATTEMPTS` is exhausted, which is what makes
        this shape distinguishable from the legacy one below."""
        _image(inbox, described_at="2026-08-31T09:05:00+00:00",
               vision_error="gemini call failed: 503", vision_failed=True)
        item = scan.list_pending(1)[0]
        assert item["described"] is True
        assert item["vision_error"] == "gemini call failed: 503"

    def test_vision_failed_pre_157_legacy_shape_is_not_described(self, inbox):
        """Issue #160: the pre-#147/#157 `describe_pending` stamped
        `described_at` on the very first attempt regardless of outcome, so a
        row it failed on carries `described_at` + `vision_error` set but
        *no* `vision_attempts`/`vision_failed` — unlike every shape the
        current code can produce, where a failure always sets one or the
        other. That combination must not report `described: true`; it's a
        never-actually-retried failure, not a terminal one."""
        _image(inbox, described_at="2026-08-31T09:05:00+00:00",
               vision_error="gemini call failed: 503")
        item = scan.list_pending(1)[0]
        assert item["described"] is False
        assert item["vision_error"] == "gemini call failed: 503"

    def test_vision_found_nothing(self, inbox):
        """Success with an empty body — a safety block or an unreadable image.
        Distinguishable from failure only because `vision_error` is absent."""
        _image(inbox, described_at="2026-08-31T09:05:00+00:00")
        item = scan.list_pending(1)[0]
        assert item["described"] is True
        assert item["vision_error"] is None
        assert item["note"] is None


class TestPreviewReadsImages:
    def test_preview_returns_full_transcription(self, inbox, mock_session):
        """`inbox_preview` used to refuse images entirely, so there was no route
        from an inbox image to its text at any length."""
        path = _image(inbox, note=LABEL_SUMMARY, image_text=LABEL_TEXT,
                      image_kind="label", described_at="2026-08-31T09:05:00+00:00")

        with use_user(1):
            result = json.loads(handle_preview(mock_session, {"path": str(path)}))

        assert "error" not in result
        assert result["kind"] == "image"
        assert result["text"] == LABEL_TEXT
        assert result["full_length"] == len(LABEL_TEXT)
        assert result["truncated"] is False
        assert result["image_kind"] == "label"
        assert result["note"] == LABEL_SUMMARY

    def test_preview_does_not_re_bill_a_vision_call(self, inbox, mock_session, monkeypatch):
        """Reading is free: vision records `described_at` even on failure so a
        file is never re-sent, and preview must not defeat that by describing
        on demand."""
        called = []
        monkeypatch.setattr(
            "app.plugin.capabilities.get_capability",
            lambda name: called.append(name),
        )
        path = _image(inbox, image_text=LABEL_TEXT, described_at="2026-08-31T09:05:00+00:00")

        with use_user(1):
            result = json.loads(handle_preview(mock_session, {"path": str(path)}))

        # Both halves matter. Asserting only `called == []` passes vacuously if
        # preview refuses images altogether — which is exactly the bug this
        # file exists to prevent regressing, so the test has to prove it read
        # the text *and* paid nothing for it.
        assert result["text"] == LABEL_TEXT
        assert called == []

    @pytest.mark.parametrize("meta,expected_hint", [
        ({}, "has not been through the vision sweep"),
        ({"described_at": "2026-08-31T09:05:00+00:00",
          "vision_error": "gemini rejected: 400"}, "vision failed"),
        ({"described_at": "2026-08-31T09:05:00+00:00"}, "found no text"),
    ])
    def test_empty_text_says_why(self, inbox, mock_session, meta, expected_hint):
        """Three reasons for no text, and a caller has to tell them apart:
        one is retryable, one is reportable, one is a finished answer."""
        path = _image(inbox, **meta)

        with use_user(1):
            result = json.loads(handle_preview(mock_session, {"path": str(path)}))

        assert result["text"] == ""
        assert expected_hint in result["hint"]
