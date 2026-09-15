"""Gemini vision call — bytes in, description out.

Deliberately dependency-free on the image side. Gemini accepts PNG, JPEG, WEBP,
**HEIC and HEIF** natively, so iPhone photos need no decoding, and because its
token cost is driven by aspect ratio rather than pixel count there is nothing to
gain by downscaling first. Both facts remove a Pillow/`pillow-heif` dependency
this integration would otherwise have needed — it reads the file and sends it.
"""

from __future__ import annotations

import logging
import mimetypes
import time
from pathlib import Path

from app.errors import PermanentError, TransientError

logger = logging.getLogger(__name__)

# Gemini's documented image MIME types. Anything else is rejected up front rather
# than sent and refused — `.gif` in particular sniffs as an image in `inbox`'s
# magic-byte table but is not accepted here.
SUPPORTED_MIME = {
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/webp": {".webp"},
    "image/heic": {".heic"},
    "image/heif": {".heif"},
}
_EXT_TO_MIME = {ext: mime for mime, exts in SUPPORTED_MIME.items() for ext in exts}

# One prompt, doing three jobs at once: transcribe any text, describe the image,
# and hint at where it should be routed. A pure-OCR tool would return a wall of
# text that still needed a second pass to become the one-line summary the inbox
# actually wants — see `scan.summarise()`.
#
# Every rule below earns its place from a measured failure on real inbox images
# (10 models x 8 images, 2026-08-05), not from imagination:
#
#   - Struck-through text: 7 of 10 models silently dropped the crossed-out items
#     on a handwritten list. They weren't disobeying — the old prompt never asked.
#     The single largest quality difference in the run came from an unasked
#     question, so this is now stated with its reason attached.
#   - KIND: half the models called a photo of a document `photo` and half
#     `document`, both defensible, because the old taxonomy had no tie-break. The
#     rule now turns on the *purpose* of the shot rather than its medium.
#   - Orientation: three of eight images carry labels printed rotated or upside
#     down (a wine bottle's sulphites line runs opposite to its own label).
#   - `[illegible]` vs NONE: for an archive, "I could not read it" and "there was
#     nothing there" are different facts, and conflating them turns a known gap
#     into a false negative.
PROMPT = """Describe this image for someone triaging an inbox of paperwork, \
screenshots and photographs.

Return plain text in exactly these three fields, in this order, and nothing else.

SUMMARY: One sentence, under 30 words, saying what this is. Lead with the kind of \
thing it is, then the single most identifying detail actually present — a date, a \
vendor, a title, a place. Describe any people factually and briefly (approximate \
age group, clothing, what they are doing); never attempt to name or identify them.

KIND: exactly one of:
  document    paperwork whose content is the point — letters, bills, forms, \
statements, certificates, notes, lists, tables
  receipt     a receipt, invoice or till slip
  label       packaging or product labelling — bottles, bags, boxes, jars
  screenshot  a capture of a screen or an app
  photo       a scene, place, person, object or building
  other       none of the above
Choose by what the image is *for*: when legible text is evidently the point of \
the shot, use the category for that content rather than `photo`; when text is \
incidental to a scene, use `photo`.

TEXT: Every word of text visible in the image, transcribed verbatim, in natural \
reading order.
  - Include text at any orientation. Where part of a label or page is rotated or \
upside down, read it in its own orientation and transcribe it in place.
  - Preserve line breaks. For a table, put each row on one line with " | " \
between cells.
  - Copy names, dates, reference numbers, quantities and amounts exactly as \
written. Do not normalise, correct, expand or convert them.
  - Mark deleted text you can still read as [struck through: the words], and \
deleted text you cannot read as [struck through]. Never drop it silently — a \
crossed-out line records a decision, and losing it loses the decision.
  - Mark anything you cannot read as [illegible]. Do not guess, and do not \
complete text that is cut off at the edge of the frame.
  - Write NONE only if the image contains no text at all. Text that is present \
but unreadable is [illegible], which is not the same thing."""


def mime_for(path: Path) -> str | None:
    """Gemini MIME type for `path`, or None if it isn't a format Gemini takes.

    ⚠️ **Content is checked, not just the filename, and that is load-bearing.**
    Every image reaching this integration comes from the inbox, and the inbox's
    main producer posts a bare UUID with **no extension at all** — so both
    filename routes below return None for the normal case. The result was not a
    visible error: `describe_pending` records `described_at` even on failure (by
    design — retrying costs money), so each phone-captured image was rejected as
    "not a Gemini-supported image format" and then permanently marked done. Vision
    had a 100% failure rate in production and nothing said so.

    This is the third bug in this codebase with one root cause. `sniff_kind`'s
    ISO-BMFF check and its HTML detection were both filename-blind for the same
    reason. If you infer a type from a name here, it will be wrong.

    Sniffing is hand-rolled to keep this module's no-Pillow property (see the
    module docstring) — five prefixes against stable formats, the same trade
    `inbox/scan.py::_MAGIC` makes.
    """
    ext = path.suffix.lower()
    if ext in _EXT_TO_MIME:
        return _EXT_TO_MIME[ext]

    guessed, _ = mimetypes.guess_type(path.name)
    if guessed in SUPPORTED_MIME:
        return guessed

    return _sniff_mime(path)


# ISO-BMFF brands (bytes 8:12) that mean a still image rather than video. HEIC
# from an iPhone is `heic`; `mif1`/`msf1` are the generic HEIF brands.
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim", b"heis"}


def _sniff_mime(path: Path) -> str | None:
    """Gemini MIME type from the file's own leading bytes, or None."""
    try:
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return None

    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    # WebP is a RIFF container; the fourcc at 8:12 is what distinguishes it from
    # a WAV, which the inbox's coarser table lumps in with audio.
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS:
        # Gemini accepts both and treats them alike; `image/heic` is the brand an
        # iPhone capture actually is, and the generic brands are HEIF.
        return "image/heic" if head[8:12].startswith(b"he") else "image/heif"
    return None


def describe(
    path: Path,
    *,
    api_key: str,
    model: str,
    max_image_mb: int = 18,
) -> str:
    """Send one image to Gemini and return its raw text response.

    Raises `PermanentError` for anything retrying cannot fix (unsupported
    format, oversized file, malformed request) and `TransientError` for
    everything else, so the scheduler's classification does the right thing and
    a billable call isn't retried against a file that will never work.
    """
    mime = mime_for(path)
    if mime is None:
        raise PermanentError(
            f"{path.name}: not a Gemini-supported image format "
            f"(accepts {', '.join(sorted(SUPPORTED_MIME))})"
        )

    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > max_image_mb:
        raise PermanentError(
            f"{path.name}: {size_mb:.1f}MB exceeds max_image_mb={max_image_mb}; "
            "inline bytes share a 20MB ceiling with the prompt"
        )

    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # noqa: BLE001
        raise PermanentError(
            "google-genai is not installed — vision cannot run"
        ) from exc

    client = genai.Client(api_key=api_key)
    data = path.read_bytes()

    # No `thinking_config` here, deliberately: Gemini 3.x thinks by default and
    # `ThinkingConfig(thinking_budget=0)` is rejected with a 400 on
    # gemini-3.6-flash (measured 2026-08-05) — thinking cannot be turned off on
    # this model. That matters for cost, because thinking tokens bill at the
    # output rate: a real photo measured 1,206 prompt + 1,243 thinking + 327
    # output, so thinking is ~4x the visible answer and ~75% of the bill. If
    # that ever needs cutting, the lever is a cheaper model (Flash-Lite bills
    # output at $2.50/M vs Flash's $7.50/M), not a thinking budget.
    started = time.time()
    try:
        resp = client.models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=data, mime_type=mime),
                types.Part(text=PROMPT),
            ],
        )
    except Exception as exc:  # noqa: BLE001
        _record_usage(model, started, ok=False, error=str(exc)[:300])
        # A 4xx that isn't auth is our request's fault and will fail identically
        # on retry; everything else (429, 5xx, network) is worth another sweep.
        status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if isinstance(status, int) and 400 <= status < 500 and status not in (401, 403, 429):
            raise PermanentError(f"gemini rejected {path.name}: {str(exc)[:300]}") from exc
        raise TransientError(f"gemini call failed for {path.name}: {str(exc)[:300]}") from exc

    _record_usage(model, started, ok=True, usage_metadata=getattr(resp, "usage_metadata", None))

    text = (getattr(resp, "text", None) or "").strip()
    if not text:
        # An empty body is not an error — a safety block or an unreadable image
        # both land here, and the caller records it so the file isn't re-sent on
        # every sweep forever.
        logger.info("[vision] %s returned no text", path.name)
    return text


COMPARE_PROMPT_PREFIX = """You will be shown a sequence of images from a fixed \
security camera, in the order described below. Answer the question at the end.

Return strict JSON only, exactly these keys and nothing else:
{{"answer": true or false, "confidence": a number from 0 to 1, \
"where": a short phrase locating what you saw or null, "notes": a short \
sentence explaining your answer}}

Image order: {order}

Question: {question}"""


def compare(
    images: list[Path],
    question: str,
    *,
    api_key: str,
    model: str,
    order: str = "in the order given",
    max_image_mb: int = 18,
) -> dict:
    """Send several images to Gemini and ask a yes/no-with-confidence
    question about them. Returns a dict already validated to have the four
    keys `answer`/`confidence`/`where`/`notes` (parsing/coercion happens
    here so every caller gets the same shape regardless of how tidily the
    model actually replied — see `_coerce_compare_json`).

    Raises `PermanentError`/`TransientError` with the same classification
    rules as `describe()`.
    """
    if not images:
        raise PermanentError("compare() called with no images")

    parts = []
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # noqa: BLE001
        raise PermanentError("google-genai is not installed — vision cannot run") from exc

    for path in images:
        mime = mime_for(path)
        if mime is None:
            raise PermanentError(
                f"{path.name}: not a Gemini-supported image format "
                f"(accepts {', '.join(sorted(SUPPORTED_MIME))})"
            )
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > max_image_mb:
            raise PermanentError(
                f"{path.name}: {size_mb:.1f}MB exceeds max_image_mb={max_image_mb}"
            )
        parts.append(types.Part.from_bytes(data=path.read_bytes(), mime_type=mime))

    prompt = COMPARE_PROMPT_PREFIX.format(order=order, question=question)
    parts.append(types.Part(text=prompt))

    client = genai.Client(api_key=api_key)
    started = time.time()
    try:
        resp = client.models.generate_content(model=model, contents=parts)
    except Exception as exc:  # noqa: BLE001
        _record_usage(model, started, ok=False, error=str(exc)[:300], role="vision.watch")
        status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if isinstance(status, int) and 400 <= status < 500 and status not in (401, 403, 429):
            raise PermanentError(f"gemini rejected compare(): {str(exc)[:300]}") from exc
        raise TransientError(f"gemini compare() call failed: {str(exc)[:300]}") from exc

    _record_usage(
        model, started, ok=True, usage_metadata=getattr(resp, "usage_metadata", None),
        role="vision.watch",
    )
    text = (getattr(resp, "text", None) or "").strip()
    return _coerce_compare_json(text)


def _coerce_compare_json(raw: str) -> dict:
    """Defensively parse Gemini's reply into `{answer, confidence, where, notes}`.

    Models routinely wrap JSON in a ```json fence, or add a stray sentence
    before/after it — this strips a fence if present and falls back to a
    conservative "no" (with the raw text in `notes`) if nothing parseable is
    found, rather than raising and losing the whole check.
    """
    import json
    import re

    body = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", body, re.S)
    if fence:
        body = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", body, re.S)
        if brace:
            body = brace.group(0)

    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        parsed = {}

    if not isinstance(parsed, dict):
        parsed = {}

    answer = parsed.get("answer")
    if not isinstance(answer, bool):
        answer = str(answer).strip().lower() in ("true", "yes", "1") if answer is not None else False

    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    where = parsed.get("where")
    where = where if isinstance(where, str) and where.strip() else None

    notes = parsed.get("notes")
    if not isinstance(notes, str) or not notes.strip():
        notes = raw.strip()[:500] if not parsed else ""

    return {"answer": answer, "confidence": confidence, "where": where, "notes": notes}


def _record_usage(
    model: str,
    started: float,
    *,
    ok: bool,
    usage_metadata=None,
    error: str | None = None,
    role: str = "vision.inbox",
) -> None:
    """Best-effort ai_usage row for one Gemini vision call.

    Delegates to `app.services.ai_ledger.record_genai_usage()` — the shared
    shape for the two call sites (this one and
    `transcription/gemini.py::transcribe()`) that talk to `google.genai`
    directly rather than through `coglib.llm` (vision stays dependency-light
    on purpose — see the module docstring). Never raises.

    `role` defaults to `"vision.inbox"` (this module's original, single
    caller) and `compare()` passes `"vision.watch"` explicitly — see
    `app/services/ai_roles.py`, which is what resolved `model` in the first
    place for either call site.
    """
    from app.services import ai_ledger

    ai_ledger.record_genai_usage(
        model=model,
        kind="vision",
        caller="integration:vision",
        role=role,
        started=started,
        ok=ok,
        usage_metadata=usage_metadata,
        error=error,
    )
