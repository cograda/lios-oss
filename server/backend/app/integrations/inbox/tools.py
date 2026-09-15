"""MCP tools for inbox triage.

All tools operate on /inbox/ on the server. They never touch the Tines
webhook write path; that's a one-way drop. State transitions here are
limited to *moving* files between pending and the two terminal directories
(archive, dismissed), or *copying* them into the vault / corpus and then
archiving the source.

Tool surface kept deliberately small — composability with vault/corpus
tools handles the rest.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.auth.context import current_user_id
from app.integrations.inbox import scan
from app.tools import CustomTool, ToolAnnotations

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def handle_pending(session: Session, arguments: dict[str, Any]) -> str:
    """List pending inbox items, newest enrichment fields included.

    On-demand: if any pending file hasn't been enriched yet (e.g. the
    hourly worker hasn't run since it landed), enrich it inline so the
    caller never sees a half-empty record. Skill flows happen at human
    timescales, so a sub-second enrich is well worth the freshness.

    Scoped to the calling user (F6) — `current_user_id()` is bound by the
    dispatch layer for the duration of this call.
    """
    user_id = current_user_id()
    limit = int(arguments.get("limit", 20))
    # Inline enrich anything missing — best-effort, swallow per-file errors.
    pending_files = scan.iter_pending_files(user_id)
    for p in pending_files:
        try:
            scan.enrich_one(p)
        except Exception:  # noqa: BLE001
            logger.exception(f"[inbox_pending] inline enrich failed: {p}")
    items = scan.list_pending(user_id, limit=limit)
    # A page is a page: say how big the backlog is and whether anything was
    # left out, so a caller can never mistake `count` for "everything pending"
    # (lios#184 — 20 of 25 was indistinguishable from 20 of 20).
    total = len(pending_files)
    return json.dumps({
        "count": len(items),
        "total_pending": total,
        "truncated": total > len(items),
        "items": items,
    }, indent=2, default=str)


def handle_preview(session: Session, arguments: dict[str, Any]) -> str:
    """Return the full extracted text of one inbox item (not just the
    capped preview from the sidecar). PDFs return all pages joined; text
    files return the whole file; images return the verbatim transcription
    the vision sweep already wrote to the sidecar. Cap at 50k chars —
    anything bigger is almost certainly not what you want to drop into a
    chat context."""
    user_id = current_user_id()
    path_arg = arguments.get("path", "")
    if not path_arg:
        return json.dumps({"error": "missing 'path'"})
    try:
        path = scan.safe_resolve(path_arg, user_id)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    if not path.exists():
        return json.dumps({"error": f"not found: {path_arg}"})

    kind = scan.sniff_kind(path)
    MAX = 50_000
    text = ""
    if kind == "pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            pages = []
            for i, page in enumerate(reader.pages):
                try:
                    pages.append(page.extract_text() or "")
                except Exception as e:  # noqa: BLE001
                    pages.append(f"(page {i+1} extract failed: {e})")
            text = "\n\n".join(pages)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": f"pdf read failed: {e}"})
    elif kind in ("text", "markdown"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return json.dumps({"error": f"read failed: {e}"})
    elif kind == "image":
        # An image has no text of its own to extract — the text is whatever the
        # vision sweep already transcribed into the sidecar. Reading it from
        # there is free and, crucially, never re-bills a Gemini call while a
        # retry is still due (see `describe_pending`/issue #147) — a failed
        # attempt only stamps `described_at` (which gates re-billing) after
        # `MAX_VISION_ATTEMPTS` gives up, so re-describing here would defeat
        # that budget.
        #
        # Before this branch existed there was no route from an inbox image to
        # its transcribed text at all: the listing returned only the one-line
        # summary, and preview refused the file outright. A caller that needed
        # the label had to copy the image into the vault just to get somewhere
        # it could be read.
        meta = scan.read_sidecar(path)
        image_text = meta.get("image_text") or ""
        payload = {
            "path": str(path),
            "kind": kind,
            "text": image_text[:MAX],
            "truncated": len(image_text) > MAX,
            "full_length": len(image_text),
            "image_kind": meta.get("image_kind"),
            "note": meta.get("note"),
            # Not a bare `described_at` check — a legacy pre-#157 failure
            # (issue #160) also has it set but is due another retry.
            "described": scan.vision_described(meta),
            "vision_error": meta.get("vision_error"),
            "vision_attempts": meta.get("vision_attempts", 0),
            "vision_failed": bool(meta.get("vision_failed")),
        }
        if not image_text:
            # Four situations now, and a caller has to be able to tell them
            # apart — "not looked at yet" and "failed but still retrying" are
            # both retryable (the difference is cosmetic), "gave up for good"
            # is reportable, and "no text in the image" is a finished answer.
            if meta.get("vision_failed"):
                payload["hint"] = (
                    "vision failed permanently after repeated attempts; "
                    "see vision_error"
                )
            elif meta.get("vision_attempts"):
                payload["hint"] = (
                    "vision has failed and is retrying on a later sweep; "
                    "see vision_error"
                )
            elif not meta.get("described_at"):
                payload["hint"] = (
                    "image has not been through the vision sweep yet — it runs "
                    "hourly, or inbox_pending enriches inline on read"
                )
            elif meta.get("vision_error"):
                payload["hint"] = "vision failed for this image; see vision_error"
            else:
                payload["hint"] = (
                    "vision read this image and found no text in it (or was "
                    "safety-blocked); see note for the description"
                )
        return json.dumps(payload, default=str)
    else:
        return json.dumps({
            "error": f"preview not supported for kind={kind!r}",
            "hint": "use inbox_to_vault or inbox_to_corpus to route this file instead",
        })

    truncated = len(text) > MAX
    return json.dumps({
        "path": str(path),
        "kind": kind,
        "text": text[:MAX],
        "truncated": truncated,
        "full_length": len(text),
    }, default=str)


def handle_archive(session: Session, arguments: dict[str, Any]) -> str:
    """Move an inbox item into /inbox/archive/ (item has been handled,
    keep for traceability)."""
    return _move_handler(arguments, terminal="archive")


def handle_dismiss(session: Session, arguments: dict[str, Any]) -> str:
    """Move an inbox item into /inbox/dismissed/ (explicit "ignore this")."""
    return _move_handler(arguments, terminal="dismissed")


def _move_handler(arguments: dict[str, Any], *, terminal: str) -> str:
    user_id = current_user_id()
    path_arg = arguments.get("path", "")
    if not path_arg:
        return json.dumps({"error": "missing 'path'"})
    try:
        src = scan.safe_resolve(path_arg, user_id)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    if not src.exists():
        return json.dumps({"error": f"not found: {path_arg}"})
    dest = scan.move_to(src, terminal, user_id)
    return json.dumps({"ok": True, "moved_to": str(dest), "terminal": terminal})


def handle_to_vault(session: Session, arguments: dict[str, Any]) -> str:
    """Copy an inbox item into the vault at a caller-specified logical path,
    then archive the original.

    `target` is a vault-logical path like `Inbox/2026-05-22-tines-pdf.pdf` or
    `Household/wifi.pdf`. Path resolution + traversal protection are
    handled by `app.services.vault_paths.resolve`."""
    from app.services import vault_paths

    user_id = current_user_id()
    path_arg = arguments.get("path", "")
    target_arg = arguments.get("target", "")
    if not path_arg or not target_arg:
        return json.dumps({"error": "missing 'path' and/or 'target'"})
    try:
        src = scan.safe_resolve(path_arg, user_id)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    if not src.exists():
        return json.dumps({"error": f"not found: {path_arg}"})

    try:
        dst = vault_paths.resolve(target_arg)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"target resolve failed: {e}"})

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dst))

    archived = scan.move_to(src, "archive", user_id)

    return json.dumps({
        "ok": True,
        "copied_to": str(dst),
        "archived_from": path_arg,
        "archived_to": str(archived),
        "note": (
            "File is in the vault. Markdown files will be indexed on the "
            "next obsidian sync (≤30 min). PDFs/images are not vault-indexed."
        ),
    }, default=str)


def handle_to_corpus(session: Session, arguments: dict[str, Any]) -> str:
    """Ingest an inbox item into the historical_corpus (PDFs, DOCX, XLSX),
    then archive. Reuses the corpus's own `ingest_path`, so it goes through
    the same parser dispatch, content-hash dedupe, and embedding queue."""
    from app.plugin.capabilities import get_capability

    corpus = get_capability("corpus.ingest")

    user_id = current_user_id()
    path_arg = arguments.get("path", "")
    if not path_arg:
        return json.dumps({"error": "missing 'path'"})
    raw_tags = arguments.get("project_tags") or ["tines"]
    if isinstance(raw_tags, str):
        raw_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

    try:
        src = scan.safe_resolve(path_arg, user_id)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    if not src.exists():
        return json.dumps({"error": f"not found: {path_arg}"})

    # `ingest_path`'s own dispatcher now sniffs content for pdf/docx/xlsx
    # (issue #140), so a mismatched or missing extension no longer breaks
    # routing by itself — but the filename that ends up in the vault archive
    # and the corpus `source_path` should still say what the file actually
    # is, not what the caller happened to name it. Content sniffing is the
    # one signal trusted here: nothing arriving via the inbox webhook has a
    # reliable filename (Tines posts a bare UUID with no extension at all;
    # a captured document can just as easily carry the wrong one, e.g. a
    # `.doc` that content sniffing shows is really OOXML docx).
    kind = scan.sniff_kind(src)
    ext = {"pdf": ".pdf", "docx": ".docx", "xlsx": ".xlsx"}.get(kind)
    if not src.suffix:
        if not ext:
            return json.dumps({
                "error": f"corpus does not handle kind={kind!r}; use inbox_to_vault instead",
            })
        renamed = src.with_name(src.name + ext)
    elif ext and src.suffix.lower() != ext:
        renamed = src.with_name(src.stem + ext)
    else:
        renamed = None
    if renamed is not None:
        src.rename(renamed)
        # Move the sidecar to match.
        old_sc = scan.sidecar_path(src)
        new_sc = scan.sidecar_path(renamed)
        if old_sc.exists():
            old_sc.rename(new_sc)
        src = renamed

    try:
        result = corpus.ingest_path(session, src, project_tags=list(raw_tags))
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[inbox_to_corpus] ingest failed: {src}")
        return json.dumps({"error": f"ingest failed: {e}"})

    archived = scan.move_to(src, "archive", user_id)
    return json.dumps({
        "ok": True,
        "ingest": result,
        "project_tags": list(raw_tags),
        "archived_to": str(archived),
    }, default=str)


# ---------------------------------------------------------------------------
# MCP tool registry
# ---------------------------------------------------------------------------


def get_mcp_tools() -> list[dict[str, Any]]:
    return [
        CustomTool(
            name="inbox_pending",
            description=(
                "List items waiting in the inbox triage queue (files dropped via "
                "the Tines webhook at /api/inbox/ingest). Each item carries a kind "
                "sniff, original filename, age, and a short preview so you can decide "
                "what to do with it without opening the file. Call this at the start "
                "of daily-note and refresh flows. Returns the `limit` NEWEST items "
                "(oldest-first within the page) plus `total_pending` and `truncated`, "
                "so a page is never mistaken for the whole backlog."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max items to return (default 20).",
                        "default": 20,
                    },
                },
            },
            handler=handle_pending,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
            category="inbox",
            examples=[
                "What's in my inbox?",
                "Anything new dropped from Tines?",
                "Triage the inbox",
            ],
        ).build(),
        CustomTool(
            name="inbox_preview",
            description=(
                "Return the full extracted text of one pending inbox item. PDFs "
                "are parsed page by page, text/markdown returned whole, and for "
                "an image this returns the verbatim text the vision sweep "
                "transcribed off it (labels, letters, screenshots) — which is "
                "far more than the one-line description in inbox_pending's "
                "`note`. Use after `inbox_pending` when the short preview isn't "
                "enough to decide where a file should go, or whenever you need "
                "the actual fields printed on a photographed label or document."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute /inbox/... path of the item, as returned by inbox_pending.",
                    },
                },
                "required": ["path"],
            },
            handler=handle_preview,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
            category="inbox",
            examples=[
                "Show me the full text of the first inbox PDF",
            ],
        ).build(),
        CustomTool(
            name="inbox_to_vault",
            description=(
                "Copy an inbox item into the vault at a chosen logical path (e.g. "
                "`Inbox/2026-05-22-receipt.pdf` or `Household/wifi.pdf`) and "
                "archive the original. Use this for files you want surfaced in "
                "Obsidian or attached to a daily note/project."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute /inbox/... path of the item.",
                    },
                    "target": {
                        "type": "string",
                        "description": (
                            "Vault-logical destination path (e.g. 'Inbox/foo.pdf', "
                            "'Projects/Renovation/site-visit.pdf', "
                            "'Household/wifi.pdf'). Resolved against your vault."
                        ),
                    },
                },
                "required": ["path", "target"],
            },
            handler=handle_to_vault,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
            category="inbox",
            examples=[
                "Drop the PDF into my project folder for Renovation",
                "Save the inbox PDF into Household/wifi.pdf",
            ],
        ).build(),
        CustomTool(
            name="inbox_to_corpus",
            description=(
                "Ingest an inbox item (PDF/DOCX/XLSX) into the historical_documents "
                "corpus with the given project_tags, then archive. After this, the "
                "file is searchable via corpus_search and the unified semantic "
                "search."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute /inbox/... path of the item.",
                    },
                    "project_tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Project tags to attach (default ['tines']).",
                    },
                },
                "required": ["path"],
            },
            handler=handle_to_corpus,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
            category="inbox",
            examples=[
                "Ingest this contract into the corpus tagged renovation",
            ],
        ).build(),
        CustomTool(
            name="inbox_archive",
            description=(
                "Move an inbox item to /inbox/archive/ — i.e. mark it handled "
                "without copying it anywhere. Use for items you've already "
                "actioned by hand."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
            handler=handle_archive,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
            category="inbox",
            examples=["Archive that inbox item, I've handled it"],
        ).build(),
        CustomTool(
            name="inbox_dismiss",
            description=(
                "Move an inbox item to /inbox/dismissed/ — explicitly ignored. "
                "Kept on disk so we can audit accidental dismissals."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
            handler=handle_dismiss,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
            category="inbox",
            examples=["Dismiss that inbox item, not relevant"],
        ).build(),
    ]
