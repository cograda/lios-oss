"""MCP tools for `signals`.

  - signals_recent:  read-only. Last N inlet events, filterable by
                     source/device/kind.
  - watch_history:   read-only. Runs per watcher — status, confidence,
                     frame paths — never RTSP URLs.
  - watch_confirm:   write. Grade a run's verdict (was it actually right?).
  - watch_test:      diagnostic. Run the vision check right now — either
                     against a fresh grab, or (offline mode) against two
                     server-local image files — without waiting for a
                     scheduled window. This is how the milk question gets
                     dry-run before the first real Sunday.

Household-shared, like `tasks`/`snags`: runs and events belong to the house,
not to whichever user is asking. `watch_confirm` records WHO confirmed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.context import current_user_id
from app.integrations.signals.models import SignalEvent, WatchRun
from app.tools import CustomTool, ToolAnnotations
from app.tools.helpers import iso_or_none, serialize


# ---------------------------------------------------------------------------
# Output shapes — kept beside the handlers they describe.
# ---------------------------------------------------------------------------

class SignalEventOut(BaseModel):
    id: int
    source: str
    kind: str
    device_key: str | None
    device_name: str | None
    occurred_at: str | None
    sender_event_id: str | None
    received_at: str | None
    # The provider's own one-click link to view the clip
    # (`alarm.eventLocalLink`, falling back to `alarm.eventPath`), when the
    # stored payload carries one — Protect's Alarm Manager webhook, not
    # every source, sends this. `None` when absent, same as every other
    # payload-shape-dependent field here.
    event_link: str | None


class WatchRunOut(BaseModel):
    id: int
    watcher: str
    night_date: str
    opened_at: str | None
    status: str
    detected_at: str | None
    frame_path: str | None
    confidence: float | None
    model: str | None
    checks: int
    confirmed: bool | None
    confirmed_by_user_id: int | None
    confirmed_at: str | None
    note: str | None


def _event_link(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    alarm = payload.get("alarm")
    if not isinstance(alarm, dict):
        return None
    link = alarm.get("eventLocalLink") or alarm.get("eventPath")
    return link if isinstance(link, str) and link else None


def _event_row(event: SignalEvent) -> dict:
    out = serialize(event, ["id", "source", "kind", "device_key", "device_name", "sender_event_id"])
    out["occurred_at"] = iso_or_none(event.occurred_at)
    out["received_at"] = iso_or_none(event.received_at)
    out["event_link"] = _event_link(event.payload)
    return SignalEventOut(**out).model_dump()


def _run_row(run: WatchRun) -> dict:
    out = serialize(
        run,
        ["id", "watcher", "night_date", "status", "frame_path", "confidence", "model",
         "checks", "confirmed", "confirmed_by_user_id", "note"],
    )
    out["opened_at"] = iso_or_none(run.opened_at)
    out["detected_at"] = iso_or_none(run.detected_at)
    out["confirmed_at"] = iso_or_none(run.confirmed_at)
    return WatchRunOut(**out).model_dump()


# ---------------------------------------------------------------------------
# signals_recent
# ---------------------------------------------------------------------------

def signals_recent_handler(session: Session, args: dict[str, Any]) -> str:
    import json

    limit = min(int(args.get("limit") or 20), 200)
    query = session.query(SignalEvent)
    if args.get("source"):
        query = query.filter(SignalEvent.source == args["source"])
    if args.get("device_key"):
        # Normalised the same way stored rows are (`normalize_device_key`):
        # a caller who pastes a colon-form MAC from HA's device registry
        # still matches the colonless form Protect actually sent.
        from app.integrations.signals.protect import normalize_device_key

        query = query.filter(SignalEvent.device_key == normalize_device_key(args["device_key"]))
    if args.get("kind"):
        query = query.filter(SignalEvent.kind == args["kind"])
    events = query.order_by(SignalEvent.occurred_at.desc()).limit(limit).all()
    return json.dumps({"events": [_event_row(e) for e in events]})


# ---------------------------------------------------------------------------
# watch_history
# ---------------------------------------------------------------------------

def watch_history_handler(session: Session, args: dict[str, Any]) -> str:
    import json

    limit = min(int(args.get("limit") or 20), 200)
    query = session.query(WatchRun)
    if args.get("watcher"):
        query = query.filter(WatchRun.watcher == args["watcher"])
    if args.get("status"):
        query = query.filter(WatchRun.status == args["status"])
    runs = query.order_by(WatchRun.opened_at.desc()).limit(limit).all()
    return json.dumps({"runs": [_run_row(r) for r in runs]})


# ---------------------------------------------------------------------------
# watch_confirm
# ---------------------------------------------------------------------------

def watch_confirm_handler(session: Session, args: dict[str, Any]) -> str:
    import json

    run_id = args.get("run_id")
    if run_id is None:
        return json.dumps({"error": "run_id is required"})
    run = session.get(WatchRun, int(run_id))
    if run is None:
        return json.dumps({"error": f"no watch run {run_id}"})

    run.confirmed = bool(args.get("correct"))
    run.confirmed_by_user_id = current_user_id()
    run.confirmed_at = datetime.now(timezone.utc)
    if args.get("note"):
        run.note = str(args["note"])[:2000]
    session.commit()
    return json.dumps({"ok": True, "run": _run_row(run)})


# ---------------------------------------------------------------------------
# watch_test
# ---------------------------------------------------------------------------

def watch_test_handler(session: Session, args: dict[str, Any]) -> str:
    """Dry-run a vision check right now, without a scheduled window.

    Two modes:
      - Live: `camera` (+ optional `question`) grabs a fresh frame now and
        compares it against itself (no earlier baseline exists outside a
        real window) — good for checking the camera/ffmpeg path works.
      - Offline (added 2026-09-11, so the milk question can be dry-run
        before the first real Sunday): `baseline_path` + `candidate_path`
        name two server-local image files instead of grabbing. Both must
        exist on disk already (this tool never accepts arbitrary uploads).
    """
    import json

    from app.integrations.signals import imaging
    from app.plugin.capabilities import get_capability

    question = args.get("question")
    if not question:
        from app.integrations.signals.watchers.milk import QUESTION as question  # noqa: N813
    roi = args.get("roi_bottom_fraction")
    roi = float(roi) if roi is not None else 0.3

    baseline_path = args.get("baseline_path")
    candidate_path = args.get("candidate_path")

    if baseline_path or candidate_path:
        if not (baseline_path and candidate_path):
            return json.dumps({"error": "offline mode needs both baseline_path and candidate_path"})
        baseline = Path(baseline_path)
        candidate = Path(candidate_path)
        if not baseline.exists() or not candidate.exists():
            return json.dumps({"error": "baseline_path or candidate_path does not exist on the server"})
    else:
        camera = args.get("camera")
        if not camera:
            return json.dumps({"error": "camera is required in live mode (or pass baseline_path/candidate_path)"})
        from app.integrations.signals import cameras

        now = datetime.now(timezone.utc)
        try:
            baseline = cameras.grab_frame(camera, cameras.frame_path(camera, now))
            candidate = baseline
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": f"grab failed: {exc}"})

    # A dedicated temp dir, never the source files' own directory — in
    # offline mode that would write derived crops straight into whatever
    # directory `baseline_path`/`candidate_path` live in (e.g. the test
    # fixtures directory), which is not this tool's to litter.
    import tempfile

    workdir = Path(tempfile.mkdtemp(prefix="watch_test-"))
    try:
        if roi:
            images = [
                imaging.downscale(baseline, workdir / f"{baseline.stem}-test-b-ctx.jpg"),
                imaging.crop_bottom(baseline, workdir / f"{baseline.stem}-test-b-roi.jpg", fraction=roi),
                imaging.downscale(candidate, workdir / f"{candidate.stem}-test-c-ctx.jpg"),
                imaging.crop_bottom(candidate, workdir / f"{candidate.stem}-test-c-roi.jpg", fraction=roi),
            ]
            order = (
                "1) baseline full frame; 2) baseline bottom-edge close-up; "
                "3) candidate full frame; 4) candidate bottom-edge close-up"
            )
        else:
            images = [
                imaging.downscale(baseline, workdir / f"{baseline.stem}-test-b-ctx.jpg"),
                imaging.downscale(candidate, workdir / f"{candidate.stem}-test-c-ctx.jpg"),
            ]
            order = "1) baseline; 2) candidate"
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"image prep failed: {exc}"})

    vision = get_capability("vision.image")
    result = vision.compare(images, question, order=order)
    return json.dumps({
        "result": result,
        "baseline_path": str(baseline),
        "candidate_path": str(candidate),
    })


# ---------------------------------------------------------------------------

def mcp_tools() -> list[dict[str, Any]]:
    return [
        CustomTool(
            name="signals_recent",
            description="Recent accepted inlet events (camera/sensor hits), filterable by source/device/kind.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 20},
                    "source": {"type": "string"},
                    "device_key": {"type": "string"},
                    "kind": {"type": "string", "enum": ["person", "vehicle", "package", "ring", "motion", "unknown"]},
                },
            },
            handler=signals_recent_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="watch_history",
            description="Watcher runs (one per watcher per night) — status, confidence, frame paths. Never returns RTSP URLs.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 20},
                    "watcher": {"type": "string"},
                    "status": {"type": "string", "enum": ["watching", "detected", "none", "error"]},
                },
            },
            handler=watch_history_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="watch_confirm",
            description="Grade a watch run's verdict — was the detection (or non-detection) actually correct?",
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "integer"},
                    "correct": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": ["run_id", "correct"],
            },
            handler=watch_confirm_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="watch_test",
            description=(
                "Dry-run a watcher's vision question right now. Live mode grabs a "
                "fresh frame from `camera`; offline mode compares two server-local "
                "files via `baseline_path`/`candidate_path` (e.g. saved fixture "
                "frames) without touching a camera — for testing a question before "
                "a real window ever opens."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "camera": {"type": "string"},
                    "baseline_path": {"type": "string"},
                    "candidate_path": {"type": "string"},
                    "question": {"type": "string"},
                    "roi_bottom_fraction": {"type": "number"},
                },
            },
            handler=watch_test_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False, open_world_hint=True),
        ).build(),
    ]
