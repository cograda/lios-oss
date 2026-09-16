"""Inbox kind-sniffing, media preview, and summary rendering.

Motivated by a real complaint: the ntfy confirmations for captured voice notes
carried no useful information. Three separate causes, one test class each:

  1. `sniff_kind` could not recognise an ISO-BMFF file whose ftyp box wasn't
     exactly 32 bytes — so iOS voice notes (which arrive from Tines with no
     filename extension to fall back on) were classified "unknown".
  2. There was no audio preview at all, only a placeholder note.
  3. `metadata.note` was written to the sidecar at ingest and then never
     returned by `list_pending`.

Files here are built with **no extension**, deliberately: that's what actually
arrives (`original_filename` is a bare UUID), and it's what forces the magic-byte
path rather than the `_EXT_KIND` shortcut.
"""

from __future__ import annotations

import struct

import pytest

from app.integrations.inbox import scan


# ---------------------------------------------------------------------------
# Synthetic ISO-BMFF builders — cheaper and more precise than binary fixtures
# ---------------------------------------------------------------------------

def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + box_type + payload


def _mvhd_v0(timescale: int, duration: int) -> bytes:
    payload = (
        b"\x00\x00\x00\x00"            # version 0 + flags
        + struct.pack(">I", 0)         # creation time
        + struct.pack(">I", 0)         # modification time
        + struct.pack(">I", timescale)
        + struct.pack(">I", duration)
        + b"\x00" * 80                 # matrix/rate/reserved — unread
    )
    return _box(b"mvhd", payload)


def _mvhd_v1(timescale: int, duration: int) -> bytes:
    payload = (
        b"\x01\x00\x00\x00"            # version 1 + flags
        + struct.pack(">Q", 0)         # creation time (64-bit)
        + struct.pack(">Q", 0)         # modification time (64-bit)
        + struct.pack(">I", timescale)
        + struct.pack(">Q", duration)
        + b"\x00" * 80
    )
    return _box(b"mvhd", payload)


def _ftyp(brand: bytes, compat: bytes = b"") -> bytes:
    """An ftyp box. `compat` lets the box length vary, which is the whole point
    of the regression below — the old sniffer only matched length 32."""
    return _box(b"ftyp", brand + struct.pack(">I", 0) + compat)


def _media_file(tmp_path, brand: bytes, *, mvhd: bytes | None = None,
                compat: bytes = b"", name: str = "capture") -> "object":
    """Write an extension-less ISO-BMFF file, as Tines delivers them."""
    body = _ftyp(brand, compat)
    if mvhd is not None:
        body += _box(b"moov", mvhd)
    path = tmp_path / name
    path.write_bytes(body)
    return path


# ---------------------------------------------------------------------------
# 1. Kind sniffing
# ---------------------------------------------------------------------------

class TestFtypSniffing:
    def test_m4a_brand_is_audio(self, tmp_path):
        path = _media_file(tmp_path, b"M4A ")
        assert scan.sniff_kind(path) == "audio"

    def test_mp4_brand_stays_video(self, tmp_path):
        """`isom`/`mp42` are ambiguous containers; video is the safe default."""
        assert scan.sniff_kind(_media_file(tmp_path, b"isom")) == "video"
        assert scan.sniff_kind(_media_file(tmp_path, b"mp42")) == "video"

    @pytest.mark.parametrize("compat", [b"", b"M4A ", b"M4A mp42", b"M4A mp42isom"])
    def test_detected_regardless_of_ftyp_box_length(self, tmp_path, compat):
        """THE regression.

        The old check was `head.startswith(b"\\x00\\x00\\x00 ftyp")` — a literal
        32-byte box length. Compatible-brand lists make that length vary, so any
        real file with a different one fell through to "unknown". Each `compat`
        here produces a different box length; all must still be audio.
        """
        path = _media_file(tmp_path, b"M4A ", compat=compat)
        assert scan.sniff_kind(path) == "audio"

    def test_extension_still_wins_when_present(self, tmp_path):
        """`_EXT_KIND` remains the fast path for files that do have a suffix."""
        path = tmp_path / "memo.m4a"
        path.write_bytes(_ftyp(b"isom"))  # brand says video, extension says audio
        assert scan.sniff_kind(path) == "audio"

    def test_non_bmff_is_unaffected(self, tmp_path):
        png = tmp_path / "shot"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        assert scan.sniff_kind(png) == "image"

    def test_truncated_header_does_not_raise(self, tmp_path):
        path = tmp_path / "tiny"
        path.write_bytes(b"\x00\x00\x00")
        assert scan.sniff_kind(path) == "unknown"

    def test_ftyp_marker_at_wrong_offset_is_not_bmff(self, tmp_path):
        """Guard against matching "ftyp" anywhere in the head."""
        path = tmp_path / "decoy"
        path.write_bytes(b"ftyp" + b"\x00" * 20)
        assert scan.sniff_kind(path) != "audio"


# ---------------------------------------------------------------------------
# 1b. Office format sniffing (issue #140) — a `.doc`-named file that is
# really OOXML content (a zip with `word/document.xml`) used to be routed by
# extension and fail every parser. `sniff_kind` now looks at the real bytes:
# OLE2/MS-CFB (legacy .doc/.xls) is recognised (not parsed — there's no
# parser for it), and a zip is opened and its member list inspected rather
# than assumed. Every fixture here is named with NO extension, or a
# deliberately WRONG one, so the `_EXT_KIND` fast path can't be the thing
# making the test pass.
# ---------------------------------------------------------------------------

def _ole2_bytes(*, marker: bytes | None = None) -> bytes:
    """A minimal fake OLE2/MS-CFB file: just the 8-byte magic plus enough
    padding for `_sniff_ole2_kind`'s larger read, with an optional UTF-16LE
    stream-name marker embedded in the "directory" region — good enough for
    the sniffer, which never tries to actually parse the compound file."""
    body = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504  # one 512-byte sector
    if marker:
        body += marker
    return body


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


class TestOfficeFormatSniffing:
    def test_ole2_with_no_markers_defaults_to_doc(self, tmp_path):
        path = tmp_path / "attachment.doc"
        path.write_bytes(_ole2_bytes())
        assert scan.sniff_kind(path) == "doc"

    def test_ole2_with_workbook_marker_sniffs_as_xls(self, tmp_path):
        path = tmp_path / "attachment.xls"
        path.write_bytes(_ole2_bytes(marker="Workbook".encode("utf-16-le")))
        assert scan.sniff_kind(path) == "xls"

    def test_ole2_extension_does_not_matter(self, tmp_path):
        """A real legacy .doc mislabelled with no extension at all (the
        Tines shape) still sniffs correctly — this is content, not a
        suffix lookup."""
        path = tmp_path / "no_extension_at_all"
        path.write_bytes(_ole2_bytes())
        assert scan.sniff_kind(path) == "doc"

    def test_mislabelled_doc_that_is_really_docx_sniffs_as_docx(self, tmp_path):
        """THE regression this issue is about: a `.doc` file whose real
        bytes are an OOXML zip (word/document.xml) must sniff as docx, not
        as a generic zip and not as legacy doc."""
        path = tmp_path / "renovation_contract.doc"
        path.write_bytes(_zip_bytes({
            "[Content_Types].xml": b"<Types/>",
            "word/document.xml": b"<w:document/>",
        }))
        assert scan.sniff_kind(path) == "docx"

    def test_mislabelled_doc_that_is_really_xlsx_sniffs_as_xlsx(self, tmp_path):
        """Same confusion, spreadsheet side ("while there" per the issue)."""
        path = tmp_path / "quote.doc"
        path.write_bytes(_zip_bytes({
            "[Content_Types].xml": b"<Types/>",
            "xl/workbook.xml": b"<workbook/>",
        }))
        assert scan.sniff_kind(path) == "xlsx"

    def test_correctly_named_docx_still_sniffs_as_docx(self, tmp_path):
        path = tmp_path / "note.docx"
        path.write_bytes(_zip_bytes({
            "[Content_Types].xml": b"<Types/>",
            "word/document.xml": b"<w:document/>",
        }))
        assert scan.sniff_kind(path) == "docx"

    def test_unrelated_zip_stays_zip(self, tmp_path):
        """A zip that just isn't an Office document (no word/ or xl/ member)
        must not be misclassified as one."""
        path = tmp_path / "archive.doc"
        path.write_bytes(_zip_bytes({"readme.txt": b"hello"}))
        assert scan.sniff_kind(path) == "zip"

    def test_corrupt_zip_falls_back_to_zip_not_a_crash(self, tmp_path):
        path = tmp_path / "broken.doc"
        path.write_bytes(b"PK\x03\x04" + b"\x00" * 40)  # zip magic, garbage body
        assert scan.sniff_kind(path) == "zip"

    def test_pdf_mislabelled_as_doc_still_sniffs_as_pdf(self, tmp_path):
        path = tmp_path / "scan.doc"
        path.write_bytes(b"%PDF-1.4\n%mock pdf body")
        assert scan.sniff_kind(path) == "pdf"


# ---------------------------------------------------------------------------
# 2. Duration extraction
# ---------------------------------------------------------------------------

class TestDuration:
    def test_reads_v0_mvhd(self, tmp_path):
        # 44100 ticks/sec, 44100*107 ticks = 107s
        path = _media_file(tmp_path, b"M4A ", mvhd=_mvhd_v0(44100, 44100 * 107))
        assert scan._mp4_duration_seconds(path) == pytest.approx(107.0)

    def test_reads_v1_mvhd_64bit(self, tmp_path):
        path = _media_file(tmp_path, b"M4A ", mvhd=_mvhd_v1(48000, 48000 * 90))
        assert scan._mp4_duration_seconds(path) == pytest.approx(90.0)

    def test_missing_moov_returns_none(self, tmp_path):
        path = _media_file(tmp_path, b"M4A ")  # ftyp only
        assert scan._mp4_duration_seconds(path) is None

    def test_zero_timescale_returns_none_not_zerodivision(self, tmp_path):
        path = _media_file(tmp_path, b"M4A ", mvhd=_mvhd_v0(0, 1000))
        assert scan._mp4_duration_seconds(path) is None

    def test_malformed_box_length_terminates(self, tmp_path):
        """A size < 8 would loop forever if not guarded — the parser must bail
        rather than hang the enrichment cron."""
        path = tmp_path / "bad"
        path.write_bytes(_ftyp(b"M4A ") + struct.pack(">I", 2) + b"moov")
        assert scan._mp4_duration_seconds(path) is None

    def test_preview_reports_human_duration(self, tmp_path):
        path = _media_file(tmp_path, b"M4A ", mvhd=_mvhd_v0(1000, 1000 * 107))
        preview, extras = scan.extract_preview(path, "audio")
        assert preview == ""  # no transcription — see _preview_media's docstring
        assert extras["duration_seconds"] == pytest.approx(107.0)
        assert extras["duration_human"] == "1m 47s"

    def test_undeterminable_duration_degrades_gracefully(self, tmp_path):
        preview, extras = scan.extract_preview(_media_file(tmp_path, b"M4A "), "audio")
        assert preview == ""
        assert "duration unavailable" in extras["note"]

    @pytest.mark.parametrize("seconds,expected", [
        (9, "9s"), (60, "1m 0s"), (107, "1m 47s"), (3600, "1h 0m"), (5430, "1h 30m"),
    ])
    def test_duration_formatting(self, seconds, expected):
        assert scan._format_seconds(seconds) == expected


# ---------------------------------------------------------------------------
# 3. Summary rendering — what the notification actually says
# ---------------------------------------------------------------------------

class TestSummarise:
    def test_voice_note_reads_as_a_voice_note(self):
        summary = scan.summarise(
            {"kind": "audio", "preview_meta": {"duration_human": "1m 47s"}},
            size_bytes=412 * 1024,
        )
        assert summary == "voice note, 1m 47s, 412 KB"

    def test_note_leads_when_present(self):
        """A transcript supplied by the caller is the most useful thing to say."""
        summary = scan.summarise({
            "kind": "audio",
            "preview_meta": {"duration_human": "12s"},
            "note": "remind me to ring the plumber about the utility room",
        })
        assert summary.startswith("voice note, 12s — ")
        assert "ring the plumber" in summary

    def test_note_beats_preview(self):
        summary = scan.summarise({
            "kind": "text", "preview": "extracted text", "note": "caller note",
        })
        assert "caller note" in summary
        assert "extracted text" not in summary

    def test_multiline_body_becomes_one_line(self):
        """An ntfy body spanning lines renders badly on a lock screen."""
        summary = scan.summarise({"kind": "text", "note": "line one\n\n  line two\ttab"})
        assert "\n" not in summary and "\t" not in summary
        assert "line one line two tab" in summary

    def test_long_body_is_truncated_with_an_ellipsis(self):
        summary = scan.summarise({"kind": "text", "note": "x" * 500})
        assert len(summary) < 300
        assert summary.endswith("…")

    def test_image_reports_dimensions(self):
        summary = scan.summarise(
            {"kind": "image", "preview_meta": {"width": 1206, "height": 2622}},
            size_bytes=1862962,
        )
        assert "1206×2622" in summary
        assert "image" in summary

    def test_pdf_pluralises_pages(self):
        assert "1 page" in scan.summarise({"kind": "pdf", "preview_meta": {"page_count": 1}})
        assert "9 pages" in scan.summarise({"kind": "pdf", "preview_meta": {"page_count": 9}})

    def test_falls_back_to_original_filename(self):
        summary = scan.summarise({"kind": "unknown", "original_filename": "IMG_0042"})
        assert "IMG_0042" in summary

    def test_empty_metadata_still_produces_something(self):
        """Never return an empty notification body."""
        assert scan.summarise({}).strip()


# ---------------------------------------------------------------------------
# 4. list_pending surfaces the fields consumers need
# ---------------------------------------------------------------------------

class TestListPendingFields:
    @pytest.fixture
    def inbox(self, tmp_path, monkeypatch):
        """Returns user 1's per-user subtree (`<root>/u1/`) — F6 made the
        inbox per-user, but every existing `inbox / "incoming" / ...` call
        site in this file stays correct unchanged, since it's relative to
        whatever `inbox` points at."""
        root = tmp_path / "inbox"
        monkeypatch.setattr(scan.settings, "inbox_path", str(root))
        user_root = scan.user_root(1)
        (user_root / "incoming").mkdir(parents=True)
        return user_root

    @pytest.fixture
    def transcriber(self, monkeypatch):
        """Stub the transcription + notify (push and email) capabilities
        scan reaches for.

        Returns a setter; `.calls` on it records every transcription attempt,
        `.pushes` every `notify.push` send, `.emails` every `notify.email`
        send — so tests can assert on billable work and on notifications
        rather than only on sidecar output.
        """
        import app.plugin.capabilities as capabilities

        state = {
            "text": "transcribed words", "source": "openai", "available": True,
            # Retry-migration additions: an error string and whether it's the
            # transient (retryable) kind. None/False replicate every existing
            # call site's success shape unchanged.
            "error": None, "transient": False,
            # `title` (Tines-retirement addition): None replicates every
            # existing call site, where Gemini's structured output either
            # wasn't asked for or didn't parse.
            "title": None,
            # `speakers` — same shape and same reasoning as `title`.
            "speakers": None,
        }
        calls: list[Path] = []
        pushes: list[tuple] = []
        push_user_ids: list[int | None] = []
        emails: list[tuple] = []

        class _Transcription:
            def available(self):
                return state["available"]

            def transcribe(self, path, *, prefer="openai", vault_root=None, duration_s=None):
                calls.append(path)
                from app.integrations.transcription.facade import TranscriptResult

                return TranscriptResult(
                    text=state["text"], source=state["source"],
                    error=state["error"], transient=state["transient"],
                    title=state["title"], speakers=state["speakers"],
                )

        class _Notify:
            # Signature mirrors `NotificationsFacade.send` exactly, `user_id`
            # included. That is load-bearing rather than incidental: the real
            # `_notify_enriched` wraps its send in `except Exception` (a dropped
            # push must never fail the capture it describes), so a double whose
            # signature has drifted from the facade's raises no visible
            # TypeError — it silently records zero pushes, and every assertion
            # below then reads as "the notification was not sent" when what
            # actually happened is "the test double is out of date". Keep this
            # in step with the facade.
            #
            # `user_id` lands in its own list rather than widening the `pushes`
            # tuple, so the existing three-way unpacks keep working and a
            # routing assertion is additive.
            def send(self, title, body, severity="warning", user_id=None, **kwargs):
                pushes.append((title, body, severity))
                push_user_ids.append(user_id)
                return True

        class _Email:
            def send_email(self, to_user_id, subject, body_html, body_text=None, attachments=None):
                emails.append((to_user_id, subject, body_html, body_text, attachments))
                return True

        def fake_get_capability(name):
            if name == "transcription.audio":
                return _Transcription()
            if name == "notify.push":
                return _Notify()
            if name == "notify.email":
                return _Email()
            raise KeyError(name)

        monkeypatch.setattr(capabilities, "get_capability", fake_get_capability)
        monkeypatch.setattr(scan, "_vault_root_for_dictionary", lambda user_id=None: None)

        def setter(**overrides):
            state.update(overrides)
            return state

        setter.calls = calls
        setter.pushes = pushes
        setter.push_user_ids = push_user_ids
        setter.emails = emails
        return setter

    def _audio(self, inbox, seconds=60, name="20260731-120000-abcd1234"):
        path = inbox / "incoming" / name
        path.write_bytes(_ftyp(b"M4A ") + _box(b"moov", _mvhd_v0(1000, 1000 * seconds)))
        scan.enrich_one(path)
        return path

    def test_transcript_lands_in_note_and_leads_the_summary(self, inbox, transcriber):
        """The end of the whole pipeline: the pushed message carries the words."""
        path = self._audio(inbox, seconds=107)
        transcriber(text="ring the plumber about the utility room")

        counts = scan.transcribe_pending()

        assert counts["transcribed"] == 1
        meta = scan.read_sidecar(path)
        assert meta["note"] == "ring the plumber about the utility room"
        assert meta["transcript_source"] == "openai"
        assert "ring the plumber" in scan.list_pending(1)[0]["summary"]

    def test_note_and_summary_are_returned(self, inbox):
        """`note` used to be written at ingest and then dropped here, making a
        caller-supplied transcript invisible to every consumer."""
        path = inbox / "incoming" / "20260731-120000-abcd1234"
        path.write_bytes(_ftyp(b"M4A ") + _box(b"moov", _mvhd_v0(1000, 1000 * 30)))
        scan.write_sidecar(path, {
            "original_filename": "3CB8DE0D-7301",
            "note": "pick up the blind brackets",
        })
        scan.enrich_one(path)

        items = scan.list_pending(1)
        assert len(items) == 1
        item = items[0]
        assert item["kind"] == "audio"
        assert item["note"] == "pick up the blind brackets"
        assert "pick up the blind brackets" in item["summary"]
        assert item["preview_meta"]["duration_human"] == "30s"




    def test_speakers_lands_in_the_sidecar(self, inbox, transcriber):
        """`speakers` is produced, carried, and must actually be written down.

        It was dropped at the sidecar write (2026-08-29): the transcriber
        returned it, `TranscriptResult` carried it, and only `title` was
        stored. Nothing failed — the field was simply always absent, which is
        the easiest kind of gap to keep. It is the one cheap "meeting vs note
        to self" signal; the retired Tines story chose meeting-note.md vs
        voice-note.md from exactly this.
        """
        audio = inbox / "audio" / "memo.m4a"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes((32).to_bytes(4, "big") + b"ftyp" + b"M4A " + b"\x00" * 24)
        scan.write_sidecar(audio, {"kind": "audio"})

        transcriber(text="A: hi\nB: hello", title="Two people talking", speakers=2)
        scan.transcribe_pending()

        meta = scan.read_sidecar(audio)
        assert meta["speakers"] == 2
        assert meta["title"] == "Two people talking"

    def test_absent_speakers_is_not_written_as_none(self, inbox, transcriber):
        audio = inbox / "audio" / "memo2.m4a"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes((32).to_bytes(4, "big") + b"ftyp" + b"M4A " + b"\x00" * 24)
        scan.write_sidecar(audio, {"kind": "audio"})

        transcriber(text="just me", title="A note", speakers=None)
        scan.transcribe_pending()

        assert "speakers" not in scan.read_sidecar(audio)

class TestContentDedup:
    """Server-side dedup — the only place it can be authoritative.

    Two producers reach the ingest route (a phone via the Tines tunnel, the Mac
    watcher when running) and neither can see the other's uploads. A duplicate
    costs a second transcription, so the guard has to be here.
    """

    inbox = TestListPendingFields.inbox

    def _store(self, inbox, bucket, name, data, extra=None):
        directory = inbox / bucket
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(data)
        meta = {"sha256": scan.content_hash(data)}
        meta.update(extra or {})
        scan.write_sidecar(path, meta)
        return path

    def test_same_bytes_are_recognised(self, inbox):
        data = b"identical audio bytes"
        stored = self._store(inbox, "incoming", "first", data)
        assert scan.find_by_hash(scan.content_hash(data), 1) == stored

    def test_different_bytes_are_not(self, inbox):
        self._store(inbox, "incoming", "first", b"one")
        assert scan.find_by_hash(scan.content_hash(b"two"), 1) is None

    def test_recognised_after_being_archived(self, inbox):
        """The realistic case: a memo posted from the phone, triaged into the
        vault and archived, then synced to the Mac a week later. It must not come
        back as new and get transcribed again."""
        data = b"already dealt with"
        stored = self._store(inbox, "archive", "done", data,
                             extra={"note": "already transcribed"})
        assert scan.find_by_hash(scan.content_hash(data), 1) == stored

    def test_dismissed_items_also_count(self, inbox):
        data = b"deliberately binned"
        self._store(inbox, "dismissed", "nope", data)
        assert scan.find_by_hash(scan.content_hash(data), 1) is not None

    def test_sidecars_are_not_mistaken_for_content(self, inbox):
        self._store(inbox, "incoming", "first", b"payload")
        # The sidecar itself is a file in the bucket; it must be skipped.
        assert scan.find_by_hash(scan.content_hash(b"payload"), 1).name == "first"

    def test_missing_inbox_root_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scan.settings, "inbox_path", str(tmp_path / "nonexistent"))
        assert scan.find_by_hash("deadbeef", 1) is None

    def test_files_without_a_hash_are_ignored(self, inbox):
        """Files ingested before sha256 was recorded must not match on None."""
        path = inbox / "incoming" / "legacy"
        path.write_bytes(b"old file")
        scan.write_sidecar(path, {"original_filename": "legacy"})
        assert scan.find_by_hash("", 1) is None


class TestTranscribePendingIsBillable:
    """Idempotency tests. Every redundant call here is money, so these are the
    highest-value tests in this file."""

    inbox = TestListPendingFields.inbox
    transcriber = TestListPendingFields.transcriber
    _audio = TestListPendingFields._audio

    def test_second_sweep_does_not_re_transcribe(self, inbox, transcriber):
        """A cron every 5 minutes must not re-bill the same file 288 times a day."""
        self._audio(inbox)
        scan.transcribe_pending()
        counts = scan.transcribe_pending()

        assert counts["skipped"] == 1
        assert counts["transcribed"] == 0
        assert len(transcriber.calls) == 1

    def test_silent_recording_is_not_retried_forever(self, inbox, transcriber):
        """Empty text with no error means genuine silence (a pocket recording).
        It must still be marked done, or it's a permanent recurring charge."""
        path = self._audio(inbox)
        transcriber(text="", source="none")

        scan.transcribe_pending()
        assert scan.read_sidecar(path)["transcribed_at"]

        scan.transcribe_pending()
        assert len(transcriber.calls) == 1

    def test_transient_failure_is_retried_not_stamped(self, inbox, transcriber):
        """The bug this migration fixes: a 5xx/network blip used to be
        indistinguishable from a permanent failure and buried the memo
        forever on the very first sweep. It must stay un-stamped so the next
        cron run (every 5 minutes) tries again."""
        path = self._audio(inbox)
        transcriber(text="", source="none", error="HTTP 503 upstream", transient=True)

        counts = scan.transcribe_pending()

        assert counts["retrying"] == 1
        assert counts["failed"] == 0
        meta = scan.read_sidecar(path)
        assert not meta.get("transcribed_at")
        assert meta["transcript_attempts"] == 1
        assert transcriber.pushes == []  # not a give-up yet, no notification

        # And the very next sweep picks it up again — it is not "skipped".
        scan.transcribe_pending()
        assert len(transcriber.calls) == 2

    def test_three_transient_failures_give_up_stamp_and_notify(self, inbox, transcriber):
        """Matches the retired Tines story's `retries: 3` + failure email."""
        path = self._audio(inbox)
        transcriber(text="", source="none", error="HTTP 503 upstream", transient=True)

        scan.transcribe_pending()
        scan.transcribe_pending()
        counts = scan.transcribe_pending()

        assert len(transcriber.calls) == 3
        assert counts["failed"] == 1
        assert counts["retrying"] == 0
        meta = scan.read_sidecar(path)
        assert meta["transcribed_at"]
        assert "HTTP 503" in meta["transcript_error"]
        # attempts is cleared once terminal — nothing left to count toward.
        assert "transcript_attempts" not in meta

        assert len(transcriber.pushes) == 1
        title, body, severity = transcriber.pushes[0]
        assert "failed" in title
        assert severity == "warning"

        # And now it is done: a fourth sweep does not retry it again.
        scan.transcribe_pending()
        assert len(transcriber.calls) == 3

    def test_permanent_error_is_stamped_immediately_and_notifies(self, inbox, transcriber):
        """A non-retryable error (bad container, oversized file) must not
        wait for three attempts before giving up — there's nothing a retry
        could fix."""
        path = self._audio(inbox)
        transcriber(text="", source="none", error="unsupported container", transient=False)

        counts = scan.transcribe_pending()

        assert counts["failed"] == 1
        assert counts["retrying"] == 0
        assert scan.read_sidecar(path)["transcribed_at"]
        assert len(transcriber.calls) == 1
        assert len(transcriber.pushes) == 1

    def test_unconfigured_is_a_silent_no_op(self, inbox, transcriber):
        """Otherwise an unconfigured deployment logs a failure every 5 minutes."""
        self._audio(inbox)
        transcriber(available=False)

        counts = scan.transcribe_pending()
        assert counts == {
            "considered": 0, "transcribed": 0, "skipped": 0, "failed": 0, "retrying": 0,
        }
        assert transcriber.calls == []

    def test_embedded_preference_works_unconfigured(self, inbox, transcriber):
        """The backfill path needs no API key when the free transcript exists."""
        self._audio(inbox)
        transcriber(available=False, source="embedded")
        counts = scan.transcribe_pending(prefer="embedded")
        assert counts["transcribed"] == 1

    def test_limit_bounds_spend_per_sweep(self, inbox, transcriber):
        """A backfill of hundreds walks through over successive runs rather than
        issuing hundreds of paid requests at once."""
        for i in range(5):
            self._audio(inbox, name=f"20260731-1200{i:02d}-file{i}")

        counts = scan.transcribe_pending(limit=2)
        assert counts["transcribed"] == 2
        assert len(transcriber.calls) == 2

    def test_non_audio_is_ignored(self, inbox, transcriber):
        pdf = inbox / "incoming" / "20260731-120000-doc.pdf"
        pdf.write_bytes(b"%PDF-1.4\n" + b"\x00" * 32)
        scan.enrich_one(pdf)

        counts = scan.transcribe_pending()
        assert counts["considered"] == 0
        assert transcriber.calls == []

    def test_transcript_is_announced(self, inbox, transcriber):
        """The ingest-time push could only say 'voice note, 1m 47s' — this is the
        message that carries the content."""
        self._audio(inbox)
        transcriber(text="collect the blind brackets")
        scan.transcribe_pending()

        assert len(transcriber.pushes) == 1
        title, body, _severity = transcriber.pushes[0]
        assert "transcribed" in title
        assert "collect the blind brackets" in body

    def test_failure_is_recorded_on_the_file(self, inbox, transcriber, monkeypatch):
        """A recorded reason is what makes a failure diagnosable later — and what
        stops the file being retried blindly."""
        path = self._audio(inbox)
        from app.integrations.transcription.facade import TranscriptResult

        import app.plugin.capabilities as capabilities

        class _Failing:
            def available(self):
                return True

            def transcribe(self, p, *, prefer="openai", vault_root=None, duration_s=None):
                return TranscriptResult(text="", source="none", error="HTTP 500 upstream")

        # Via monkeypatch, not direct assignment: a leaked stub here would break
        # every test that ran afterwards.
        monkeypatch.setattr(
            capabilities,
            "get_capability",
            lambda name: _Failing() if name == "transcription.audio" else None,
        )
        counts = scan.transcribe_pending()

        assert counts["failed"] == 1
        assert "HTTP 500" in scan.read_sidecar(path)["transcript_error"]


class TestTranscriptTitleOnPush:
    """Task A of the Tines retirement: a Gemini-parsed `title` should show up
    on the lock screen instead of the generic "lios: voice note
    transcribed" headline, when there is one."""

    inbox = TestListPendingFields.inbox
    transcriber = TestListPendingFields.transcriber
    _audio = TestListPendingFields._audio

    def test_title_replaces_the_generic_push_headline(self, inbox, transcriber):
        self._audio(inbox)
        transcriber(text="ring the plumber about the utility room", title="Plumber call notes")

        scan.transcribe_pending()

        assert len(transcriber.pushes) == 1
        title, _body, _severity = transcriber.pushes[0]
        assert title == "lios: Plumber call notes"

    def test_title_is_persisted_to_the_sidecar(self, inbox, transcriber):
        path = self._audio(inbox)
        transcriber(title="Plumber call notes")

        scan.transcribe_pending()

        assert scan.read_sidecar(path)["title"] == "Plumber call notes"

    def test_no_title_falls_back_to_the_generic_headline(self, inbox, transcriber):
        """Embedded transcripts and any response that didn't parse never
        have a title — today's behaviour must survive unchanged."""
        path = self._audio(inbox)
        transcriber(text="collect the blind brackets", title=None)

        scan.transcribe_pending()

        assert len(transcriber.pushes) == 1
        title, _body, _severity = transcriber.pushes[0]
        assert title == "lios: voice note transcribed"
        assert "title" not in scan.read_sidecar(path)


class TestTranscriptEmail:
    """Task B of the Tines retirement: `notify.email` reproduces the two
    Tines transcript emails (`../tines-reference.json`'s `Done`/`Not Done`
    stories)."""

    inbox = TestListPendingFields.inbox
    transcriber = TestListPendingFields.transcriber
    _audio = TestListPendingFields._audio

    def test_success_emails_the_owner_with_a_transcript_attachment(self, inbox, transcriber):
        self._audio(inbox)
        transcriber(text="ring the plumber about the utility room", title="Plumber call notes")

        scan.transcribe_pending()

        assert len(transcriber.emails) == 1
        to_user_id, subject, body_html, body_text, attachments = transcriber.emails[0]
        assert to_user_id == 1
        assert subject == "Transcript complete: Plumber call notes"
        assert "ring the plumber about the utility room" in body_html
        assert "ring the plumber about the utility room" in body_text
        assert "Automated by Tines" not in body_html
        assert len(attachments) == 1
        assert attachments[0].filename == "transcript.txt"
        assert attachments[0].content_type == "text/plain"
        assert attachments[0].content == b"ring the plumber about the utility room"

    def test_success_email_falls_back_to_a_generic_subject_without_a_title(self, inbox, transcriber):
        self._audio(inbox)
        transcriber(text="collect the blind brackets", title=None)

        scan.transcribe_pending()

        assert len(transcriber.emails) == 1
        _to, subject, *_rest = transcriber.emails[0]
        assert subject == "Transcript complete"

    def test_give_up_sends_the_failure_email(self, inbox, transcriber):
        """Same give-up branch that already pushes.

        No longer Tines' `Not Done` wording verbatim: that body was "No usable
        transcript was produced. Please try again." and nothing else, which
        names neither the recording nor where it went. Two memos in one morning
        and it cannot be acted on. See `_notify_transcript_failure_email`.
        """
        self._audio(inbox)
        transcriber(text="", source="none", error="HTTP 503 upstream", transient=True)

        scan.transcribe_pending()
        scan.transcribe_pending()
        scan.transcribe_pending()

        assert len(transcriber.emails) == 1
        to_user_id, subject, body_html, body_text, attachments = transcriber.emails[0]
        assert to_user_id == 1
        assert attachments is None

        # The recording is identified in the subject, so it is legible in a
        # mail list without opening anything.
        assert "20260731-120000-abcd1234" in subject

        # And the body says the audio survives. This is the half that stops a
        # recipient re-recording a thought they cannot reconstruct: "try again"
        # on its own implies the capture was lost, and it was not.
        assert "20260731-120000-abcd1234" in body_text
        assert "no need to re-record" in body_text.lower()
        # …and where it still is, so it can be found without a search.
        assert "Still on disk at" in body_text

    # ---------------------------------------------------------------
    # Owner routing.
    #
    # The bug this pins: `_notify_enriched` used to call `send()` with no
    # `user_id`, which `client._resolve_targets` reads as household-wide and
    # resolves to `household_targets` — in production a single entry, one
    # person's phone. Every capture notification went there regardless of who
    # captured it, so one household member's voice notes notified another, and
    # the Gemini-derived title — built deliberately name-free because a lock
    # screen is semi-public — was delivered to the wrong lock screen.
    #
    # It was never a missing-information problem: the owning user is in the
    # path, and the *email* alongside this push was already reading it via
    # `owner_user_id_from_path`. Push and email simply disagreed.
    # ---------------------------------------------------------------

    def test_transcript_push_is_routed_to_the_files_owner(self, inbox, transcriber):
        self._audio(inbox)
        transcriber(text="collect the blind brackets")

        scan.transcribe_pending()

        assert transcriber.push_user_ids == [1]

    def test_another_users_capture_does_not_notify_user_one(self, inbox, transcriber):
        """The whole point. A memo under `u2/` must reach user 2 and nobody
        else — `None` here would mean household-wide, i.e. the wrong phone."""
        other = scan.user_root(2) / "incoming"
        other.mkdir(parents=True)
        path = other / "20260731-121500-beef5678"
        path.write_bytes(_ftyp(b"M4A ") + _box(b"moov", _mvhd_v0(1000, 60000)))
        scan.enrich_one(path)
        transcriber(text="the upstairs radiator is cold")

        scan.transcribe_pending()

        assert transcriber.push_user_ids == [2]
        assert 1 not in transcriber.push_user_ids
        # The email half was already correct; assert both agree now, since the
        # failure being pinned is precisely the two disagreeing.
        assert transcriber.emails[0][0] == 2

    def test_an_unowned_path_falls_back_to_household_wide(self, inbox, tmp_path, transcriber):
        """A path with no resolvable owner still announces, household-wide.

        Tested against `_notify_enriched` directly rather than through the
        sweep, because the sweep cannot produce this state: `adopt_legacy_files`
        migrates pre-F6 flat-tree files into user 1's subtree, so they arrive
        owned. The fallback still has to be right — `owner_user_id_from_path`
        returns None for anything outside the inbox root — and the failure it
        guards against is the notification going *nowhere* rather than to
        everyone, which is the silent direction.
        """
        stray = tmp_path / "not-the-inbox" / "20260731-123000-cafe9999"
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b"x" * 32)

        scan._notify_enriched({"kind": "audio"}, stray, "lios: voice note captured")

        assert transcriber.push_user_ids == [None]
        assert len(transcriber.pushes) == 1

    def test_a_transient_retry_does_not_email_yet(self, inbox, transcriber):
        """Only the terminal give-up sends mail — a retry that will succeed
        on the next sweep is not a failure worth emailing about."""
        self._audio(inbox)
        transcriber(text="", source="none", error="HTTP 503 upstream", transient=True)

        scan.transcribe_pending()

        assert transcriber.emails == []

    def test_genuine_silence_sends_no_email(self, inbox, transcriber):
        """A pocket recording is not a failure and Tines never emailed about
        it — matches the push behaviour for the same outcome."""
        self._audio(inbox)
        transcriber(text="", source="none")

        scan.transcribe_pending()

        assert transcriber.emails == []
        assert transcriber.pushes == []

    def test_mail_failure_does_not_fail_the_transcription(self, inbox, transcriber, monkeypatch):
        """Email is a convenience on an already-saved transcript — a mail
        outage must never fail or re-queue the transcription."""
        path = self._audio(inbox)
        transcriber(text="ring the plumber about the utility room")

        import app.plugin.capabilities as capabilities

        real_get_capability = capabilities.get_capability

        def _boom(name):
            if name == "notify.email":
                raise RuntimeError("smtp is down")
            return real_get_capability(name)

        monkeypatch.setattr(capabilities, "get_capability", _boom)

        counts = scan.transcribe_pending()

        assert counts["transcribed"] == 1
        meta = scan.read_sidecar(path)
        assert meta["transcribed_at"]
        assert meta["note"] == "ring the plumber about the utility room"

        # And it stays done — a second sweep must not re-bill because of the
        # earlier mail failure.
        scan.transcribe_pending()
        assert len(transcriber.calls) == 1


# ---------------------------------------------------------------------------
# lios#198/#191 — `comar` (a.k.a. "metadata.comar") answered No: transcribe/
# describe and email, never file. `test_inbox_scoping.py::TestMetadataComarFlag`
# pins the route wiring (does the right delivery path get scheduled, with the
# right owner); this class pins the delivery itself — `spool_capture` and
# `deliver_capture_by_email`, including that nothing survives the call.
# ---------------------------------------------------------------------------


class TestEmailOnlyCapture:
    transcriber = TestListPendingFields.transcriber

    @pytest.fixture
    def inbox_root(self, tmp_path, monkeypatch):
        """A real (empty) inbox tree, so `spool_capture` writing OUTSIDE it
        is a meaningful assertion rather than a vacuous one."""
        root = tmp_path / "inbox"
        monkeypatch.setattr(scan.settings, "inbox_path", str(root))
        return root

    def _audio_bytes(self, seconds: int = 60) -> bytes:
        return _ftyp(b"M4A ") + _box(b"moov", _mvhd_v0(1000, 1000 * seconds))

    def test_spool_capture_writes_outside_the_inbox_root(self, inbox_root):
        path, kind = scan.spool_capture(self._audio_bytes(), "memo.m4a")

        assert path.exists()
        assert kind == "audio"
        # Not under inbox_root() at all — not even under some bucket-shaped
        # subdirectory of it, since that's the whole point.
        assert scan.inbox_root().resolve() not in path.resolve().parents
        assert path.resolve() != scan.inbox_root().resolve()

    def test_spool_capture_sniffs_non_audio_kinds_too(self, inbox_root):
        path, kind = scan.spool_capture(b"hello world", "note.txt")
        assert kind == "text"
        assert path.read_bytes() == b"hello world"

    def test_audio_success_emails_the_transcript_and_cleans_up(self, inbox_root, transcriber):
        path, kind = scan.spool_capture(self._audio_bytes(seconds=90), "memo.m4a")
        transcriber(text="ring the plumber about the utility room", title="Plumber call notes")

        scan.deliver_capture_by_email(
            path, owner_user_id=1, original_filename="memo.m4a", kind=kind,
        )

        assert len(transcriber.emails) == 1
        to_user_id, subject, body_html, body_text, attachments = transcriber.emails[0]
        assert to_user_id == 1
        assert subject == "Transcript complete: Plumber call notes"
        assert "ring the plumber about the utility room" in body_text
        assert "not filed" in body_html.lower()
        assert len(attachments) == 1
        assert attachments[0].filename == "transcript.txt"

        # Nothing survives the call — no push, and the spool directory is gone.
        assert transcriber.pushes == []
        assert not path.parent.exists()

    def test_audio_failure_attaches_the_original_and_cleans_up(self, inbox_root, transcriber):
        path, kind = scan.spool_capture(self._audio_bytes(), "memo.m4a")
        transcriber(text="", source="none", error="HTTP 503 upstream", transient=True)

        scan.deliver_capture_by_email(
            path, owner_user_id=1, original_filename="memo.m4a", kind=kind,
        )

        assert len(transcriber.emails) == 1
        to_user_id, subject, body_html, body_text, attachments = transcriber.emails[0]
        assert to_user_id == 1
        assert "memo.m4a" in subject
        # One attempt, not three — there is no sweep to retry an email-only
        # capture, so the "after three attempts" wording (which is correct
        # for the filed path) would be wrong here.
        assert "on this attempt" in body_text
        assert "after three attempts" not in body_text
        # The recording is not kept anywhere else, so it has to be attached
        # or it is genuinely gone.
        assert attachments is not None and len(attachments) == 1

        assert not path.parent.exists()

    def test_transcription_unavailable_still_emails_a_failure_notice(self, inbox_root, transcriber):
        path, kind = scan.spool_capture(self._audio_bytes(), "memo.m4a")
        transcriber(available=False)

        scan.deliver_capture_by_email(
            path, owner_user_id=1, original_filename="memo.m4a", kind=kind,
        )

        assert len(transcriber.emails) == 1
        assert transcriber.calls == []  # never even attempted a paid call
        assert not path.parent.exists()

    def test_non_audio_capture_emails_the_document_not_a_transcript(self, inbox_root, transcriber):
        path, kind = scan.spool_capture(b"call the plumber about the boiler", "note.txt")

        scan.deliver_capture_by_email(
            path, owner_user_id=1, original_filename="note.txt", kind=kind,
        )

        assert transcriber.calls == []  # the transcription path was never taken
        assert len(transcriber.emails) == 1
        to_user_id, subject, body_html, body_text, attachments = transcriber.emails[0]
        assert to_user_id == 1
        assert "call the plumber about the boiler" in body_text
        assert "not filed" in body_html.lower() or "answered no" in body_html.lower()
        assert not path.parent.exists()

    def test_non_audio_capture_with_no_extractable_text_still_sends_an_email(self, inbox_root, transcriber):
        """Unlike the filed `_notify_document_email`, which stays quiet when
        there's nothing to say (a push already announced the capture), the
        email-only path has no push — silence here means the caller hears
        nothing at all about a file they just sent."""
        path, kind = scan.spool_capture(b"\x00\x01\x02\x03" * 4, "blob.bin")

        scan.deliver_capture_by_email(
            path, owner_user_id=1, original_filename="blob.bin", kind=kind,
        )

        assert len(transcriber.emails) == 1
        assert not path.parent.exists()

    def test_cleanup_happens_even_when_delivery_raises(self, inbox_root, transcriber, monkeypatch):
        """The spool directory must not survive a bug in the delivery path
        itself — this is the one guarantee `deliver_capture_by_email` makes
        unconditionally."""
        path, kind = scan.spool_capture(self._audio_bytes(), "memo.m4a")

        def _boom(path, kind):
            raise RuntimeError("boom")

        monkeypatch.setattr(scan, "extract_preview", _boom)

        scan.deliver_capture_by_email(
            path, owner_user_id=1, original_filename="memo.m4a", kind=kind,
        )

        assert not path.parent.exists()


class TestHtmlSniffing:
    """A saved web page is text, but previewing it *as* text shows the user a
    doctype declaration.

    Same root cause as the ftyp bug above: Tines posts a bare-UUID filename with
    no extension, so `_EXT_KIND` never fires and the content is all there is.
    """

    def _write(self, tmp_path, content: str, name: str = "20260817-110513-50ca9604"):
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_saved_page_with_no_extension_is_html(self, tmp_path):
        p = self._write(tmp_path, '<!DOCTYPE html><html lang="en"><head>'
                                  "<title>TanStack</title></head><body>hi</body></html>")
        assert scan.sniff_kind(p) == "html"

    @pytest.mark.parametrize("lead", [
        "<!-- generated by a static site builder -->\n",
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        "\n\n   ",
        "﻿",  # UTF-8 BOM — three high bytes that used to fail the printable gate
    ])
    def test_markup_behind_a_preamble_is_still_html(self, tmp_path, lead):
        """A real page rarely starts with the marker on byte zero."""
        p = self._write(tmp_path, lead + "<html><head><title>T</title></head><body>x</body></html>")
        assert scan.sniff_kind(p) == "html"

    @pytest.mark.parametrize("content", [
        "I was reading about <html> tags today and how they nest.",
        '{"note": "the <html> spec", "n": 1}',
        "# Heading\n\nSome prose about the <html> element.",
    ])
    def test_prose_mentioning_markup_is_not_html(self, tmp_path, content):
        """The marker alone is not enough. Misfiling a note as a web page would
        run its text through a tag stripper, deleting what the note *is*."""
        assert scan.sniff_kind(self._write(tmp_path, content)) == "text"

    def test_plain_text_and_markdown_are_unaffected(self, tmp_path):
        assert scan.sniff_kind(self._write(tmp_path, "first day back to work")) == "text"
        assert scan.sniff_kind(self._write(tmp_path, "# NIBE\n\nswapped.", "note.md")) == "markdown"


class TestHtmlPreview:
    def test_title_is_extracted_and_leads_the_summary(self, tmp_path):
        """The whole point: the push says what the page *is*, not `<!DOCTYPE`."""
        p = tmp_path / "20260817-110513-50ca9604"
        p.write_text(
            "<!DOCTYPE html><html><head>"
            "<script>var t=localStorage.getItem('theme')</script>"
            "<title>TanStack | The open-source application stack for the web.</title>"
            "<style>body{margin:0}</style></head>"
            "<body><nav>Docs Blog</nav><p>Headless, type-safe utilities.</p></body></html>",
            encoding="utf-8",
        )
        meta = scan.enrich_one(p)

        assert meta["kind"] == "html"
        assert meta["preview_meta"]["title"] == "TanStack | The open-source application stack for the web."
        summary = scan.summarise(meta, size_bytes=250790)
        assert summary.startswith("web page")
        assert "TanStack" in summary
        assert "DOCTYPE" not in summary

    def test_script_and_style_bodies_are_not_the_preview(self, tmp_path):
        """An inlined <script> is often the largest thing in a saved page, so
        skipping it is the difference between prose and minified JS."""
        p = tmp_path / "page"
        p.write_text(
            "<!DOCTYPE html><html><head><title>T</title>"
            "<style>.a{color:red}</style></head><body>"
            "<script>function veryLongMinifiedThing(){return 1}</script>"
            "<p>Real content here.</p></body></html>",
            encoding="utf-8",
        )
        preview = scan.enrich_one(p)["preview"]
        assert "Real content here." in preview
        assert "veryLongMinifiedThing" not in preview
        assert "color:red" not in preview

    def test_a_real_description_still_outranks_the_title(self, tmp_path):
        """`note` is where vision and triage write. The title is a fallback for
        when nothing better exists, not a competitor."""
        p = tmp_path / "page"
        p.write_text("<!DOCTYPE html><html><head><title>Generic Page Title</title>"
                     "</head><body>x</body></html>", encoding="utf-8")
        meta = scan.enrich_one(p)
        meta["note"] = "the LD2410 datasheet Alex was looking for"

        summary = scan.summarise(meta, size_bytes=1000)
        assert "LD2410 datasheet" in summary
        assert "Generic Page Title" not in summary

    def test_titleless_page_degrades_to_its_text(self, tmp_path):
        p = tmp_path / "page"
        p.write_text("<html><body><p>No title on this one.</p></body></html>", encoding="utf-8")
        meta = scan.enrich_one(p)
        assert "title" not in meta["preview_meta"]
        assert "No title on this one." in scan.summarise(meta, size_bytes=100)

    def test_malformed_markup_does_not_raise(self, tmp_path):
        """A broken page must degrade to 'we know it's a web page', never fail
        the ingest that is trying to report the file arrived."""
        p = tmp_path / "page"
        p.write_text("<!DOCTYPE html><html><head><title>Half a ti", encoding="utf-8")
        meta = scan.enrich_one(p)
        assert meta["kind"] == "html"
        assert scan.summarise(meta, size_bytes=42).startswith("web page")


class TestDescribePendingAnnounces:
    """Images were described and then nobody was told.

    `describe_pending` has written the description into `note` since vision
    shipped, but had no equivalent of `_notify_transcribed` — so an image's only
    push was the ingest-time one, which knows the dimensions and a bare UUID.
    """

    inbox = TestListPendingFields.inbox

    @pytest.fixture
    def vision(self, monkeypatch):
        import app.plugin.capabilities as capabilities
        from app.integrations.vision.facade import VisionResult

        state = {"available": True, "summary": "A screenshot of a Zappi charger schedule",
                 "kind": "screenshot", "text": "", "error": None}
        calls: list = []
        pushes: list[tuple] = []
        push_user_ids: list[int | None] = []

        class _Vision:
            def available(self):
                return state["available"]

            def describe(self, path):
                calls.append(path)
                return VisionResult(
                    summary=state["summary"], kind=state["kind"],
                    text=state["text"], model="gemini-3.5-flash-lite",
                    error=state["error"],
                )

        class _Notify:
            # Signature mirrors `NotificationsFacade.send` exactly, `user_id`
            # included. That is load-bearing rather than incidental: the real
            # `_notify_enriched` wraps its send in `except Exception` (a dropped
            # push must never fail the capture it describes), so a double whose
            # signature has drifted from the facade's raises no visible
            # TypeError — it silently records zero pushes, and every assertion
            # below then reads as "the notification was not sent" when what
            # actually happened is "the test double is out of date". Keep this
            # in step with the facade.
            #
            # `user_id` lands in its own list rather than widening the `pushes`
            # tuple, so the existing three-way unpacks keep working and a
            # routing assertion is additive.
            def send(self, title, body, severity="warning", user_id=None, **kwargs):
                pushes.append((title, body, severity))
                push_user_ids.append(user_id)
                return True

        def fake_get_capability(name):
            if name == "vision.image":
                return _Vision()
            if name == "notify.push":
                return _Notify()
            raise KeyError(name)

        monkeypatch.setattr(capabilities, "get_capability", fake_get_capability)

        def setter(**overrides):
            state.update(overrides)
            return state

        setter.calls = calls
        setter.pushes = pushes
        setter.push_user_ids = push_user_ids
        return setter

    def _image(self, inbox, name="20260817-110133-10f7f27e"):
        p = inbox / "incoming" / name
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        scan.write_sidecar(p, {"original_filename": "BE10BE85-AA94-4457-854B-F4D92C12327C"})
        return p

    def test_description_is_announced(self, inbox, vision):
        path = self._image(inbox)
        counts = scan.describe_pending()

        assert counts["described"] == 1
        assert len(vision.pushes) == 1
        title, body, _severity = vision.pushes[0]
        assert "image" in title
        assert "Zappi charger schedule" in body
        # The failure this fixes: the push used to be able to say nothing but
        # the dimensions and the meaningless upload filename.
        assert "BE10BE85" not in body
        assert scan.read_sidecar(path)["note"] == "A screenshot of a Zappi charger schedule"

    def test_a_single_failure_retries_rather_than_giving_up(self, inbox, vision):
        """Issue #147: a first failed attempt must not be indistinguishable
        from a permanent one — it retries silently, not permanently, and
        gets no push (that would be noise on every transient blip)."""
        self._image(inbox)
        vision(summary="", error="HTTP 429 rate limited")

        counts = scan.describe_pending()
        assert counts["retrying"] == 1
        assert counts["failed"] == 0
        assert vision.pushes == []

    def test_repeated_failures_eventually_give_up_and_announce(self, inbox, vision):
        """After `MAX_VISION_ATTEMPTS` failures, `describe_pending` gives up
        for good — `described_at` is stamped, `vision_failed` is set, and
        (unlike a still-retrying failure) a push goes out, because "will
        never be tried again" is worth telling someone about."""
        path = self._image(inbox)
        vision(summary="", error="HTTP 429 rate limited")

        # Pre-seed as if this is already the last attempt, long enough after
        # its predecessor that the backoff has elapsed — avoids a real sleep.
        meta = scan.read_sidecar(path)
        meta["vision_attempts"] = scan.MAX_VISION_ATTEMPTS - 1
        meta["vision_attempted_at"] = "2000-01-01T00:00:00+00:00"
        scan.write_sidecar(path, meta)

        counts = scan.describe_pending()

        assert counts["failed"] == 1
        assert counts["retrying"] == 0
        meta_after = scan.read_sidecar(path)
        assert meta_after["described_at"]
        assert meta_after["vision_failed"] is True
        assert len(vision.pushes) == 1
        _title, _body, severity = vision.pushes[0]
        assert severity == "warning"

    def test_second_sweep_neither_re_bills_nor_re_announces(self, inbox, vision):
        """Same idempotency contract as transcription — and a duplicate push is
        its own harm, independent of the money."""
        self._image(inbox)
        scan.describe_pending()
        counts = scan.describe_pending()

        assert counts["skipped"] == 1
        assert len(vision.calls) == 1
        assert len(vision.pushes) == 1

    def test_unconfigured_is_silent(self, inbox, vision):
        self._image(inbox)
        vision(available=False)
        assert scan.describe_pending()["considered"] == 0
        assert vision.pushes == []

    def test_legacy_pre_157_failure_is_retried(self, inbox, vision):
        """Issue #160: the pre-#147/#157 `describe_pending` stamped
        `described_at` on the very first attempt regardless of outcome — so
        an image that hit a provider error before that fix landed carries
        `described_at` set, `vision_error` a real error, `note` empty, and
        `vision_attempts` 0 (never actually retried). `_vision_ready_for_retry`
        used to bail out on `described_at` alone, so that row could never be
        picked up again. It must now get its one retry, and `list_pending`
        must stop reporting `described: true` for it while it's still due
        one — that combination reads as "looked at, nothing to say" to a
        human, when the truth is "never actually looked at"."""
        path = self._image(inbox)
        meta = scan.read_sidecar(path)
        meta["described_at"] = "2026-09-06T21:41:00+00:00"
        meta["vision_error"] = "503 UNAVAILABLE"
        scan.write_sidecar(path, meta)

        listed = scan.list_pending(1)
        assert len(listed) == 1
        assert listed[0]["described"] is False

        vision(summary="A screenshot of a Zappi charger schedule", error=None)
        counts = scan.describe_pending()

        assert counts["described"] == 1
        assert len(vision.calls) == 1
        meta_after = scan.read_sidecar(path)
        assert meta_after["note"] == "A screenshot of a Zappi charger schedule"
        assert meta_after.get("vision_error") is None
        assert scan.list_pending(1)[0]["described"] is True


class TestIngestConfirmation:
    """The server announcing a capture itself, rather than the caller doing it.

    This is the last job holding the Tines relay in the capture path. A relay can
    push the route's rendered `summary`; an iOS Shortcut posting directly can move
    bytes and nothing else. Config-gated so that during the transition both paths
    don't announce every capture twice.
    """

    inbox = TestListPendingFields.inbox
    transcriber = TestListPendingFields.transcriber

    @pytest.fixture
    def confirm_enabled(self, monkeypatch):
        """Set `inbox_confirm_push` without touching the config store.

        Deliberately a bare stub rather than a wrapper around the real
        `plugin_config`: calling that instantiates the integration registry, which
        starts resolving every declared capability (`media.store` and friends) and
        drags the whole plugin graph into a filesystem test. `notify_ingested`
        reads exactly one attribute, so one attribute is all this needs to be.
        """
        def _enable(value: bool):
            import app.plugin.config_store as config_store

            class _Cfg:
                inbox_confirm_push = value

            monkeypatch.setattr(config_store, "plugin_config", lambda name: _Cfg())

        return _enable

    def _item(self, inbox):
        path = inbox / "incoming" / "20260817-110513-50ca9604"
        path.write_text(
            "<!DOCTYPE html><html><head><title>TanStack</title></head>"
            "<body><p>Headless utilities.</p></body></html>",
            encoding="utf-8",
        )
        return path, scan.enrich_one(path)

    def test_disabled_by_default_sends_nothing(self, inbox, transcriber, confirm_enabled):
        """Deploying this must change nothing until it's deliberately turned on —
        otherwise every capture is announced twice while Tines is still wired up."""
        confirm_enabled(False)
        path, meta = self._item(inbox)

        assert scan.notify_ingested(meta, path) is False
        assert transcriber.pushes == []

    def test_enabled_announces_what_landed(self, inbox, transcriber, confirm_enabled):
        confirm_enabled(True)
        path, meta = self._item(inbox)

        assert scan.notify_ingested(meta, path) is True
        assert len(transcriber.pushes) == 1
        title, body, _severity = transcriber.pushes[0]
        assert title == "lios: web page captured"
        # The body is the same rendered line `inbox_pending` shows, so a push and
        # the queue can never disagree about what a file is.
        assert "TanStack" in body

    def test_the_facade_never_raises_into_the_route(self, inbox, monkeypatch, confirm_enabled):
        """The file is already on disk by the time this runs. A notification
        failure must not become an error the producer retries a capture over."""
        confirm_enabled(True)
        path, meta = self._item(inbox)

        def boom(*_a, **_kw):
            raise RuntimeError("HA unreachable")

        monkeypatch.setattr(scan, "notify_ingested", boom)

        from app.integrations.inbox.facade import FACADE

        assert FACADE.confirm_ingest(meta, path) is False



class TestDocumentEmail:
    """The `Send To Comar` path's durable half.

    Audio always had two beats — a push on arrival, a transcript minutes later.
    A shared document had only the first, so the text comar had already
    extracted at ingest went nowhere a person would look, and the file was
    rediscovered by accident. These pin the second beat for documents.
    """

    @pytest.fixture
    def inbox(self, tmp_path, monkeypatch):
        root = tmp_path / "inbox"
        monkeypatch.setattr(scan.settings, "inbox_path", str(root))
        user_root = scan.user_root(1)
        (user_root / "incoming").mkdir(parents=True)
        return user_root

    @pytest.fixture
    def emails(self, monkeypatch):
        import app.plugin.capabilities as capabilities

        sent: list[tuple] = []

        class _Email:
            def send_email(self, to_user_id, subject, body_html, body_text=None,
                           attachments=None):
                sent.append((to_user_id, subject, body_html, body_text, attachments))
                return True

        class _Notify:
            def send(self, title, body, severity="warning", user_id=None, **kwargs):
                return True

        def fake_get_capability(name):
            if name == "notify.email":
                return _Email()
            if name == "notify.push":
                return _Notify()
            raise KeyError(name)

        monkeypatch.setattr(capabilities, "get_capability", fake_get_capability)
        return sent

    def _doc(self, inbox, name="20260830-090000-11112222"):
        path = inbox / "incoming" / name
        path.write_text(
            "<!DOCTYPE html><html><head><title>Crannarc</title></head>"
            "<body><p>Quotation 5942 for the Riverside works.</p></body></html>",
            encoding="utf-8",
        )
        return path, scan.enrich_one(path)

    def test_a_document_is_emailed_with_its_extracted_text(self, inbox, emails):
        path, meta = self._doc(inbox)

        assert scan.email_ingested_document(meta, path) is True

        assert len(emails) == 1
        to_user_id, subject, _html, body_text, attachments = emails[0]
        assert to_user_id == 1
        assert "Quotation 5942" in body_text
        # The original travels with it, so the mail is self-contained.
        assert attachments and attachments[0].filename

    def test_audio_is_not_emailed_here(self, inbox, emails):
        """Audio's email is the transcript, which does not exist yet at ingest.
        Sending one here would mean two emails per memo, the second of which
        says less than the first."""
        path = inbox / "incoming" / "20260830-091000-33334444"
        path.write_bytes(_ftyp(b"M4A ") + _box(b"moov", _mvhd_v0(1000, 60000)))
        meta = scan.enrich_one(path)

        assert scan.email_ingested_document(meta, path) is False
        assert emails == []

    def test_an_image_waits_for_the_description_sweep(self, inbox, emails):
        """Same rule, different reason: at ingest an image has dimensions and
        no description, so there is nothing worth sending yet."""
        path, meta = self._doc(inbox, name="20260830-092000-55556666")
        meta["kind"] = "image"

        assert scan.email_ingested_document(meta, path) is False
        assert emails == []

    def test_a_document_with_no_extracted_text_sends_nothing(self, inbox, emails):
        """An email whose body is just a filename trains you to ignore the
        sender. Better to stay quiet and leave the push as the only signal."""
        path, meta = self._doc(inbox, name="20260830-093000-77778888")
        meta["preview"] = ""
        meta.pop("description", None)

        scan.email_ingested_document(meta, path)
        assert emails == []

    def test_an_oversized_original_is_dropped_not_the_email(self, inbox, emails, monkeypatch):
        """The file is a convenience copy — the authoritative one is in the
        inbox and named in the body. Losing the whole notification because an
        attachment was too big would be the wrong trade."""
        monkeypatch.setattr(scan, "EMAIL_ATTACHMENT_MAX_BYTES", 10)
        path, meta = self._doc(inbox, name="20260830-094000-99990000")

        scan.email_ingested_document(meta, path)

        assert len(emails) == 1
        *_rest, body_text, attachments = emails[0]
        assert attachments is None
        assert "too large" in body_text.lower()
