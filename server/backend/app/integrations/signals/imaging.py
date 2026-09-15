"""Small ffmpeg-shelled image ops for watchers — no Pillow dependency.

Added after looking at a real milkman delivery clip (2026-09-11): the milk
lands directly beneath the camera, so the delivered item appears only at the
very bottom edge of the frame, partly cut off, centred. Sending vision a
downscaled full frame alone loses exactly the detail that matters. So every
comparison sends TWO images per timepoint: a downscaled full frame (context —
"is anyone/anything on the step at all") and a full-resolution crop of the
bottom `roi_bottom_fraction` of the frame (detail — where the item actually
appears).

Kept out of `vision/client.py` deliberately: vision stays dependency-light on
the image-format side (accepts whatever Gemini accepts, sends the raw bytes),
and cropping/downscaling is a watcher-framework concern, not a vision one.
Uses the `ffmpeg` binary this integration already depends on for frame grabs,
not a second image library.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from app.errors import TransientError


def _ffmpeg_bin() -> str:
    return os.environ.get("FFMPEG", "").strip() or shutil.which("ffmpeg") or "ffmpeg"


def downscale(src: Path, dest: Path, *, long_edge: int = 1024, timeout: int = 15) -> Path:
    """Write `src` to `dest`, its long edge resized to `long_edge` (never
    upscaled — `min(iw,long_edge)` style would need two passes, so instead
    this trusts callers to only downscale real camera frames, which are
    always larger than 1024px on their long edge in this fleet).

    Both dimensions are forced even (`scale` requires it for some codecs;
    `-2` keeps the aspect ratio and rounds to the nearest even number).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf = f"scale='if(gt(iw,ih),{long_edge},-2)':'if(gt(iw,ih),-2,{long_edge})'"
    cmd = [
        _ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", str(src), "-vf", vf, "-frames:v", "1", "-y", str(dest),
    ]
    _run(cmd, timeout, f"downscale {src.name}")
    return dest


def crop_bottom(src: Path, dest: Path, *, fraction: float, timeout: int = 15) -> Path:
    """Write the bottom `fraction` of `src` to `dest`, at full resolution."""
    if not 0 < fraction <= 1:
        raise ValueError(f"fraction must be in (0, 1], got {fraction!r}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    crop = f"crop=iw:ih*{fraction}:0:ih*(1-{fraction})"
    cmd = [
        _ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", str(src), "-vf", crop, "-frames:v", "1", "-y", str(dest),
    ]
    _run(cmd, timeout, f"crop_bottom {src.name}")
    return dest


def mean_brightness(path: Path, *, bottom_fraction: float | None = None, timeout: int = 15) -> float:
    """Mean grayscale brightness (0-255) of `path`, optionally restricted to
    its bottom `bottom_fraction`. Used to detect "someone in hi-vis is
    standing on the step right now" (a candidate frame's bottom-strip mean
    wildly brighter than the baseline's) so the milk watcher can wait them
    out rather than ask vision about a frame that's mostly a person.

    Implementation: scale the (optionally cropped) region to a single pixel
    and read its raw gray value off stdout — one byte, no parsing of
    ffmpeg's own stats filters.
    """
    vf = "format=gray"
    if bottom_fraction is not None:
        if not 0 < bottom_fraction <= 1:
            raise ValueError(f"bottom_fraction must be in (0, 1], got {bottom_fraction!r}")
        vf = f"crop=iw:ih*{bottom_fraction}:0:ih*(1-{bottom_fraction}),{vf}"
    vf += ",scale=1:1"
    cmd = [
        _ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", str(path), "-vf", vf, "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise TransientError(f"ffmpeg exceeded {timeout}s measuring brightness of {path.name}") from exc
    if proc.returncode != 0 or not proc.stdout:
        stderr = proc.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        raise TransientError(
            f"ffmpeg rc={proc.returncode} measuring brightness of {path.name}: "
            f"{(stderr or '')[:200]}"
        )
    return float(proc.stdout[0])


def _run(cmd: list[str], timeout: int, what: str) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise TransientError(f"ffmpeg exceeded {timeout}s: {what}") from exc
    if proc.returncode != 0:
        raise TransientError(f"ffmpeg rc={proc.returncode} ({what}): {proc.stderr.strip()[:200]}")
