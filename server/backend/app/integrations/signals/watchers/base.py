"""The `Watcher` state machine: window -> baseline -> event/poll -> verdict.

One `WatchRun` row per watcher per night (see `models.py`). The flow:

  1. The window opens (`get_or_open_run`) — a baseline frame is grabbed and
     the run row created with `status="watching"`.
  2. Every inlet event that `matches()` this watcher, and every scheduled
     poll inside the window, calls `check()` — which waits `settle_seconds`
     (event path only), grabs a candidate frame, asks vision to compare it
     against the baseline, and either marks the run `detected` (pushing a
     notification) or leaves it `watching` for the next check.
  3. Once `detected`, `check()` is a no-op (debounce) — see its own guard.
  4. The window closes (`close_run`) — if still `watching`, marked `none`
     and (only if the watcher opts in) a "no detection" push goes out.

IO (frame grabs, vision calls, HA pushes) all sit behind small methods so
`window.py`'s pure maths and this class's control flow can be tested with a
fake grabber/vision/notify rather than a live camera and a live model.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from app.integrations.signals import cameras, imaging
from app.integrations.signals.models import SignalEvent, WatchRun
from app.integrations.signals.watchers.window import DUBLIN, Window, active_window

logger = logging.getLogger(__name__)


@dataclass
class Watcher:
    name: str
    camera: str
    weekdays: frozenset  # {0..6}, Monday=0 (date.weekday())
    start: "datetime.time"
    end: "datetime.time"
    question: str

    # Detection push — Alex's exact wording (2026-09-11): fixed strings, not
    # templated with time/where/confidence. That detail lives on the
    # `WatchRun` row (`frame_path`, `confidence`, `answer`), not the push.
    detected_title: str = "Signal"
    detected_message: str = "Detected."

    # "No detection tonight" push — OFF by default (2026-09-11: Alex is
    # reviewing phone-alert volume and wants fewer pushes, not more). A
    # watcher opts in explicitly.
    notify_on_close: bool = False
    close_title: str = "Signal"
    close_message: str = "No detection tonight."

    # Merged into the HA notify payload's `data` on a DETECTION push only —
    # e.g. `{"entity_id": "camera.front_door_high_resolution_channel"}` so
    # the phone shows a live camera view, as `gate_package_alert.yaml`'s
    # automation does. Not sent on the close ("no detection") push.
    notify_data: dict | None = None

    confidence_threshold: float = 0.7
    kinds: frozenset = field(default_factory=lambda: frozenset({"person"}))
    # None = any device on this source triggers a check; a real set
    # restricts to devices resolved (via `signals_devices`) to those names.
    device_names: frozenset | None = None
    source: str = "protect"

    settle_seconds: int = 45
    poll_minutes: int = 15

    # ROI crop (2026-09-11, from a real delivery clip): the milk lands
    # directly beneath the camera, visible only at the very bottom edge of
    # the frame. None disables ROI entirely (a plain two-image, downscaled-
    # full-frame compare) — set for any watcher whose subject is near-field.
    roi_bottom_fraction: float | None = None
    # If the candidate's bottom-strip mean brightness is this many times the
    # baseline's, treat it as "someone (likely hi-vis) is standing on the
    # step right now" and wait `brightness_retry_delay`s before re-grabbing.
    brightness_ratio_threshold: float = 1.6
    brightness_retry_delay: int = 30

    def matches(self, event: SignalEvent) -> bool:
        if event.source != self.source:
            return False
        if self.kinds and event.kind not in self.kinds:
            return False
        if self.device_names is not None and event.device_name not in self.device_names:
            return False
        return True

    # ------------------------------------------------------------------
    # Window lifecycle
    # ------------------------------------------------------------------
    def current_window(self, now: datetime | None = None) -> Window | None:
        now = now or datetime.now(timezone.utc)
        return active_window(now, self.weekdays, self.start, self.end)

    def get_or_open_run(self, session: Session, window: Window) -> WatchRun:
        run = (
            session.query(WatchRun)
            .filter_by(watcher=self.name, night_date=window.night_date.isoformat())
            .one_or_none()
        )
        if run is not None:
            return run
        run = WatchRun(
            watcher=self.name,
            night_date=window.night_date.isoformat(),
            opened_at=window.open_dt,
            status="watching",
            checks=0,
        )
        session.add(run)
        session.flush()
        try:
            baseline = self.grab(window.night_date, label="baseline")
            run.baseline_path = str(baseline)
        except Exception:  # noqa: BLE001
            logger.exception("[signals] %s: baseline grab failed", self.name)
        session.commit()
        return run

    def close_run(self, session: Session, run: WatchRun) -> None:
        if run.status != "watching":
            return
        run.status = "none"
        session.commit()
        if self.notify_on_close:
            self._send_notification(self.close_title, self.close_message)

    # ------------------------------------------------------------------
    # The check itself
    # ------------------------------------------------------------------
    async def check(self, session: Session, run: WatchRun, *, wait_settle: bool) -> None:
        """Grab a candidate frame and (maybe) ask vision. No-ops once the
        run is already `detected` — repeated triggers in one window must
        not re-spend a vision call or re-notify."""
        if run.status != "watching":
            return

        if wait_settle and self.settle_seconds:
            await asyncio.sleep(self.settle_seconds)

        night_date = date.fromisoformat(run.night_date)
        try:
            candidate = self.grab(night_date, label="check")
        except Exception:  # noqa: BLE001
            logger.exception("[signals] %s: candidate grab failed", self.name)
            return

        candidate = await self._maybe_wait_out_brightness(run, candidate, night_date)

        baseline_path = Path(run.baseline_path) if run.baseline_path else candidate
        try:
            images, order = self.build_compare_images(baseline_path, candidate)
        except Exception:  # noqa: BLE001
            logger.exception("[signals] %s: image prep failed", self.name)
            return

        result = self.ask_vision(images, order)

        run.checks += 1
        run.frame_path = str(candidate)
        run.confidence = result.get("confidence")
        run.answer = result
        run.model = result.get("model")

        if result.get("answer") and (result.get("confidence") or 0) >= self.confidence_threshold:
            run.status = "detected"
            run.detected_at = datetime.now(timezone.utc)
            session.commit()
            self._send_notification(self.detected_title, self.detected_message, data=self.notify_data)
        else:
            session.commit()

    async def _maybe_wait_out_brightness(
        self, run: WatchRun, candidate: Path, night_date: date,
    ) -> Path:
        """Re-grab once if the candidate's ROI is far brighter than the
        baseline's — a person (often in hi-vis, which saturates IR) is
        likely standing on the step. Best-effort: any failure measuring
        brightness proceeds with the original candidate rather than
        blocking the check."""
        if not self.roi_bottom_fraction or not run.baseline_path:
            return candidate
        try:
            baseline_bright = imaging.mean_brightness(
                Path(run.baseline_path), bottom_fraction=self.roi_bottom_fraction
            )
            candidate_bright = imaging.mean_brightness(
                candidate, bottom_fraction=self.roi_bottom_fraction
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "[signals] %s: brightness check failed, proceeding anyway",
                self.name, exc_info=True,
            )
            return candidate

        if baseline_bright > 0 and candidate_bright / baseline_bright >= self.brightness_ratio_threshold:
            logger.info(
                "[signals] %s: candidate bottom-strip brightness %.0f is >= %.1fx baseline "
                "%.0f — waiting %ds before re-grabbing",
                self.name, candidate_bright, self.brightness_ratio_threshold,
                baseline_bright, self.brightness_retry_delay,
            )
            await asyncio.sleep(self.brightness_retry_delay)
            try:
                return self.grab(night_date, label="check")
            except Exception:  # noqa: BLE001
                logger.exception("[signals] %s: re-grab after brightness wait failed", self.name)
                return candidate
        return candidate

    # ------------------------------------------------------------------
    # IO — thin enough to monkeypatch in tests
    # ------------------------------------------------------------------
    def grab(self, night_date: date, *, label: str) -> Path:
        now = datetime.now(timezone.utc).astimezone(DUBLIN)
        dest = cameras.frame_dir(self.camera, now) / (
            f"{self.camera}-{night_date.isoformat()}-{label}-{now:%H%M%S}.jpg"
        )
        return cameras.grab_frame(self.camera, dest)

    def build_compare_images(self, baseline: Path, candidate: Path) -> tuple[list[Path], str]:
        """Prepare the images sent to vision, and describe their order in
        the prompt. With an ROI configured: downscaled-full-frame + full-
        resolution bottom crop, for BOTH baseline and candidate (four images)
        — sending the full frame alone loses the one detail (an item at the
        very bottom edge, partly cut off) that a near-field delivery shows.
        Without an ROI: just the two downscaled full frames.
        """
        workdir = candidate.parent
        if self.roi_bottom_fraction:
            b_ctx = imaging.downscale(baseline, workdir / f"{baseline.stem}-ctx.jpg")
            b_roi = imaging.crop_bottom(
                baseline, workdir / f"{baseline.stem}-roi.jpg", fraction=self.roi_bottom_fraction
            )
            c_ctx = imaging.downscale(candidate, workdir / f"{candidate.stem}-ctx.jpg")
            c_roi = imaging.crop_bottom(
                candidate, workdir / f"{candidate.stem}-roi.jpg", fraction=self.roi_bottom_fraction
            )
            order = (
                "1) earlier this evening, full frame; "
                "2) earlier this evening, a close-up of the bottom edge of the frame "
                "(the doorstep, directly beneath the camera); "
                "3) now, full frame; "
                "4) now, the same close-up of the bottom edge"
            )
            return [b_ctx, b_roi, c_ctx, c_roi], order

        b_ctx = imaging.downscale(baseline, workdir / f"{baseline.stem}-ctx.jpg")
        c_ctx = imaging.downscale(candidate, workdir / f"{candidate.stem}-ctx.jpg")
        return [b_ctx, c_ctx], "1) earlier this evening; 2) now"

    def ask_vision(self, images: list[Path], order: str) -> dict:
        from app.plugin.capabilities import get_capability

        vision = get_capability("vision.image")
        return vision.compare(images, self.question, order=order)

    def _send_notification(self, title: str, message: str, *, data: dict | None = None) -> None:
        from app.plugin.capabilities import get_capability

        notify = get_capability("notify.push")
        # user_id=None fans out to every configured household_targets entry
        # — "both household users" per the brief, resolved the same way
        # every other household-wide push in this codebase already is.
        notify.send(title, message, severity="warning", source="signals", data=data)
