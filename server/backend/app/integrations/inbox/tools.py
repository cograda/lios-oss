"""MCP tools for inbox triage.

All tools operate on /inbox/ on the server. They never touch the
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

from app.integrations.inbox import scan

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
    """
    limit = int(arguments.get("limit", 20))
    # Inline enrich anything missing — best-effort, swallow per-file errors.
    for p in scan.iter_pending_files():
        try:
            scan.enrich_one(p)
        except Exception:  # noqa: BLE001
            logger.exception(f"[inbox_pending] inline enrich failed: {p}")
    items = scan.list_pending(limit=limit)
    return json.dumps({
        "count": len(items),
        "items": items,
    }, indent=2, default=str)


def handle_preview(session: Session, arguments: dict[str, Any]) -> str:
    """Return the full extracted text of one inbox item (not just the
    capped preview from the sidecar). PDFs return all pages joined; text
    files return the whole file. Cap at 50k chars — anything bigger is
    almost certainly not what you want to drop into a chat context."""
    path_arg = arguments.get("path", "")
    if not path_arg:
        return json.dumps({"error": "missing 'path'"})
    try:
        path = scan.safe_resolve(path_arg)
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
    path_arg = arguments.get("path", "")
    if not path_arg:
        return json.dumps({"error": "missing 'path'"})
    try:
        src = scan.safe_resolve(path_arg)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    if not src.exists():
        return json.dumps({"error": f"not found: {path_arg}"})
    dest = scan.move_to(src, terminal)
    return json.dumps({"ok": True, "moved_to": str(dest), "terminal": terminal})


def handle_to_vault(session: Session, arguments: dict[str, Any]) -> str:
    """Copy an inbox item into the vault at a caller-specified logical path,
    then archive the original.

    `target` is a vault-logical path like `Inbox/2026-05-22-webhook-pdf.pdf` or
    `Household/wifi.pdf`. Path resolution + traversal protection are
    handled by `app.services.vault_paths.resolve`."""
    from app.services import vault_paths

    path_arg = arguments.get("path", "")
    target_arg = arguments.get("target", "")
    if not path_arg or not target_arg:
        return json.dumps({"error": "missing 'path' and/or 'target'"})
    try:
        src = scan.safe_resolve(path_arg)
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

    archived = scan.move_to(src, "archive")

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
    from app.integrations.historical_corpus.ingest import ingest_path

    path_arg = arguments.get("path", "")
    if not path_arg:
        return json.dumps({"error": "missing 'path'"})
    raw_tags = arguments.get("project_tags") or ["work"]
    if isinstance(raw_tags, str):
        raw_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

    try:
        src = scan.safe_resolve(path_arg)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    if not src.exists():
        return json.dumps({"error": f"not found: {path_arg}"})

    # ingest_path dispatches by suffix. If the file came in with no extension
    # (webhook senders often do this), we need to hand it a path with the right
    # suffix or the dispatcher returns 'unsupported'. Restore from sniff.
    if not src.suffix:
        kind = scan.sniff_kind(src)
        ext = {"pdf": ".pdf", "docx": ".docx", "xlsx": ".xlsx"}.get(kind)
        if not ext:
            return json.dumps({
                "error": f"corpus does not handle kind={kind!r}; use inbox_to_vault instead",
            })
        renamed = src.with_name(src.name + ext)
        src.rename(renamed)
        # Move the sidecar to match.
        old_sc = scan.sidecar_path(src)
        new_sc = scan.sidecar_path(renamed)
        if old_sc.exists():
            old_sc.rename(new_sc)
        src = renamed

    try:
        result = ingest_path(session, src, project_tags=list(raw_tags))
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[inbox_to_corpus] ingest failed: {src}")
        return json.dumps({"error": f"ingest failed: {e}"})

    archived = scan.move_to(src, "archive")
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
        {
            "name": "inbox_pending",
            "description": (
                "List items waiting in the inbox triage queue (files dropped via "
                "the automation webhook at /api/inbox/ingest). Each item carries a kind "
                "sniff, original filename, age, and a short preview so you can decide "
                "what to do with it without opening the file. Call this at the start "
                "of daily-note and refresh flows."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max items to return (default 20).",
                        "default": 20,
                    },
                },
            },
            "handler": handle_pending,
            "category": "inbox",
            "examples": [
                "What's in my inbox?",
                "Anything new dropped in the inbox?",
                "Triage the inbox",
            ],
        },
        {
            "name": "inbox_preview",
            "description": (
                "Return the full extracted text of one pending inbox item (PDF or "
                "plaintext/markdown). Use after `inbox_pending` when the short "
                "preview isn't enough to decide where the file should go."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute /inbox/... path of the item, as returned by inbox_pending.",
                    },
                },
                "required": ["path"],
            },
            "handler": handle_preview,
            "category": "inbox",
            "examples": [
                "Show me the full text of the first inbox PDF",
            ],
        },
        {
            "name": "inbox_to_vault",
            "description": (
                "Copy an inbox item into the vault at a chosen logical path (e.g. "
                "`Inbox/2026-05-22-receipt.pdf` or `Household/wifi.pdf`) and "
                "archive the original. Use this for files you want surfaced in "
                "Obsidian or attached to a daily note/project."
            ),
            "inputSchema": {
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
            "handler": handle_to_vault,
            "category": "inbox",
            "examples": [
                "Drop the PDF into my project folder for Renovation",
                "Save the inbox PDF into Household/wifi.pdf",
            ],
        },
        {
            "name": "inbox_to_corpus",
            "description": (
                "Ingest an inbox item (PDF/DOCX/XLSX) into the historical_documents "
                "corpus with the given project_tags, then archive. After this, the "
                "file is searchable via renovation_context and the unified semantic "
                "search."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute /inbox/... path of the item.",
                    },
                    "project_tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Project tags to attach (default ['work']).",
                    },
                },
                "required": ["path"],
            },
            "handler": handle_to_corpus,
            "category": "inbox",
            "examples": [
                "Ingest this contract into the corpus tagged renovation",
            ],
        },
        {
            "name": "inbox_archive",
            "description": (
                "Move an inbox item to /inbox/archive/ — i.e. mark it handled "
                "without copying it anywhere. Use for items you've already "
                "actioned by hand."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
            "handler": handle_archive,
            "category": "inbox",
            "examples": ["Archive that inbox item, I've handled it"],
        },
        {
            "name": "inbox_dismiss",
            "description": (
                "Move an inbox item to /inbox/dismissed/ — explicitly ignored. "
                "Kept on disk so we can audit accidental dismissals."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
            "handler": handle_dismiss,
            "category": "inbox",
            "examples": ["Dismiss that inbox item, not relevant"],
        },
    ]
