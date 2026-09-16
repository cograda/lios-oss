"""`signals/cameras.py` — camera registry (env-derived) + frame retention."""

from __future__ import annotations

import time
from pathlib import Path

from app.integrations.signals import cameras


def test_camera_registry_reads_env(monkeypatch):
    monkeypatch.setenv("HOME_CAMERA_FRONT_DOOR_RTSP", "rtsps://example/front")
    monkeypatch.setenv("HOME_CAMERA_SHED_RTSP", "rtsp://example/shed")
    monkeypatch.delenv("HOME_CAMERA_EMPTY_RTSP", raising=False)

    registry = cameras.camera_registry()

    assert registry["front_door"] == "rtsps://example/front"
    assert registry["shed"] == "rtsp://example/shed"
    assert cameras.camera_url("FRONT_DOOR") == "rtsps://example/front"
    assert cameras.camera_url("unknown_camera") is None


def test_camera_registry_ignores_blank_values(monkeypatch):
    monkeypatch.setenv("HOME_CAMERA_BLANK_RTSP", "   ")
    assert "blank" not in cameras.camera_registry()


def test_prune_frames_deletes_old_files_and_empty_dirs(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "inbox_path", str(tmp_path))
    root = tmp_path / "signals" / "frames" / "2026" / "01" / "01"
    root.mkdir(parents=True)
    old_file = root / "front_door-000000.jpg"
    old_file.write_bytes(b"x")
    old_time = time.time() - 40 * 86400
    import os
    os.utime(old_file, (old_time, old_time))

    fresh_dir = tmp_path / "signals" / "frames" / "2026" / "09" / "10"
    fresh_dir.mkdir(parents=True)
    fresh_file = fresh_dir / "front_door-120000.jpg"
    fresh_file.write_bytes(b"y")

    removed = cameras.prune_frames(30)

    assert removed == 1
    assert not old_file.exists()
    assert not root.exists()  # emptied directory removed
    assert fresh_file.exists()


def test_prune_frames_missing_root_is_a_noop(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "inbox_path", str(tmp_path / "does_not_exist"))
    assert cameras.prune_frames(30) == 0


def test_frame_path_shape(monkeypatch):
    from datetime import datetime
    from app.config import settings

    monkeypatch.setattr(settings, "inbox_path", "/inbox")
    when = datetime(2026, 9, 11, 21, 5, 30)
    path = cameras.frame_path("front_door", when)
    assert str(path) == "/inbox/signals/frames/2026/09/11/front_door-210530.jpg"
