"""Camera registry + RTSP frame grabs.

The registry is read straight from the environment (`HOME_CAMERA_<NAME>_RTSP`),
never through `plugin_config()` — the set of cameras isn't known in advance,
so there's no static `config_schema` key to declare per camera, and these
values are exactly the kind of thing that must never be logged (an RTSP URL
embeds a per-channel alias that is effectively a credential — see
`house/patio-timelapse/grab_frame.py`'s own docstring on this). Same shape as
that script's `site.env` seam, just read from process env instead of a file.

`grab_frame()` is the one proven incantation from that script, adapted:
`-rtsp_transport tcp` (UDP loses single-frame grabs to torn macroblocks) and
a structural JPEG-completeness check (a stream drop mid-grab exits 0 having
written only a header).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.errors import PermanentError, TransientError

logger = logging.getLogger(__name__)

_CAMERA_ENV_RE = re.compile(r"^HOME_CAMERA_([A-Z0-9_]+)_RTSP$")

# Matches grab_frame.py's calibration: good frames were 336-510 KB at
# 2688x1512 q2; a torn/short grab measured ~31 KB. Front-door frames here are
# 1920x2560 (measured 2026-09-11, ~700 KB), so this floor still sits well
# under a real frame while catching a truncated one.
MIN_FRAME_BYTES = int(os.environ.get("HOME_SIGNALS_MIN_FRAME_BYTES", "50000"))

DEFAULT_TIMEOUT_SEC = 20


def camera_registry() -> dict[str, str]:
    """`{camera_name: rtsp_url}`, lower-cased names, read fresh from env.

    Not cached — config can change between calls (tests monkeypatch env),
    and this is at most a handful of entries.
    """
    registry: dict[str, str] = {}
    for key, value in os.environ.items():
        m = _CAMERA_ENV_RE.match(key)
        if m and value.strip():
            registry[m.group(1).lower()] = value.strip()
    return registry


def camera_url(camera: str) -> str | None:
    return camera_registry().get(camera.lower())


def _ffmpeg_bin() -> str:
    return os.environ.get("FFMPEG", "").strip() or shutil.which("ffmpeg") or "ffmpeg"


def _is_complete_jpeg(path: Path) -> bool:
    """Structural check: SOI/EOI markers present and size above the floor.

    Same lesson as `grab_frame.py::is_complete_jpeg` — ffmpeg can exit 0
    having written a truncated file when the stream drops mid-grab.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    if size < MIN_FRAME_BYTES:
        return False
    try:
        with path.open("rb") as fh:
            if fh.read(2) != b"\xff\xd8":
                return False
            fh.seek(-2, os.SEEK_END)
            return fh.read(2) == b"\xff\xd9"
    except OSError:
        return False


def grab_frame(
    camera: str,
    dest: Path,
    *,
    url: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> Path:
    """Grab one frame from `camera`'s RTSP stream to `dest`. Returns `dest`.

    Raises `PermanentError` if the camera isn't configured (retrying with no
    config change is pointless) and `TransientError` for anything ffmpeg-side
    (a dropped stream, a timeout, a truncated frame) — the caller (a watcher
    poll, or `watch_test`) decides whether to retry.
    """
    rtsp_url = url if url is not None else camera_url(camera)
    if not rtsp_url:
        raise PermanentError(
            f"no RTSP URL configured for camera {camera!r} — set "
            f"HOME_CAMERA_{camera.upper()}_RTSP"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".grab-", suffix=".jpg")
    os.close(fd)
    tmp = Path(tmp_name)

    cmd = [
        _ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-timeout", str(timeout * 1_000_000),
        "-i", rtsp_url,
        "-frames:v", "1", "-q:v", "2",
        "-y", str(tmp),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout + 15,
        )
    except subprocess.TimeoutExpired as exc:
        tmp.unlink(missing_ok=True)
        raise TransientError(f"ffmpeg exceeded {timeout + 15}s grabbing {camera!r}") from exc
    finally:
        pass

    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        # Never log rtsp_url — it may appear in ffmpeg's stderr, so the
        # message is truncated to a length that (in practice) drops it, and
        # callers must not print `cmd` either.
        raise TransientError(
            f"ffmpeg rc={proc.returncode} grabbing {camera!r}: {proc.stderr.strip()[:200]}"
        )
    if not _is_complete_jpeg(tmp):
        size = tmp.stat().st_size if tmp.exists() else 0
        tmp.unlink(missing_ok=True)
        raise TransientError(f"incomplete frame grabbing {camera!r} ({size} bytes)")

    os.replace(tmp, dest)
    return dest


def frame_dir(camera: str, when: datetime | None = None) -> Path:
    """`signals/frames/YYYY/MM/DD/` under the same volume `inbox` uses."""
    from app.config import settings

    when = when or datetime.now(timezone.utc).astimezone()
    return Path(settings.inbox_path) / "signals" / "frames" / f"{when:%Y}" / f"{when:%m}" / f"{when:%d}"


def frame_path(camera: str, when: datetime | None = None) -> Path:
    when = when or datetime.now(timezone.utc).astimezone()
    return frame_dir(camera, when) / f"{camera}-{when:%H%M%S}.jpg"


def prune_frames(days: int) -> int:
    """Delete frames older than `days`. Returns the count removed."""
    from app.config import settings

    root = Path(settings.inbox_path) / "signals" / "frames"
    if not root.exists():
        return 0
    import time as _time

    cutoff = _time.time() - days * 86400
    removed = 0
    for jpg in root.rglob("*.jpg"):
        try:
            if jpg.stat().st_mtime < cutoff:
                jpg.unlink()
                removed += 1
        except OSError:
            continue
    for d in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if d.is_dir():
            try:
                if not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                continue
    return removed


async def prune_frames_task() -> None:
    """Cron entry — `signals_prune_frames`."""
    from app.plugin import config_store as _config_store

    cfg = _config_store.plugin_config("signals")
    days = cfg.signals_frame_retention_days or 30
    removed = prune_frames(days)
    if removed:
        logger.info("[signals] pruned %d frames older than %dd", removed, days)
