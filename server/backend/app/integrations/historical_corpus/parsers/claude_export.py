"""Parse Claude.ai data-export conversations.json files into per-conversation chunks.

Input shape (one or more files, from claude.ai Settings → Export data):
  [{uuid, name, summary, created_at, updated_at, account: {uuid},
    chat_messages: [{uuid, text, content, sender, created_at, ...}]}, ...]

Emission (per conversation = one document):
  - Short conversations (<=6000 chars OR <=1 message): one claude_conversation chunk.
  - Long conversations: one claude_conversation_summary + windowed
    claude_conversation_turn chunks (each up to ~4000 chars, never splitting
    a single message).

Caller iterates conversations across all export files and treats each as its
own DocMeta. The `source_path` on each doc encodes the conversation uuid so
the unique constraint holds even across re-exports.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from app.integrations.historical_corpus.parsers.types import ChunkRecord, DocMeta

TURN_MAX_CHARS = 4000
INLINE_MAX_CHARS = 6000


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        return None


def _render_message(m: dict) -> str:
    sender = "Alex" if m.get("sender") == "human" else "Claude"
    text = (m.get("text") or "").strip()
    if not text:
        return ""
    return f"{sender}: {text}"


def iter_conversations(paths: list[Path]) -> Iterator[tuple[str, DocMeta, list[ChunkRecord]]]:
    """Yield (conversation_uuid, DocMeta, chunks) across all given export files."""
    for path in paths:
        with path.open("r", encoding="utf-8") as f:
            conversations = json.load(f)

        for conv in conversations:
            conv_uuid = str(conv.get("uuid", ""))
            title = (conv.get("name") or "").strip() or "(untitled conversation)"
            messages = [m for m in conv.get("chat_messages", []) if (m.get("text") or "").strip()]
            if not messages:
                continue

            rendered = [_render_message(m) for m in messages]
            full_body = f"# {title}\n\n" + "\n\n".join(rendered)
            breadcrumb = f"Claude Chat › {title}"
            created = _parse_date(conv.get("created_at"))

            meta = DocMeta(
                title=title,
                source_type="claude_conversation",
                author="Alex",
                participants=["Alex", "Claude"],
                document_date=created,
                metadata={
                    "conversation_uuid": conv_uuid,
                    "message_count": len(messages),
                    "created_at": conv.get("created_at"),
                    "updated_at": conv.get("updated_at"),
                    "export_file": path.name,
                },
            )

            chunks: list[ChunkRecord] = []
            if len(full_body) <= INLINE_MAX_CHARS:
                chunks.append(ChunkRecord(
                    chunk_type="claude_conversation",
                    chunk_text=full_body,
                    breadcrumb=breadcrumb,
                    metadata={"conversation_uuid": conv_uuid, "message_count": len(messages)},
                ))
            else:
                first_human = next((m for m in messages if m.get("sender") == "human"), messages[0])
                summary_text = (conv.get("summary") or "").strip() or (first_human.get("text") or "")[:400]
                chunks.append(ChunkRecord(
                    chunk_type="claude_conversation_summary",
                    chunk_text=(
                        f"Claude conversation: {title}\n"
                        f"Date: {conv.get('created_at', '')}\n"
                        f"Messages: {len(messages)}\n\n"
                        f"{summary_text}"
                    ),
                    breadcrumb=breadcrumb,
                    metadata={"conversation_uuid": conv_uuid, "message_count": len(messages)},
                ))

                window: list[str] = []
                window_chars = 0
                window_start_idx = 0
                for i, text in enumerate(rendered):
                    if window and window_chars + len(text) > TURN_MAX_CHARS:
                        chunks.append(ChunkRecord(
                            chunk_type="claude_conversation_turn",
                            chunk_text="\n\n".join(window),
                            breadcrumb=f"{breadcrumb} › msgs {window_start_idx + 1}-{i}",
                            metadata={
                                "conversation_uuid": conv_uuid,
                                "message_start": window_start_idx,
                                "message_end": i - 1,
                            },
                        ))
                        window, window_chars, window_start_idx = [], 0, i
                    window.append(text)
                    window_chars += len(text)
                if window:
                    chunks.append(ChunkRecord(
                        chunk_type="claude_conversation_turn",
                        chunk_text="\n\n".join(window),
                        breadcrumb=f"{breadcrumb} › msgs {window_start_idx + 1}-{len(rendered)}",
                        metadata={
                            "conversation_uuid": conv_uuid,
                            "message_start": window_start_idx,
                            "message_end": len(rendered) - 1,
                        },
                    ))

            yield conv_uuid, meta, chunks
