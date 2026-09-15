"""`signals/imaging.py` — ROI crop/downscale ffmpeg filter maths (unit tier).

ffmpeg itself is mocked throughout (`subprocess.run`) — these tests are about
the FILTER STRING built for a given fraction/long-edge, and about
`mean_brightness`'s stdout-byte parsing, not about ffmpeg actually running.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.integrations.signals import imaging
from app.errors import TransientError


def _ok_proc(stdout: bytes = b"", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


@patch("app.integrations.signals.imaging.subprocess.run")
def test_crop_bottom_builds_correct_filter_for_fraction(mock_run, tmp_path):
    mock_run.return_value = _ok_proc()
    src = tmp_path / "in.jpg"
    src.write_bytes(b"x")
    dest = tmp_path / "out.jpg"

    imaging.crop_bottom(src, dest, fraction=0.3)

    cmd = mock_run.call_args[0][0]
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == "crop=iw:ih*0.3:0:ih*(1-0.3)"


@pytest.mark.parametrize("fraction", [0.0, -0.1, 1.1, 2])
@patch("app.integrations.signals.imaging.subprocess.run")
def test_crop_bottom_rejects_out_of_range_fraction(mock_run, tmp_path, fraction):
    src = tmp_path / "in.jpg"
    src.write_bytes(b"x")
    with pytest.raises(ValueError):
        imaging.crop_bottom(src, tmp_path / "out.jpg", fraction=fraction)
    mock_run.assert_not_called()


@patch("app.integrations.signals.imaging.subprocess.run")
def test_downscale_builds_aspect_aware_filter(mock_run, tmp_path):
    mock_run.return_value = _ok_proc()
    src = tmp_path / "in.jpg"
    src.write_bytes(b"x")
    imaging.downscale(src, tmp_path / "out.jpg", long_edge=1024)

    cmd = mock_run.call_args[0][0]
    vf = cmd[cmd.index("-vf") + 1]
    assert "1024" in vf
    assert "gt(iw,ih)" in vf


@patch("app.integrations.signals.imaging.subprocess.run")
def test_mean_brightness_reads_single_stdout_byte(mock_run, tmp_path):
    mock_run.return_value = _ok_proc(stdout=bytes([200]))
    src = tmp_path / "in.jpg"
    src.write_bytes(b"x")

    result = imaging.mean_brightness(src, bottom_fraction=0.3)

    assert result == 200.0
    cmd = mock_run.call_args[0][0]
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.startswith("crop=iw:ih*0.3:0:ih*(1-0.3)")
    assert vf.endswith("scale=1:1")


@patch("app.integrations.signals.imaging.subprocess.run")
def test_mean_brightness_without_roi_has_no_crop_filter(mock_run, tmp_path):
    mock_run.return_value = _ok_proc(stdout=bytes([50]))
    src = tmp_path / "in.jpg"
    src.write_bytes(b"x")

    imaging.mean_brightness(src)

    cmd = mock_run.call_args[0][0]
    vf = cmd[cmd.index("-vf") + 1]
    assert "crop=" not in vf


@patch("app.integrations.signals.imaging.subprocess.run")
def test_mean_brightness_raises_transient_on_empty_stdout(mock_run, tmp_path):
    mock_run.return_value = _ok_proc(stdout=b"")
    src = tmp_path / "in.jpg"
    src.write_bytes(b"x")
    with pytest.raises(TransientError):
        imaging.mean_brightness(src)
