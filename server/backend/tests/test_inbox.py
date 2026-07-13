"""Tests for inbox ingestion logic.

Since inbox.py depends on FastAPI and app.config (not available in test
env without Docker), we test the file-writing logic directly by
replicating the core algorithm.
"""

import base64
import json
import re
import uuid
from datetime import datetime
from pathlib import Path

import pytest


def _default_ext(file_type: str) -> str:
    """Replicated from inbox.py for testing."""
    return {
        "audio": ".m4a",
        "image": ".jpg",
        "text": ".txt",
        "file": ".bin",
    }.get(file_type, "")


ALLOWED_TYPES = {"audio", "image", "text", "file"}


def _ingest_file(inbox_path: str, file_type: str, data_b64: str,
                 filename: str = "", metadata: dict | None = None) -> dict:
    """Core ingestion logic extracted from the endpoint handler.

    Returns the same dict as the endpoint on success, or raises ValueError.
    """
    if file_type not in ALLOWED_TYPES:
        raise ValueError(f"Invalid type: {file_type}")

    file_bytes = base64.b64decode(data_b64)

    if filename:
        ext = Path(filename).suffix or ""
    else:
        ext = _default_ext(file_type)
        filename = f"inbox{ext}"

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    short_id = uuid.uuid4().hex[:8]
    dest_name = f"{ts}-{short_id}{ext}"

    inbox_dir = Path(inbox_path) / file_type
    inbox_dir.mkdir(parents=True, exist_ok=True)
    dest_path = inbox_dir / dest_name

    dest_path.write_bytes(file_bytes)

    if metadata:
        meta_path = dest_path.with_suffix(dest_path.suffix + ".meta.json")
        meta_path.write_text(json.dumps({
            "original_filename": filename,
            "source": metadata.get("source", "unknown"),
            "note": metadata.get("note", ""),
            "ingested_at": datetime.now().isoformat(),
        }, indent=2))

    return {
        "ok": True,
        "type": file_type,
        "filename": dest_name,
        "size_bytes": len(file_bytes),
        "path": str(dest_path),
    }


class TestDefaultExtension:
    def test_audio(self):
        assert _default_ext("audio") == ".m4a"

    def test_image(self):
        assert _default_ext("image") == ".jpg"

    def test_text(self):
        assert _default_ext("text") == ".txt"

    def test_file(self):
        assert _default_ext("file") == ".bin"

    def test_unknown(self):
        assert _default_ext("unknown") == ""


class TestAllowedTypes:
    def test_has_expected_types(self):
        assert ALLOWED_TYPES == {"audio", "image", "text", "file"}


class TestIngestion:
    @pytest.fixture
    def inbox_dir(self, tmp_path):
        return str(tmp_path / "inbox")

    def test_writes_text_file(self, inbox_dir):
        content = b"Hello, world!"
        resp = _ingest_file(inbox_dir, "text", base64.b64encode(content).decode(), "note.txt")

        assert resp["ok"] is True
        assert resp["type"] == "text"
        assert resp["size_bytes"] == len(content)
        dest = Path(resp["path"])
        assert dest.exists()
        assert dest.read_bytes() == content
        assert dest.parent.name == "text"

    def test_writes_audio_file(self, inbox_dir):
        resp = _ingest_file(inbox_dir, "audio", base64.b64encode(b"fake-audio").decode(), "rec.m4a")
        assert resp["ok"] is True
        assert Path(resp["path"]).parent.name == "audio"

    def test_writes_metadata_sidecar(self, inbox_dir):
        resp = _ingest_file(
            inbox_dir, "audio",
            base64.b64encode(b"audio").decode(), "recording.m4a",
            metadata={"source": "shortcut", "note": "Meeting notes"},
        )
        dest = Path(resp["path"])
        meta_path = dest.with_suffix(dest.suffix + ".meta.json")
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["source"] == "shortcut"
        assert meta["note"] == "Meeting notes"
        assert meta["original_filename"] == "recording.m4a"

    def test_no_sidecar_without_metadata(self, inbox_dir):
        resp = _ingest_file(inbox_dir, "text", base64.b64encode(b"test").decode())
        dest = Path(resp["path"])
        meta_path = dest.with_suffix(dest.suffix + ".meta.json")
        assert not meta_path.exists()

    def test_default_extension_when_no_filename(self, inbox_dir):
        resp = _ingest_file(inbox_dir, "image", base64.b64encode(b"img").decode())
        assert resp["filename"].endswith(".jpg")

    def test_preserves_custom_extension(self, inbox_dir):
        resp = _ingest_file(inbox_dir, "audio", base64.b64encode(b"riff").decode(), "voice.wav")
        assert resp["filename"].endswith(".wav")

    def test_rejects_invalid_type(self, inbox_dir):
        with pytest.raises(ValueError, match="Invalid type"):
            _ingest_file(inbox_dir, "video", base64.b64encode(b"test").decode())

    def test_all_types_create_subdirectories(self, inbox_dir):
        for file_type in ["audio", "image", "text", "file"]:
            resp = _ingest_file(inbox_dir, file_type, base64.b64encode(b"test").decode())
            assert resp["ok"] is True
            assert (Path(inbox_dir) / file_type).is_dir()

    def test_filename_format(self, inbox_dir):
        resp = _ingest_file(inbox_dir, "text", base64.b64encode(b"test").decode(), "note.txt")
        assert re.match(r"\d{8}-\d{6}-[a-f0-9]{8}\.txt", resp["filename"])

    def test_unique_filenames(self, inbox_dir):
        """Each ingestion produces a unique filename."""
        data = base64.b64encode(b"test").decode()
        names = {_ingest_file(inbox_dir, "text", data)["filename"] for _ in range(10)}
        assert len(names) == 10
