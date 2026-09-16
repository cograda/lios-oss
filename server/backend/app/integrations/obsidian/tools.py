"""MCP tool definitions and handlers for Obsidian vault integration.

vault_search stays CustomTool because the SemanticSearchTool builder doesn't
expose the folder/source-filter the EmbeddingService supports. vault_recent
and vault_stats fit ListTool / StatsTool cleanly.
vault_transfer is a CustomTool — it moves a file across user vaults; no DSL
shape fits.

Every read of `VaultChunk`/`Embedding` in this module goes through
`app.services.vault_scope.restrict` (2026-09-06) — that is the one place a
folder-scoped cross-user grant is enforced, and a Session-level guard refuses
any vault-table read that bypasses it while such a grant is bound. A new read
tool here must route its queries through the same call; forgetting it does
not leak, it refuses.
"""

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.auth.context import current_user_id
from app.config import settings
from app.integrations.obsidian.models import VaultChunk
from app.models.users import User
from app.services import vault_paths, vault_scope
from app.services.embedding import (
    DEFAULT_DUPLICATE_EXCLUDES,
    Embedding,
    EmbeddingService,
    MODEL_NAME,
)
from app.tools import CustomTool, ExtraFilter, ListTool, StatsTool, ToolAnnotations
from app.tools.helpers import iso_or_none, serialize

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hand-written: vault_search (needs source_filter passthrough)
# ---------------------------------------------------------------------------

def handle_search(session: Session, arguments: dict[str, Any]) -> str:
    """Semantic search across the vault, optionally scoped to a folder."""
    query = arguments.get("query", "").strip()
    if not query:
        return json.dumps({"error": "query is required"})

    top_k = min(int(arguments.get("limit", 10)), 50)
    folder = arguments.get("folder")

    # Status filtering rides `metadata_json`, not the embedded text — a note's
    # lifecycle must be filterable without bleeding into its semantic position.
    # Same JSONB cast whatsapp/tools.py already uses for its `start` key.
    extra_filter = None
    wanted = arguments.get("status")
    if wanted:
        if isinstance(wanted, str):
            wanted = [wanted]
        wanted = [str(w).strip().lower() for w in wanted if str(w).strip()]
        if wanted:
            extra_filter = cast(Embedding.metadata_json, JSONB)["status"].astext.in_(
                wanted
            )

    # Over-fetch, because the results below are collapsed per *document* and a
    # heavily chunked note can otherwise occupy every slot. The vault averages
    # ~10 chunks per file, and a real query for "comar architecture plan"
    # returned six hits that were six chunks of one file — one document where
    # six were asked for. EmbeddingService.search caps at 50 internally, so
    # that is the ceiling on how much we can widen.
    # R4 decay decision: ON (the default, no override needed) — this is the
    # flagship case R4 exists for: the 1 Aug `.stversions` snapshot that
    # outranked its own live file at 0.7651 vs 0.7600. `status` already labels
    # done/superseded notes rather than hiding them (see `_status_of` below);
    # decay is the complementary fix for the case a stale copy isn't even
    # labelled — a snapshot, or a duplicate nobody has marked superseded yet.
    results = EmbeddingService.search(
        session, query=query, sources=["vault"],
        limit=min(max(top_k * 5, top_k), 50),
        source_filter=folder,
        extra_filter=extra_filter,
    )

    # Large list-structured files are chunked by heading/bullet (see
    # chunking.py) — a chunk's source_id is `{path}#{index}`, not the bare
    # path VaultChunk tracks. Strip the suffix to recover the real file path
    # for the mtime lookup below; a whole-file (unchunked) result's
    # source_id has no "#" and passes through unchanged.
    paths = [r["source_id"].split("#", 1)[0] for r in results]
    mtimes: dict[str, Any] = {}
    if paths:
        mtime_q = session.query(VaultChunk.path, VaultChunk.modified_at).filter(
            VaultChunk.user_id == current_user_id(),
            VaultChunk.path.in_(paths),
        )
        # `paths` already came out of a restricted search, but this lookup is
        # a vault-table read in its own right and goes through the chokepoint
        # like every other — the guard would refuse it otherwise.
        rows = vault_scope.restrict(mtime_q, VaultChunk.path).all()
        # EmbeddingService.search already scopes to the caller (own + shared);
        # this mtime lookup must match, or one user's file dates leak through
        # the other's results whenever the two vaults share a relative path.
        mtimes = {r.path: r.modified_at for r in rows}

    def _status_of(r) -> str | None:
        """Every result carries its status — the decision is to label, not hide.

        `vault_search` deliberately does NOT exclude `done`/`superseded` by
        default. A search that silently drops things produces confidently wrong
        "there is nothing about that" answers, which is the same failure the
        `.stversions` bug had: damaging precisely because it was invisible.
        The honest fix is to make staleness *visible* and let the reader weigh
        it — a hit labelled `[superseded]` is not a trap, a hit silently
        dropped is a different kind of wrong. Callers who want precision pass
        `status`.
        """
        try:
            meta = json.loads(r.get("metadata") or "{}")
        except (TypeError, ValueError):
            return None
        return meta.get("status")

    # Collapse to one row per document, keeping its best-scoring chunk.
    #
    # A result list is a list of *notes*, not of chunk offsets — `Task
    # Backlog.md` chunks into dozens of rows and would otherwise answer every
    # query about tasks with itself, twelve times. The preview kept is the
    # best-matching chunk's, which is the excerpt worth reading. `vault_similar`
    # already aggregated this way; search was the inconsistent one.
    #
    # `chunks_matched` is reported because it is real signal: a note matching in
    # one place is a mention, a note matching in nine is about the subject.
    #
    # ⚠️ This can return fewer than `limit` documents when one note dominates
    # the top 50 chunks. Returning fewer real documents is the honest outcome —
    # the alternative is padding the list with the same file again.
    by_doc: dict[str, dict] = {}
    for r in results:
        doc = r["source_id"].split("#", 1)[0]
        prev = by_doc.get(doc)
        if prev is None:
            by_doc[doc] = {
                "path": doc,
                "score": r["score"],
                "status": _status_of(r),
                # R4 provenance, carried through from EmbeddingService.search:
                # is_history/stale are what already fed the recency decay
                # baked into `score` above, surfaced too so a reader can see
                # *why* a hit ranks where it does, not just that it does.
                "is_history": r.get("is_history", False),
                "stale": r.get("stale", False),
                "source_date": r.get("source_date"),
                "preview": r["preview"][:200],
                "chunks_matched": 1,
                "modified": mtimes[doc].isoformat() if mtimes.get(doc) else None,
            }
        else:
            prev["chunks_matched"] += 1
            if r["score"] > prev["score"]:
                prev["score"] = r["score"]
                prev["preview"] = r["preview"][:200]
                prev["is_history"] = r.get("is_history", False)
                prev["stale"] = r.get("stale", False)
                prev["source_date"] = r.get("source_date")

    formatted = sorted(by_doc.values(), key=lambda d: d["score"], reverse=True)[:top_k]
    return json.dumps(formatted, indent=2)


# ---------------------------------------------------------------------------
# Hand-written: vault_similar / vault_duplicates (vector-native, no API calls)
# ---------------------------------------------------------------------------

def handle_similar(session: Session, arguments: dict[str, Any]) -> str:
    """More-like-this for a vault note, starting from its stored vector.

    Unlike `vault_search` this embeds nothing — it reads a vector already in
    Postgres — so it is free, fast, and works with no network. That makes it
    the right tool for "what else is about this", and for the question a text
    query is bad at: "find the other copy of this note".
    """
    path = (arguments.get("path") or "").strip()
    if not path:
        return json.dumps({"error": "path is required"})

    top_k = min(int(arguments.get("limit", 10)), 50)
    results = EmbeddingService.similar_to(
        session, "vault", path,
        limit=top_k,
        source_filter=arguments.get("folder"),
        min_score=float(arguments.get("min_score", 0.0)),
    )
    if not results:
        return json.dumps({
            "path": path,
            "results": [],
            "note": (
                "No vectors found for that path, or no neighbours above the "
                "threshold. Check the path is exactly as vault_search reports "
                "it (vault-relative, including the .md extension)."
            ),
        }, indent=2)

    return json.dumps({
        "path": path,
        "space": results[0]["space"],
        "results": [
            {"path": r["source_id"], "score": r["score"], "preview": r["preview"][:200]}
            for r in results
        ],
    }, indent=2)


def handle_duplicates(session: Session, arguments: dict[str, Any]) -> str:
    """Find near-identical note pairs — conflict twins, forks, stale copies."""
    threshold = float(arguments.get("threshold", 0.95))
    excludes = arguments.get("exclude_folders")
    prefixes = (
        [p if p.endswith("/") else p + "/" for p in excludes]
        if excludes is not None
        else list(DEFAULT_DUPLICATE_EXCLUDES)
    )
    pairs = EmbeddingService.near_duplicates(
        session, "vault",
        threshold=threshold,
        source_filter=arguments.get("folder"),
        limit=min(int(arguments.get("limit", 50)), 200),
        exclude_prefixes=prefixes,
    )
    return json.dumps({
        "threshold": threshold,
        "folder": arguments.get("folder"),
        "excluded_prefixes": prefixes,
        "pair_count": len(pairs),
        "pairs": pairs,
        "note": (
            "A high score means 'worth a human look', not 'identical'. Diff the "
            "two before deleting either — the shorter file is not reliably the "
            "stale one."
        ),
    }, indent=2)


# ---------------------------------------------------------------------------
# DSL: ListTool folder filter (prefix ILIKE) + StatsTool compute
# ---------------------------------------------------------------------------

def _folder_prefix(session: Session, query, value: str):
    """Filter VaultChunk.path with a startswith ILIKE, inside any grant scope."""
    return vault_scope.restrict(query, VaultChunk.path, value)


def _grant_scope(session: Session, query):
    """ListTool `scope_filter`: the grant's folder scope with no caller filter.

    `ExtraFilter.apply` skips a filter whose argument is absent, so without
    this a `vault_recent` call with no `folder` would list the owner's whole
    vault under a folder-scoped grant. Applied unconditionally; a no-op when
    no grant is bound.
    """
    return vault_scope.restrict(query, VaultChunk.path)


def _vault_to_dict(c: VaultChunk) -> dict:
    return serialize(
        c, ["path", "modified_at"],
        renames={"modified_at": "modified"},
        transforms={"modified_at": iso_or_none},
    )


def _vault_stats_compute(session: Session, _arguments: dict[str, Any]) -> dict:
    from app.services.embedding import Embedding, EmbeddingQueue

    # Index health for the caller's own vault — a household total would make
    # a stalled index look healthy on the strength of the other user's files.
    uid = current_user_id()

    # Under a folder-scoped grant every count below covers the granted
    # folders only — a whole-vault total would tell a grantee how much of the
    # owner's vault they cannot see, and `by_folder` would name the folders.
    def _scoped(q, column):
        return vault_scope.restrict(q, column)

    total_tracked = (
        _scoped(session.query(func.count(VaultChunk.id))
                .filter(VaultChunk.user_id == uid), VaultChunk.path)
        .scalar() or 0
    )
    total_embedded = (
        _scoped(session.query(func.count(Embedding.id))
                .filter_by(source="vault", user_id=uid), Embedding.source_id)
        .scalar() or 0
    )
    latest = (
        _scoped(session.query(func.max(VaultChunk.indexed_at))
                .filter(VaultChunk.user_id == uid), VaultChunk.path)
        .scalar()
    )

    folders = (
        _scoped(
            session.query(
                func.split_part(VaultChunk.path, "/", 1).label("folder"),
                func.count(VaultChunk.id),
            )
            .filter(VaultChunk.user_id == uid),
            VaultChunk.path,
        )
        .group_by("folder")
        .order_by(func.count(VaultChunk.id).desc())
        .all()
    )

    queue_pending = (
        _scoped(session.query(func.count(EmbeddingQueue.id))
                .filter_by(source="vault", status="pending", user_id=uid),
                EmbeddingQueue.source_id)
        .scalar() or 0
    )

    return {
        "total_files": total_tracked,
        "total_embedded": total_embedded,
        "queue_pending": queue_pending,
        "model": MODEL_NAME,
        "last_indexed": latest.isoformat() if latest else None,
        "by_folder": {name: count for name, count in folders},
    }


# ---------------------------------------------------------------------------
# Hand-written: vault_transfer (move a file from caller's vault to recipient's Inbox/)
# ---------------------------------------------------------------------------

def handle_transfer(session: Session, arguments: dict[str, Any]) -> str:
    """Move a file from the calling user's vault into a recipient's Inbox.

    True handoff semantics — the file leaves the sender's vault and lands in
    the recipient's `Inbox/<filename>`. Syncthing propagates both the delete
    (from sender) and the create (in recipient) to all paired Macs within
    seconds; no further wiring needed.

    The caller-facing `path` is logical (e.g. `Notes/recipe.md`); it resolves
    against the calling user's vault via `vault_paths.resolve()`.
    """
    path_arg = (arguments.get("path") or "").strip()
    recipient_name = (arguments.get("recipient") or "").strip().lower()
    if not path_arg or not recipient_name:
        return json.dumps({"error": "both `path` and `recipient` are required"})

    uid = current_user_id()
    if uid is None:
        return json.dumps({"error": "no authenticated user"})

    sender = session.get(User, uid)
    if sender is None:
        return json.dumps({"error": "calling user not found"})
    if sender.name == recipient_name:
        return json.dumps({"error": "cannot transfer to yourself"})

    recipient = session.execute(
        select(User).where(User.name == recipient_name)
    ).scalar_one_or_none()
    if recipient is None or not recipient.is_active:
        return json.dumps({"error": f"unknown or inactive recipient: {recipient_name}"})

    src = vault_paths.resolve(path_arg)
    if not src.is_file():
        return json.dumps({"error": f"source file not found: {path_arg}"})

    inbox = vault_paths.user_vault_path(recipient.name) / "Inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    # Avoid overwriting an existing file in the recipient's inbox: prefix a timestamp.
    dst = inbox / src.name
    if dst.exists():
        stem, suffix = src.stem, src.suffix
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dst = inbox / f"{stem}.{ts}{suffix}"

    shutil.move(str(src), str(dst))
    logger.info(
        "vault_transfer: %s/%s → %s/Inbox/%s",
        sender.name, path_arg, recipient.name, dst.name,
    )

    # CRITICAL: poke Syncthing to rescan both affected folders immediately.
    # Without this, Syncthing waits up to 60s for its next periodic rescan, and
    # the calling user's Mac (which still has the source file locally) will
    # announce its version first → server resurrects the file from Mac's
    # "newer" view. The rescan flips the order so Syncthing notices the
    # server-side delete + create before Mac has time to re-announce.
    try:
        _poke_rescan(f"vault-{sender.name}", str(src.relative_to(vault_paths.user_vault_path(sender.name))))
        _poke_rescan(f"vault-{recipient.name}", str(dst.relative_to(vault_paths.user_vault_path(recipient.name))))
    except Exception:
        logger.exception("vault_transfer: rescan poke failed (non-fatal; sync will catch up at next periodic rescan)")

    return json.dumps({
        "ok": True,
        "from": f"{sender.name}/{path_arg}",
        "to": f"{recipient.name}/Inbox/{dst.name}",
    })


def _poke_rescan(folder_id: str, sub_path: str) -> None:
    """Tell the local Syncthing to rescan a specific path under a folder.

    `sub_path` is relative to the folder root; rescanning a specific subpath is
    faster than rescanning the whole folder. Blocking but fast (<200ms).
    """
    import httpx

    if not settings.syncthing_api_key:
        return  # No-op if Syncthing isn't wired (e.g. dev environment)

    base = settings.syncthing_url.rstrip("/")
    params = {"folder": folder_id}
    if sub_path:
        params["sub"] = sub_path
    r = httpx.post(
        f"{base}/rest/db/scan",
        params=params,
        headers={"X-API-Key": settings.syncthing_api_key},
        timeout=5.0,
    )
    if r.status_code not in (200, 204):
        logger.warning("Syncthing rescan poke for %s/%s returned %s", folder_id, sub_path, r.status_code)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

def get_mcp_tools() -> list[dict]:
    return [
        CustomTool(
            name="vault_search",
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
            description=(
                "Semantic search across the Obsidian vault — finds notes by meaning, "
                "not just keywords. Returns one row per **note** (not per chunk), each "
                "with its best-matching excerpt, similarity score, frontmatter `status`, "
                "and `chunks_matched` — a note matching in one place is a mention, a "
                "note matching in nine is about the subject. `score` already applies "
                "recency decay so a live file outranks its own stale snapshot; "
                "`is_history`/`stale`/`source_date` explain why a hit ranks where it "
                "does. Optionally filter by folder or status. "
                "For keyword search, use the client's native grep against the vault path."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Max notes to return (default 10, max 50). Fewer may come "
                            "back than asked for: results are collapsed per note, so "
                            "one heavily chunked file dominating the matches yields "
                            "fewer distinct notes rather than the same file repeated."
                        ),
                        "default": 10,
                    },
                    "folder": {
                        "type": "string",
                        "description": "Filter to a vault folder (e.g. 'Household', 'Daily Notes/Alex').",
                    },
                    "status": {
                        "type": ["string", "array"],
                        "items": {"type": "string"},
                        "description": (
                            "Restrict to notes whose frontmatter `status` is one of "
                            "these. Project docs use active | next | parked | done | "
                            "superseded; blog posts use draft | published. "
                            "Omitted by default — results are NOT filtered, and every "
                            "result carries its own `status` field instead, so you can "
                            "see that a hit is `done` or `superseded` rather than have "
                            "it silently withheld."
                        ),
                    },
                },
                "required": ["query"],
            },
            handler=handle_search,
            category="search",
            examples=[
                "Find notes about the renovation",
                "Search for a family member's medical notes",
            ],
        ).build(),

        CustomTool(
            name="vault_similar",
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
            description=(
                "Find vault notes similar to a given note, starting from its stored "
                "vector — no query to compose and no embedding call, so this is free "
                "and works offline. Use it for 'what else covers this?', for finding "
                "a note's duplicate or superseded twin, and for gathering scattered "
                "material on one topic before consolidating it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Vault-relative path of the seed note, e.g. "
                            "'Projects/lios/Backlog.md'. Exactly as vault_search reports it."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10, max 50).",
                        "default": 10,
                    },
                    "folder": {
                        "type": "string",
                        "description": "Restrict neighbours to a folder prefix.",
                    },
                    "min_score": {
                        "type": "number",
                        "description": "Drop results below this cosine similarity (0-1).",
                        "default": 0,
                    },
                },
                "required": ["path"],
            },
            handler=handle_similar,
            category="search",
            examples=[
                "What else is about the same thing as this plan?",
                "Find the other copy of this note",
            ],
        ).build(),

        CustomTool(
            name="vault_duplicates",
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
            description=(
                "Find pairs of near-identical vault notes (default cosine >= 0.95). "
                "Catches sync-conflict twins, copy-paste forks, and superseded drafts "
                "that a keyword search cannot. Read-only — it reports pairs, it never "
                "deletes. Scope with 'folder' to keep a run quick."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threshold": {
                        "type": "number",
                        "description": "Minimum cosine similarity to report (default 0.95).",
                        "default": 0.95,
                    },
                    "folder": {
                        "type": "string",
                        "description": (
                            "Restrict to a folder prefix, e.g. 'Projects/lios'. "
                            "Recommended — a whole-vault run is a few hundred queries."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max pairs to return (default 50, max 200).",
                        "default": 50,
                    },
                    "exclude_folders": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Folders to leave out of the comparison. Defaults to "
                            "['Daily Notes', 'Weekly Reviews'] because template-"
                            "generated notes are near-identical to each other by "
                            "construction — an unfiltered run buried every real "
                            "duplicate under 45 daily-note pairs. Pass [] to "
                            "compare everything."
                        ),
                    },
                },
            },
            handler=handle_duplicates,
            category="search",
            examples=[
                "Find duplicate notes in the vault",
                "Which plans in Projects/lios overlap almost exactly?",
            ],
        ).build(),

        ListTool(
            name="vault_recent",
            description=(
                "List recently modified files in the Obsidian vault, sorted newest first. "
                "Shows file path and modification time. Use this to see what's been "
                "worked on lately or find recently created notes."
            ),
            model=VaultChunk,
            timestamp_col="modified_at",
            to_dict=_vault_to_dict,
            default_limit=20,
            max_limit=100,
            scope_filter=_grant_scope,
            extra_filters=[
                ExtraFilter(
                    param_name="folder",
                    column="path",
                    description="Filter to a vault folder (prefix match).",
                    match_mode=_folder_prefix,
                ),
            ],
            category="home",
            examples=[
                "What notes were changed recently?",
                "Show recent daily notes",
            ],
        ).build(),

        CustomTool(
            name="vault_transfer",
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
            description=(
                "Move a file from your personal vault into another user's Inbox. "
                "True handoff — the file leaves your vault and appears in theirs. "
                "Useful for passing tasks, notes, or recipes to a household member. "
                "Recipient's Mac receives the file within seconds via Syncthing."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Logical path inside your vault (e.g. 'Notes/recipe.md').",
                    },
                    "recipient": {
                        "type": "string",
                        "description": "Short username of the recipient, as in the users table.",
                    },
                },
                "required": ["path", "recipient"],
            },
            handler=handle_transfer,
            category="home",
            examples=[
                "Send 'Notes/Carrot Soup.md' to another user",
                "Hand off the renovation snag list to Alex",
            ],
        ).build(),

        StatsTool(
            name="vault_stats",
            description=(
                "Vault index statistics: total files indexed, embedding count, "
                "queue status, embedding model info, and file counts by folder. "
                "Admin tool for checking index health."
            ),
            model=VaultChunk,
            compute=_vault_stats_compute,
            input_schema={"type": "object", "properties": {}},
            category="system",
        ).build(),
    ]
