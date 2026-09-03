"""MCP tools for Google Docs — read, write, edit.

Five `CustomTool`s. None of them fit `ListTool`/`SearchTool`/`StatsTool`,
because none of them query a local table: every one is a live call against
Google's APIs from the tool handler. That is what makes this an "action"
integration rather than a "source" one.

Which account's token each tool uses is resolved by `_account()` below —
read that first, because it is the one piece of behaviour shared by all
five and the one most likely to surprise.

Annotations are explicit on every tool (`CustomTool` has no defaults, by
design): `openWorldHint` is true throughout since all five reach Google,
and `docs_replace`/`docs_write` carry `destructiveHint` because they
overwrite content that was previously there.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.auth.context import current_user_id
from app.errors import PermanentError
from app.integrations.google_docs import writer
from app.integrations.google_docs.models import DocExport
from app.tools import CustomTool, ToolAnnotations

logger = logging.getLogger(__name__)


def _account(session: Session, *, prefer_owner: bool) -> tuple[str, int]:
    """Resolve which Google account's token to use, as (email, user_id).

    `prefer_owner` splits the two cases, and the split is the point:

      - **Creating** a document (`prefer_owner=True`) should use the
        configured `docs_owner_account` when there is one, so a shared
        document does not change hands depending on who happened to ask for
        it — the same reasoning `snags` applies to its Sheets export.
      - **Reading or editing an existing** document (`prefer_owner=False`)
        should use the *caller's* own token first. A document someone asks
        about is usually theirs or shared with them, and the household owner
        account may not be able to see it at all. Preferring the owner here
        would make `docs_read` fail on exactly the documents a person is
        most likely to ask about.

    Either way the other account is the fallback, so a household with one
    Google account behaves identically under both.

    Raises `PermanentError` naming what is missing rather than returning
    None, so the failure arrives at the call that needed it. Per the root
    CLAUDE.md rule, `docs_owner_account` is deliberately not a `required`
    config key — marking it required would gate the read paths off too.
    """
    from app.models.tokens import OAuthToken
    from app.plugin.config_store import plugin_config

    configured = plugin_config("google_docs").docs_owner_account

    def _owner() -> tuple[str, int] | None:
        if not configured:
            return None
        token = (
            session.query(OAuthToken)
            .filter_by(provider="google", account_email=configured)
            .first()
        )
        if token is None:
            logger.warning(
                f"[google_docs] docs_owner_account {configured!r} has no stored "
                "OAuth token — falling back to the calling user's own account"
            )
            return None
        return configured, token.user_id

    def _caller() -> tuple[str, int] | None:
        user_id = current_user_id()
        token = (
            session.query(OAuthToken)
            .filter_by(provider="google", user_id=user_id)
            .first()
        )
        return (token.account_email, user_id) if token is not None else None

    order = (_owner, _caller) if prefer_owner else (_caller, _owner)
    for resolve in order:
        resolved = resolve()
        if resolved is not None:
            return resolved

    raise PermanentError(
        "no usable Google account: this user has no connected Google account, "
        f"and google_docs' docs_owner_account is "
        f"{'set to ' + repr(configured) + ' but has no stored token' if configured else 'unset'}"
        " — connect a Google account or set docs_owner_account"
    )


def _share_with() -> list[str]:
    from app.plugin.config_store import plugin_config

    return list(plugin_config("google_docs").docs_share_with or [])


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def handle_read(session: Session, arguments: dict[str, Any]) -> str:
    document = (arguments.get("document") or "").strip()
    if not document:
        return json.dumps({"error": "document is required (a document id or a Google Docs URL)"})
    account_email, user_id = _account(session, prefer_owner=False)
    result = writer.read_markdown(
        session, document=document, account_email=account_email, user_id=user_id,
    )
    return json.dumps(result)


def handle_write(session: Session, arguments: dict[str, Any]) -> str:
    """Create-or-overwrite the document tracked under `key`.

    The create-once/overwrite-on-write contract is the point: the same `key`
    always resolves to the same document id, so a document revised weekly
    keeps one URL and one set of shares for its whole life.
    """
    key = (arguments.get("key") or "").strip()
    markdown = arguments.get("markdown") or ""
    if not key:
        return json.dumps({"error": "key is required"})
    if not markdown.strip():
        return json.dumps({"error": "markdown is empty — refusing to blank the document"})

    title = (arguments.get("title") or key).strip()
    export = session.query(DocExport).filter_by(key=key).one_or_none()

    if export is None:
        account_email, user_id = _account(session, prefer_owner=True)
        share_with = arguments.get("share_with")
        export = writer.ensure_export(
            session,
            key=key,
            title=title,
            markdown=markdown,
            owner_account_email=account_email,
            owner_user_id=user_id,
            share_with=list(share_with) if share_with is not None else _share_with(),
        )
        if export is None:
            return json.dumps({"error": f"no valid Google credentials for {account_email}"})
        return json.dumps({
            "created": True, "key": export.key, "title": export.title,
            "document_id": export.document_id, "url": export.document_url,
        })

    # Existing doc: the owning account is whatever created it, not whoever is
    # calling now — otherwise a rewrite by the other household member would
    # fail on a file their token has no drive.file grant for.
    from app.models.tokens import OAuthToken

    owner = (
        session.query(OAuthToken)
        .filter_by(provider="google", account_email=export.owner_account_email)
        .first()
    )
    if owner is None:
        return json.dumps({
            "error": f"document {key!r} is owned by {export.owner_account_email}, "
                     "which has no stored OAuth token",
        })
    writer.write_markdown(session, export, owner_user_id=owner.user_id, markdown=markdown)
    return json.dumps({
        "created": False, "key": export.key, "title": export.title,
        "document_id": export.document_id, "url": export.document_url,
    })


def handle_append(session: Session, arguments: dict[str, Any]) -> str:
    document = (arguments.get("document") or "").strip()
    text = arguments.get("text") or ""
    if not document:
        return json.dumps({"error": "document is required"})
    if not text:
        return json.dumps({"error": "text is required"})
    account_email, user_id = _account(session, prefer_owner=False)
    result = writer.append_text(
        session, document=document, text=text,
        account_email=account_email, user_id=user_id,
    )
    return json.dumps(result)


def handle_replace(session: Session, arguments: dict[str, Any]) -> str:
    document = (arguments.get("document") or "").strip()
    find = arguments.get("find") or ""
    if not document:
        return json.dumps({"error": "document is required"})
    if not find:
        return json.dumps({"error": "find is required"})
    account_email, user_id = _account(session, prefer_owner=False)
    result = writer.replace_text(
        session,
        document=document,
        find=find,
        replace=arguments.get("replace") or "",
        match_case=bool(arguments.get("match_case", True)),
        account_email=account_email,
        user_id=user_id,
    )
    return json.dumps(result)


def handle_list(session: Session, arguments: dict[str, Any]) -> str:
    rows = session.query(DocExport).order_by(DocExport.key).all()
    return json.dumps({
        "count": len(rows),
        "documents": [
            {
                "key": row.key,
                "title": row.title,
                "url": row.document_url,
                "owner": row.owner_account_email,
                "last_written_at": row.last_synced_at.isoformat() if row.last_synced_at else None,
            }
            for row in rows
        ],
    })


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_DOCUMENT_ARG = {
    "type": "string",
    "description": "Document id or any Google Docs URL (both accepted).",
}


def get_mcp_tools() -> list[dict[str, Any]]:
    return [
        CustomTool(
            name="docs_read",
            description=(
                "Read a Google Doc and return its contents as markdown. Works on any "
                "document the connected Google account can open, including ones created "
                "by hand. Headings, lists, tables, links and bold/italic are preserved."
            ),
            input_schema={
                "type": "object",
                "properties": {"document": _DOCUMENT_ARG},
                "required": ["document"],
            },
            handler=handle_read,
            annotations=ToolAnnotations(
                title="Read a Google Doc",
                read_only_hint=True, idempotent_hint=True, open_world_hint=True,
            ),
            category="docs",
            examples=[
                "Read that Google Doc and summarise it",
                "What does the shared doc say about the timeline?",
            ],
        ).build(),
        CustomTool(
            name="docs_write",
            description=(
                "Create or fully rewrite a Google Doc from markdown, tracked under a "
                "stable key. First call creates the document and shares it; later calls "
                "with the same key replace its contents while keeping the same URL, the "
                "same shares, and the document's Google Docs version history. Markdown "
                "headings, nested lists, tables, code and links become real Docs "
                "formatting. Use this for formatted content; use docs_append to add a "
                "plain line to an existing doc."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Stable identifier for this document, e.g. 'project-brief'. Same key always means the same document.",
                    },
                    "markdown": {"type": "string", "description": "Full document contents as markdown."},
                    "title": {"type": "string", "description": "Document title, used on creation only. Defaults to the key."},
                    "share_with": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Emails to share with as writers, on creation only. Defaults to the configured docs_share_with list.",
                    },
                },
                "required": ["key", "markdown"],
            },
            handler=handle_write,
            annotations=ToolAnnotations(
                title="Write a Google Doc",
                read_only_hint=False, destructive_hint=True,
                idempotent_hint=True, open_world_hint=True,
            ),
            category="docs",
            examples=[
                "Write these notes up into a Google Doc I can share",
                "Update the brief doc with the revised plan",
            ],
        ).build(),
        CustomTool(
            name="docs_append",
            description=(
                "Append plain text to the end of a Google Doc. Text is inserted "
                "literally — markdown syntax will NOT become formatting, so use "
                "docs_write for anything that needs headings or tables. Works on any "
                "document the connected account can edit."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "document": _DOCUMENT_ARG,
                    "text": {"type": "string", "description": "Plain text to append. Include a leading newline if you want a new paragraph."},
                },
                "required": ["document", "text"],
            },
            handler=handle_append,
            annotations=ToolAnnotations(
                title="Append to a Google Doc",
                read_only_hint=False, destructive_hint=False,
                idempotent_hint=False, open_world_hint=True,
            ),
            category="docs",
            examples=["Add a line to the end of the running log doc"],
        ).build(),
        CustomTool(
            name="docs_replace",
            description=(
                "Find and replace text throughout a Google Doc, preserving all "
                "surrounding formatting. The targeted-edit tool: use it to change a "
                "date, a name or a sentence without rewriting the document. Returns how "
                "many occurrences changed, so zero is visible rather than silent."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "document": _DOCUMENT_ARG,
                    "find": {"type": "string", "description": "Exact text to find."},
                    "replace": {"type": "string", "description": "Replacement text. Empty string deletes the found text."},
                    "match_case": {"type": "boolean", "default": True, "description": "Whether the search is case-sensitive."},
                },
                "required": ["document", "find"],
            },
            handler=handle_replace,
            annotations=ToolAnnotations(
                title="Find and replace in a Google Doc",
                read_only_hint=False, destructive_hint=True,
                idempotent_hint=True, open_world_hint=True,
            ),
            category="docs",
            examples=[
                "Change every mention of the old completion date in that doc",
                "Fix the misspelled surname throughout the doc",
            ],
        ).build(),
        CustomTool(
            name="docs_list",
            description=(
                "List the Google Docs comar created and tracks, with their keys and "
                "URLs. Documents created by hand are not listed — only ones written "
                "through docs_write."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=handle_list,
            annotations=ToolAnnotations(
                title="List tracked Google Docs",
                read_only_hint=True, idempotent_hint=True, open_world_hint=False,
            ),
            category="docs",
            examples=["Which Google Docs has comar written?"],
        ).build(),
    ]
