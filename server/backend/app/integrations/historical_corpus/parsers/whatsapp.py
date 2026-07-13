"""Parse WhatsApp .txt exports into conversation-window chunks.

Format: `[DD/MM/YYYY, HH:MM:SS] Sender: Message`
System lines may be prefixed with U+200E (LTR mark) — stripped before matching.

Emission:
  - One DocMeta per chat (participants = all senders seen).
  - One conversation_header chunk (chat-level summary).
  - N conversation_window chunks (30-min gap OR 3000-char cap starts a new window).
Skips: E2E notices, `<Media omitted>`, deleted messages, contact-addition notices,
group-creation/rename lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from app.integrations.historical_corpus.parsers.types import ChunkRecord, DocMeta


TS_RE = re.compile(
    r"^\[(\d{2})/(\d{2})/(\d{4}),\s+(\d{2}):(\d{2}):(\d{2})\]\s(.*?):\s(.*)$"
)
SKIP_PATTERNS = [
    re.compile(r"Messages and calls are end-to-end encrypted", re.I),
    re.compile(r"<Media omitted>", re.I),
    re.compile(r"This message was deleted", re.I),
    re.compile(r"is a contact$", re.I),
    re.compile(r"changed the subject", re.I),
    re.compile(r"changed this group's icon", re.I),
    re.compile(r"added you|added \+\d", re.I),
    re.compile(r"created group", re.I),
]
WINDOW_MAX_GAP = timedelta(minutes=30)
WINDOW_MAX_CHARS = 3000


@dataclass
class _Msg:
    ts: datetime
    sender: str
    body: str


def _should_skip(body: str) -> bool:
    stripped = body.strip()
    if not stripped:
        return True
    return any(p.search(stripped) for p in SKIP_PATTERNS)


def _parse_messages(path: Path) -> list[_Msg]:
    messages: list[_Msg] = []
    current: _Msg | None = None
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n").replace("\u200e", "")
            m = TS_RE.match(line)
            if m:
                if current and not _should_skip(current.body):
                    messages.append(current)
                d, mo, y, hh, mm, ss, sender, body = m.groups()
                ts = datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss))
                current = _Msg(ts=ts, sender=sender.strip(), body=body)
            elif current is not None:
                current.body += "\n" + line
    if current and not _should_skip(current.body):
        messages.append(current)
    return messages


def parse(path: Path, chat_name: str | None = None) -> tuple[DocMeta, list[ChunkRecord]]:
    chat_name = chat_name or path.parent.name or path.stem
    messages = _parse_messages(path)
    if not messages:
        meta = DocMeta(title=chat_name, source_type="whatsapp_txt")
        return meta, []

    participants = sorted({m.sender for m in messages})
    meta = DocMeta(
        title=chat_name,
        source_type="whatsapp_txt",
        participants=participants,
        document_date=messages[0].ts.date(),
        metadata={
            "message_count": len(messages),
            "last_message_date": messages[-1].ts.date().isoformat(),
        },
    )

    chunks: list[ChunkRecord] = []
    window: list[_Msg] = []

    def flush() -> None:
        if not window:
            return
        body = "\n".join(
            f"[{m.ts.isoformat(timespec='minutes')}] {m.sender}: {m.body.strip()}"
            for m in window
        )
        chunks.append(ChunkRecord(
            chunk_type="conversation_window",
            chunk_text=body,
            breadcrumb=f"WhatsApp › {chat_name} › {window[0].ts.date()}",
            metadata={
                "start": window[0].ts.isoformat(),
                "end": window[-1].ts.isoformat(),
                "participants": sorted({m.sender for m in window}),
                "message_count": len(window),
            },
        ))

    for msg in messages:
        if not window:
            window = [msg]
            continue
        gap = msg.ts - window[-1].ts
        total_chars = sum(len(m.body) for m in window)
        if gap > WINDOW_MAX_GAP or total_chars > WINDOW_MAX_CHARS:
            flush()
            window = [msg]
        else:
            window.append(msg)
    flush()

    # Prepend a chat-level header chunk for broad topical queries.
    header_body = (
        f"WhatsApp conversation: {chat_name}\n"
        f"Participants: {', '.join(participants)}\n"
        f"Date range: {messages[0].ts.date()} → {messages[-1].ts.date()}\n"
        f"Total messages: {len(messages)}\n"
        f"Windows: {len(chunks)}"
    )
    chunks.insert(0, ChunkRecord(
        chunk_type="conversation_header",
        chunk_text=header_body,
        breadcrumb=f"WhatsApp › {chat_name}",
        metadata={
            "start": messages[0].ts.isoformat(),
            "end": messages[-1].ts.isoformat(),
            "participants": participants,
            "message_count": len(messages),
            "window_count": len(chunks),
        },
    ))
    return meta, chunks
