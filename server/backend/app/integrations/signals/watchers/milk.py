"""The milk watcher — the first tenant of the watcher framework.

Milk lands on the front doorstep Sunday/Tuesday/Thursday nights, usually
21:00-23:00 but drifting; window is generous (20:30-00:30) either side. The
camera sees the doorstep only at the very bottom edge of its frame, partly
cut off — see `watchers/base.py`'s `roi_bottom_fraction` and
`imaging.crop_bottom` — because the milk is placed directly beneath the
camera (confirmed from a real delivery clip, 2026-09-11: frame 1 of 15 shows
an empty step; frame 15 shows a white protective box at the very bottom
centre of the frame, partly out of shot).

The question below states that geometry explicitly (delivered items are
easy to miss if the model assumes they'd be centred in the frame like a
person would be) and asks about milk/delivery items ONLY, so a passing cat,
a shifted plant pot or headlight glare doesn't read as a false positive.
"""

from __future__ import annotations

from datetime import time

from app.integrations.signals.watchers.base import Watcher

QUESTION = (
    "This is a fixed doorbell camera. The doorstep is at the very BOTTOM EDGE "
    "of the frame, directly beneath the camera — anything left there appears "
    "small, low in the frame, and is often partly cut off by the frame edge, "
    "on either side of centre. Has milk been delivered since the earlier "
    "image — bottles, a milk crate, or a white/pale protective box placed on "
    "the ground near the bottom edge of the frame? Answer only about milk or "
    "delivery items left on the ground; ignore people, vehicles, plants, "
    "bins, lighting changes and shadows."
)

MILK_WATCHER = Watcher(
    name="milk",
    camera="front_door",
    # Monday=0 .. Sunday=6 (date.weekday()). Sunday=6, Tuesday=1, Thursday=3.
    weekdays=frozenset({6, 1, 3}),
    start=time(20, 30),
    end=time(0, 30),
    question=QUESTION,
    confidence_threshold=0.7,
    kinds=frozenset({"person", "unknown"}),
    device_names=frozenset({"front_door"}),
    source="protect",
    settle_seconds=45,
    poll_minutes=15,
    roi_bottom_fraction=0.3,
    brightness_ratio_threshold=1.6,
    brightness_retry_delay=30,
    # Alex's exact wording (2026-09-11) — nothing else appended to the message.
    detected_title="Milk 🥛",
    detected_message="Milk has been delivered — take it in.",
    # OFF for now (Alex reviewing phone-alert volume) — flip on later.
    notify_on_close=False,
    close_title="Milk 🥛",
    close_message="No milk tonight.",
    notify_data={"entity_id": "camera.frankfort_front_door_high_resolution_channel"},
)
