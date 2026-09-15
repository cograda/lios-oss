"""Voice-memo watcher — settling, eligibility, and the upload ledger.

What matters here is what it *doesn't* do:

  - doesn't upload a recording that's still being written (a memo grows from the
    moment recording starts, so the first fsevent is a truncated file)
  - doesn't upload the same recording twice (each upload costs a server-side
    transcription)
  - doesn't ledger an upload that failed (which would silently lose the memo)

Note the client ledger is an optimisation, not the authority — the server
deduplicates by content hash, because the phone/Tines producer and this watcher
can't see each other's uploads. These tests cover the local half.
"""

from __future__ import annotations

import json
import struct
from unittest.mock import MagicMock

import pytest

from lios_sync.voice_memos import (
    MAX_BYTES,
    MIN_BYTES,
    UploadLedger,
    VoiceMemoHandler,
    _fingerprint,
    start_voice_memo_watcher,
)


def _audio(tmp_path, name="20260731 120000.m4a", size=MIN_BYTES * 2, filler=b"\x01"):
    """A file that looks like a recording. Contents don't matter to the client —
    it never parses the container; that's the server's job."""
    path = tmp_path / name
    header = struct.pack(">I", 28) + b"ftyp" + b"M4A " + b"\x00" * 16
    path.write_bytes(header + filler * max(0, size - len(header)))
    return path


@pytest.fixture
def handler(tmp_path):
    ledger = UploadLedger(tmp_path / "ledger.json")
    client = MagicMock()
    client.ingest_inbox_file.return_value = {"ok": True}
    return VoiceMemoHandler(client, ledger), client


class TestEligibility:
    def test_uploads_a_normal_recording(self, tmp_path, handler):
        h, client = handler
        assert h.upload(_audio(tmp_path)) is True
        assert client.ingest_inbox_file.call_count == 1

    def test_skips_a_mis_tap(self, tmp_path, handler):
        """31 of 373 real memos were sub-threshold accidental taps."""
        h, client = handler
        assert h.upload(_audio(tmp_path, size=MIN_BYTES - 1)) is False
        client.ingest_inbox_file.assert_not_called()

    def test_skips_oversized(self, tmp_path, handler):
        """The server rejects these anyway; no point spending the bandwidth."""
        h, client = handler
        big = tmp_path / "long.m4a"
        big.write_bytes(b"\x00" * (MAX_BYTES + 1))
        assert h.upload(big) is False
        client.ingest_inbox_file.assert_not_called()

    def test_missing_file_is_not_an_error(self, tmp_path, handler):
        h, _ = handler
        assert h.upload(tmp_path / "gone.m4a") is False

    def test_sends_type_and_provenance(self, tmp_path, handler):
        h, client = handler
        h.upload(_audio(tmp_path))
        kwargs = client.ingest_inbox_file.call_args.kwargs
        assert kwargs["file_type"] == "audio"
        assert kwargs["metadata"]["source"] == "voice-memo"
        assert kwargs["metadata"]["recorded_at"]

    def test_sends_raw_bytes_and_parses_nothing(self, tmp_path, handler):
        """The daemon is a transport. No duration, no transcript, no OpenAI key."""
        h, client = handler
        path = _audio(tmp_path)
        h.upload(path)
        _name, data = client.ingest_inbox_file.call_args.args
        assert data == path.read_bytes()
        assert "duration" not in client.ingest_inbox_file.call_args.kwargs["metadata"]


class TestLedger:
    def test_second_upload_is_skipped(self, tmp_path, handler):
        h, client = handler
        path = _audio(tmp_path)
        h.upload(path)
        assert h.upload(path) is False
        assert client.ingest_inbox_file.call_count == 1

    def test_survives_a_restart(self, tmp_path):
        """A daemon restart must not re-upload — that's a second charge."""
        ledger_path = tmp_path / "ledger.json"
        path = _audio(tmp_path)

        first = VoiceMemoHandler(MagicMock(), UploadLedger(ledger_path))
        first.upload(path)

        client = MagicMock()
        second = VoiceMemoHandler(client, UploadLedger(ledger_path))
        assert second.upload(path) is False
        client.ingest_inbox_file.assert_not_called()

    def test_failed_upload_is_not_ledgered(self, tmp_path):
        """Otherwise a network blip silently loses the memo forever."""
        client = MagicMock()
        client.ingest_inbox_file.side_effect = RuntimeError("network down")
        h = VoiceMemoHandler(client, UploadLedger(tmp_path / "ledger.json"))
        path = _audio(tmp_path)

        with pytest.raises(RuntimeError):
            h.upload(path)
        assert h.failed == 1

        client.ingest_inbox_file.side_effect = None
        client.ingest_inbox_file.return_value = {"ok": True}
        assert h.upload(path) is True

    def test_rename_does_not_cause_a_re_upload(self, tmp_path, handler):
        """Voice Memos renames files when a memo is retitled, so the ledger is
        keyed on content rather than path."""
        h, client = handler
        path = _audio(tmp_path, name="New Recording 12.m4a")
        h.upload(path)

        renamed = path.parent / "Riverside call.m4a"
        path.rename(renamed)
        assert h.upload(renamed) is False
        assert client.ingest_inbox_file.call_count == 1

    def test_distinct_recordings_are_distinguished(self, tmp_path, handler):
        h, client = handler
        h.upload(_audio(tmp_path, name="a.m4a", filler=b"\x01"))
        h.upload(_audio(tmp_path, name="b.m4a", filler=b"\x02"))
        assert client.ingest_inbox_file.call_count == 2

    def test_corrupt_ledger_raises_rather_than_re_uploading(self, tmp_path):
        """Silently treating an unreadable ledger as empty would re-transcribe
        everything at cost. Loud failure with manual recovery is correct."""
        bad = tmp_path / "ledger.json"
        bad.write_text("{ not json")
        with pytest.raises(Exception):
            UploadLedger(bad)

    def test_ledger_is_persisted_immediately(self, tmp_path, handler):
        h, _ = handler
        h.upload(_audio(tmp_path))
        assert json.loads(h.ledger.path.read_text())


class TestFingerprint:
    def test_stable_across_reads(self, tmp_path):
        path = _audio(tmp_path)
        assert _fingerprint(path) == _fingerprint(path)

    def test_size_change_changes_it(self, tmp_path):
        """A growing file must not look identical to the finished one."""
        path = _audio(tmp_path)
        before = _fingerprint(path)
        with path.open("ab") as handle:
            handle.write(b"\x09" * 4096)
        assert _fingerprint(path) != before

    def test_small_file_does_not_seek_past_start(self, tmp_path):
        """Files under the tail-read threshold must not raise on the seek."""
        path = tmp_path / "tiny.m4a"
        path.write_bytes(b"\x00" * 1024)
        assert _fingerprint(path)


class TestSettling:
    def test_growing_file_reschedules_instead_of_uploading(self, tmp_path, handler):
        """A memo appears when recording *starts*; uploading then ships a stub."""
        h, client = handler
        path = _audio(tmp_path)

        h._schedule(str(path))
        assert str(path) in h._timers
        timer = h._timers[str(path)]

        h._schedule(str(path))  # another write event
        assert timer is not h._timers[str(path)], "timer should have been replaced"
        client.ingest_inbox_file.assert_not_called()

        h._timers[str(path)].cancel()

    def test_non_audio_is_ignored(self, tmp_path, handler):
        """The container also holds CloudRecordings.db and its WAL, which churn."""
        h, _ = handler
        db = tmp_path / "CloudRecordings.db-wal"
        db.write_bytes(b"\x00" * (MIN_BYTES * 2))
        h._schedule(str(db))
        assert h._timers == {}

    def test_qta_is_watched_too(self, tmp_path, handler):
        """Memos synced from an iPhone land as .qta."""
        h, _ = handler
        path = _audio(tmp_path, name="synced.qta")
        h._schedule(str(path))
        assert str(path) in h._timers
        h._timers[str(path)].cancel()

    def test_file_growing_between_settle_and_read_is_deferred(self, tmp_path, handler, monkeypatch):
        """Guards the race the settle window is meant to close."""
        h, client = handler
        path = _audio(tmp_path)
        real_read = type(path).read_bytes

        def grow_then_read(self_path):
            # Grow BEFORE reading, so the read returns more than the earlier
            # stat() saw — which is the actual race: the recording was still
            # being written when we decided it had settled.
            with self_path.open("ab") as handle:
                handle.write(b"\x07" * 8192)
            return real_read(self_path)

        monkeypatch.setattr(type(path), "read_bytes", grow_then_read)
        # Length is re-checked against the pre-read stat, so the mismatch defers.
        assert h.upload(path) is False
        client.ingest_inbox_file.assert_not_called()


class TestStartup:
    def test_missing_container_returns_none(self, tmp_path, handler):
        h, client = handler
        assert start_voice_memo_watcher(client, tmp_path / "nope") is None

    def test_backfill_is_not_automatic(self, tmp_path):
        """`scan_existing` must be an explicit command: a fresh install
        auto-uploading years of memos would bill a surprise."""
        import inspect

        from lios_sync import daemon

        source = inspect.getsource(daemon.run_daemon)
        assert "scan_existing" not in source


class TestScanExisting:
    def test_uploads_eligible_and_skips_the_rest(self, tmp_path, monkeypatch, handler):
        h, client = handler
        import lios_sync.voice_memos as vm

        _audio(tmp_path, name="a.m4a", filler=b"\x01")
        _audio(tmp_path, name="b.m4a", filler=b"\x02")
        _audio(tmp_path, name="tap.m4a", size=MIN_BYTES - 1, filler=b"\x03")
        (tmp_path / "CloudRecordings.db").write_bytes(b"\x00" * (MIN_BYTES * 2))
        monkeypatch.setattr(vm, "RECORDINGS_DIR", tmp_path)

        counts = h.scan_existing()

        assert counts["considered"] == 3, "the .db file must not be considered"
        assert counts["uploaded"] == 2
        assert counts["skipped"] == 1, "the sub-threshold tap"
        assert client.ingest_inbox_file.call_count == 2

    def test_limit_bounds_a_backfill(self, tmp_path, monkeypatch, handler):
        h, client = handler
        import lios_sync.voice_memos as vm

        for i in range(5):
            _audio(tmp_path, name=f"memo{i}.m4a", filler=bytes([i + 1]))
        monkeypatch.setattr(vm, "RECORDINGS_DIR", tmp_path)

        counts = h.scan_existing(limit=2)
        assert counts["uploaded"] == 2
        assert client.ingest_inbox_file.call_count == 2
