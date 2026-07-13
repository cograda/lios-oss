"""Parse email_conversations.json (bulk JSON dump) into per-thread chunks.

Input shape:
  {'metadata': {...},
   'conversations': [{thread_id, subject, participants, message_count,
                      start_date, last_date,
                      messages: [{from: {name, email} | str, to, cc, date,
                                  content, ...}]}]}

Emission (per conversation = one document):
  - Short threads (<=6000 chars OR single message): one email_thread chunk.
  - Long threads: one email_thread_summary + one email_message per message.

Caller iterates conversations and treats each as its own DocMeta — meaning a
single input file produces many HistoricalDocument rows. The `source_path` on
each doc encodes the thread_id so the unique constraint still holds.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from app.integrations.historical_corpus.parsers.types import ChunkRecord, DocMeta


THREAD_MAX_CHARS = 6000


def _render_address(addr) -> str:
    if isinstance(addr, dict):
        name = (addr.get("name") or "").strip().strip('"').strip("'")
        email = (addr.get("email") or "").strip()
        if name and email:
            return f"{name} <{email}>"
        return email or name
    if isinstance(addr, str):
        return addr.strip()
    return ""


def _render_message(m: dict) -> str:
    sender = _render_address(m.get("from"))
    to = _render_address(m.get("to"))
    date_str = m.get("date", "")
    content = (m.get("content") or "").strip()
    parts = [f"From: {sender}", f"To: {to}", f"Date: {date_str}"]
    if m.get("cc"):
        parts.append(f"Cc: {_render_address(m['cc'])}")
    parts.append("")
    parts.append(content)
    return "\n".join(parts)


def _parse_date(s: str) -> date | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(s[: len(fmt)], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        return None


def iter_threads(path: Path) -> Iterator[tuple[str, DocMeta, list[ChunkRecord]]]:
    """Yield (thread_id, DocMeta, chunks) per conversation in the file."""
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    for conv in data.get("conversations", []):
        thread_id = str(conv.get("thread_id", ""))
        subject = (str(conv.get("subject", "")).strip() or "(no subject)")
        raw_participants = conv.get("participants") or []
        participants = [
            _render_address(p) if isinstance(p, dict) else str(p)
            for p in raw_participants
        ] if isinstance(raw_participants, list) else []
        start = str(conv.get("start_date", ""))
        end = str(conv.get("last_date", ""))
        messages = conv.get("messages", [])

        rendered = [_render_message(m) for m in messages]
        full_body = f"Subject: {subject}\n\n" + "\n\n---\n\n".join(rendered)
        breadcrumb = f"Email › {subject}"

        meta = DocMeta(
            title=subject,
            source_type="email_json",
            author=_render_address(messages[0].get("from")) if messages else None,
            participants=participants,
            document_date=_parse_date(start),
            metadata={
                "thread_id": thread_id,
                "message_count": len(messages),
                "start_date": start,
                "last_date": end,
            },
        )

        chunks: list[ChunkRecord] = []
        if len(full_body) <= THREAD_MAX_CHARS or len(messages) <= 1:
            chunks.append(ChunkRecord(
                chunk_type="email_thread",
                chunk_text=full_body,
                breadcrumb=breadcrumb,
                metadata={"thread_id": thread_id, "message_count": len(messages)},
            ))
        else:
            summary = (
                f"Email thread: {subject}\n"
                f"Participants: {', '.join(participants)}\n"
                f"Date range: {start} → {end}\n"
                f"Messages: {len(messages)}"
            )
            chunks.append(ChunkRecord(
                chunk_type="email_thread_summary",
                chunk_text=summary,
                breadcrumb=breadcrumb,
                metadata={"thread_id": thread_id, "message_count": len(messages)},
            ))
            for i, m in enumerate(messages):
                chunks.append(ChunkRecord(
                    chunk_type="email_message",
                    chunk_text=f"Subject: {subject}\n\n{_render_message(m)}",
                    breadcrumb=f"{breadcrumb} › msg {i+1}/{len(messages)}",
                    metadata={
                        "thread_id": thread_id,
                        "message_index": i,
                        "from": _render_address(m.get("from")),
                        "date": m.get("date", ""),
                    },
                ))
        yield thread_id, meta, chunks
