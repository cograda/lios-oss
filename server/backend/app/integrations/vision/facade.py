"""vision's facade — capability `vision.image`.

Two methods, mirroring `transcription.audio`, and for the same reason:

  - `available()` — is vision usable at all? A caller driving a queue needs to
    tell "no key configured, skip quietly" apart from "configured but this file
    failed". Without it an unconfigured deployment logs an error for every
    pending image on every cron tick.
  - `describe()` — do it, and say what came back.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from app.errors import ComarError
from app.integrations.vision.manifest import MANIFEST

# Imported as a module, never `from ... import plugin_config`. Binding the
# function at import time is the documented trap here: a test that patches
# `app.plugin.config_store.plugin_config` would then patch a name this module no
# longer points at, and the test would pass or fail for the wrong reason.
from app.plugin import config_store as _config_store

logger = logging.getLogger(__name__)

_FIELD = re.compile(
    r"^(SUMMARY|KIND|TEXT):\s*(.*?)(?=^(?:SUMMARY|KIND|TEXT):|\Z)",
    re.M | re.S,
)
# Must match the KIND enum in `client.PROMPT`. Kept deliberately small: `letter`
# and `form` were dropped because they route identically to `document` and only
# gave the model more ways to disagree with itself, while `label` was added
# because product packaging is a real inbox category (a coffee bag belongs to the
# coffee log, a bottle to a drinks note) that previously landed in `other`.
_VALID_KINDS = {
    "document", "receipt", "label", "screenshot", "photo", "other",
}


@dataclass(frozen=True)
class VisionResult:
    """Outcome of one image description attempt.

    `summary` empty with `error` set is a failure. `summary` empty with no
    `error` means the model genuinely returned nothing (a safety block, or an
    image too dark to read) — the caller should record that so the file isn't
    re-sent on every sweep.
    """

    summary: str
    kind: str = "other"
    text: str = ""          # transcribed text, "" when the image had none
    model: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.summary)


def parse_response(raw: str) -> tuple[str, str, str]:
    """Pull SUMMARY / KIND / TEXT out of the model's reply.

    Tolerant on purpose: a missing field yields an empty string rather than an
    exception, and an unrecognised KIND falls back to "other". The prompt asks
    for a fixed shape, but a model that adds a stray line or drops a field
    shouldn't cost us the fields it *did* return — and this runs against files a
    human is waiting on, not a schema-validated API.
    """
    fields = {m.group(1): m.group(2).strip() for m in _FIELD.finditer(raw or "")}

    summary = " ".join(fields.get("SUMMARY", "").split())
    kind = fields.get("KIND", "").strip().lower()
    if kind not in _VALID_KINDS:
        kind = "other"

    text = fields.get("TEXT", "").strip()
    if text.upper() == "NONE":
        text = ""

    # No recognisable fields but a non-empty reply: treat the whole thing as the
    # summary rather than discarding a usable description over its formatting.
    if not summary and not text and (raw or "").strip():
        summary = " ".join(raw.split())[:300]

    return summary, kind, text


class VisionFacade:
    def available(self) -> bool:
        """Whether a paid image description can currently be attempted."""
        try:
            return bool((_config_store.plugin_config("vision").gemini_api_key or "").strip())
        except Exception:  # noqa: BLE001
            # Config table unreachable — report unavailable rather than raising
            # into a caller's queue loop.
            logger.exception("[vision] config lookup failed")
            return False

    def describe(self, path: Path) -> VisionResult:
        """Describe `path`. Never raises — failures come back as a result.

        Callers are queue walkers processing many files; one bad image must not
        abort the batch, and the reason has to be recordable against that file.
        """
        from app.integrations.vision import client as _client
        from app.services import ai_roles

        cfg = _config_store.plugin_config("vision")
        api_key = (cfg.gemini_api_key or "").strip()
        if not api_key:
            return VisionResult(summary="", error="no gemini_api_key configured")

        # Model comes from the role registry, not read directly off cfg — this
        # is the one-place-to-look guarantee: `ai_roles.resolve_vision_inbox()`
        # is what reads `cfg.model` (falling back to the manifest default, and
        # raising RoleNotBoundError if that's ever also empty). A test still
        # pins model == the manifest default, because a silent drift there
        # means the configured model and the unconfigured one differ and
        # nothing says so.
        try:
            # `fallback` is vision's own manifest default — ai_roles.py can't
            # import it itself (kernel/integration import boundary; see
            # resolve_vision_inbox()'s docstring), so the integration that
            # owns the manifest supplies it.
            binding = ai_roles.resolve_vision_inbox(
                fallback=MANIFEST.config_schema["model"].default
            )
        except ai_roles.RoleNotBoundError as exc:
            # Loud (an ERROR-level log, not a debug line — an operator should
            # see this), but `describe()`'s own contract is "never raises":
            # one misconfigured role must not abort a queue walker's whole
            # batch. See ai_roles.py's module docstring for why "loud" means
            # "logged clearly" here rather than "raises".
            logger.error("[vision] role 'vision.inbox' is not bound: %s", exc)
            return VisionResult(summary="", error=str(exc)[:300])
        model = binding.model
        try:
            raw = _client.describe(
                path,
                api_key=api_key,
                model=model,
                max_image_mb=cfg.max_image_mb or 18,
            )
        except ComarError as exc:
            logger.warning("[vision] %s failed: %s", path.name, exc)
            return VisionResult(summary="", model=model, error=str(exc)[:300])
        except Exception as exc:  # noqa: BLE001
            logger.exception("[vision] %s raised unexpectedly", path.name)
            return VisionResult(summary="", model=model, error=str(exc)[:300])

        summary, kind, text = parse_response(raw)
        return VisionResult(summary=summary, kind=kind, text=text, model=model)


FACADE = VisionFacade()
