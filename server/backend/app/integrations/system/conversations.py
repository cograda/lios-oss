"""conversations_since — deterministic cross-source conversation grouping (lios#192).

The gap this closes: `whatsapp_thread` returns one *entire chat's* history by
`chat_id`, and `gmail_thread` returns one *already-known* `thread_id`'s
messages — neither groups "what conversations happened across every chat/
thread in the window" the way a morning kickoff needs. Two concrete misreads
from #192: a WhatsApp group question that was already answered later in the
same thread surfaced as ~10 separate "new" candidates when messages were
judged one at a time; a passing evening update, read as the last message of
an ordinary thread rather than in context, was elevated to something that
needed a check-in.

See `vault/Projects/lios/Plans/Daily Kickoff — Phase contract (design,
2026-09-10).md` §4 and §8 — this module is that design's "server-side and
deterministic" grouping call, step 1 of the build order in §11. It is
**deliberately not a judgement pass**: no LLM call, no embedding similarity,
just chat/thread identity plus a time-gap window — the same kind of
segmentation `whatsapp/sync.py` already uses for its own conversation-window
embedding chunks, applied here to raw messages instead of embedding input.

## Grouping rule

- **Gmail** — one group per `thread_id` within the window. Gmail's own
  threading is free and correct; no burst logic needed.
- **WhatsApp** — one group per `chat_id` per *burst*, where a burst is a run
  of messages in that chat separated by no more than
  `system.conversations_burst_gap_minutes` (default 360 — six hours,
  configurable, not a constant; see manifest.py). A long-running group chat
  that goes quiet overnight and picks up again the next morning is two
  conversations, not one.

Both sources are then sorted together by last-message time, descending, and
capped at `limit`.

## What is excluded, and why

- **A group where every message is from the caller** never surfaces — there
  is nothing to review; it's the caller talking to themselves in a thread
  they already know about (as opposed to an actual self-chat/notes channel,
  which is excluded structurally, below).
- **WhatsApp's self-chat/notes channel** — the jid `whatsapp.self_chat_map()`
  already treats as a personal-notes destination (see `inbox`'s
  `inbox_route_whatsapp_notes`, which routes exactly this jid's own outgoing
  messages into the inbox queue) is excluded outright. Grouping someone's own
  notes-to-self as a "conversation" would be noise, not signal.

## Scoping

Both sources are read through `mail.query`/`whatsapp.query`'s `recent()`
handlers, which run inside the caller's own request context
(`current_user_id()`) — the same scoping every other caller of those facades
relies on (see `tasks/intake.py`). This module never widens that: pass
`user_id` explicitly to `conversations_since()` only to select *which*
caller's data a request-scoped call reads; it is never used to read a
second user's data from within one call.

## `mentions_me`

Not computed. A reliable "does this message address me" signal needs either
alias data (a person's known names/nicknames, which lives in the vault, not
this database) or fields the WhatsApp facade's public message dict
deliberately doesn't expose (`reply_to_id` — widening that dict is its own
change, not a side effect of this tool; see `google_mail.facade.labels_for`'s
docstring for the same boundary applied to mail). Each group carries
`mentions_me: None` rather than a guess.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.plugin.capabilities import get_capability
from app.plugin.config_store import plugin_config
from app.tools.base import parse_iso_date

logger = logging.getLogger(__name__)

ALL_SOURCES = ("whatsapp", "mail")
DEFAULT_LIMIT = 30
MAX_LIMIT = 100
MAX_MESSAGES_PER_GROUP = 20
_TEXT_CAP_LAST_FIRST = 200
_TEXT_CAP_MESSAGE = 300
_PAGE_SIZE = {"whatsapp": 100, "mail": 500}
_FETCH_CAP = 3000  # hard ceiling per source per call — a runaway window must not hang the tool
DEFAULT_BURST_GAP_MINUTES = 360
MAX_SINCE_DAYS = 30

_QUESTION_WORDS_RE = re.compile(
    r"\b(who|what|when|where|why|how|can|could|would|should|will|is|are|do|does|did)\b",
    re.IGNORECASE,
)


def _burst_gap_minutes() -> int:
    """Config-driven (`system.conversations_burst_gap_minutes`), same
    fallback-on-bad-value pattern as `tools._daemon_silent_minutes()`."""
    value = getattr(plugin_config("system"), "conversations_burst_gap_minutes", None)
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(
            "ignoring bad system.conversations_burst_gap_minutes %r; using default %d",
            value, DEFAULT_BURST_GAP_MINUTES,
        )
        return DEFAULT_BURST_GAP_MINUTES


def _has_question(text: str | None) -> bool:
    """Deterministic: ends in '?', or contains both '?' and a question word."""
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith("?"):
        return True
    return "?" in t and bool(_QUESTION_WORDS_RE.search(t))


def _trim(text: str | None, n: int) -> str:
    t = (text or "").strip()
    if len(t) <= n:
        return t
    return t[: max(n - 1, 0)].rstrip() + "…"


def _dedupe(names: list[str | None]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def _paged_fetch(method, session: Session, since: datetime, until: datetime, cap: int, page_size: int) -> list[dict]:
    """Walk a facade `recent(session, args)` handler backwards from `until`
    to `since`, newest-first pages, accumulating until the window is
    exhausted or `cap` is hit.

    Deliberately duplicated rather than imported from `tasks/intake.py`'s
    identically-shaped `_paged_fetch` — `system` may not reach into another
    integration's internals (`test_kernel_import_guard.py`/
    `test_capability_boundaries.py`), and `tasks/intake.py`'s copy isn't a
    declared facade method either, so there is no legal import path to it.
    """
    collected: list[dict] = []
    cur_before = until
    while len(collected) < cap:
        args = {"after": since.isoformat(), "before": cur_before.isoformat(), "limit": page_size}
        try:
            rows = json.loads(method(session, args))
        except Exception as e:  # noqa: BLE001 — one bad page must not fail the whole tool
            logger.warning("conversations_since: paged fetch failed: %s", e)
            break
        if not isinstance(rows, list) or not rows:
            break
        collected.extend(rows)
        if len(rows) < page_size:
            break
        oldest = parse_iso_date(rows[-1].get("date"))
        if oldest is None or oldest <= since:
            break
        cur_before = oldest - timedelta(microseconds=1)
    return collected[:cap]


# ─── WhatsApp: burst grouping within a chat ────────────────────────────────


def _whatsapp_groups(session: Session, since: datetime, until: datetime, gap_minutes: int) -> list[dict]:
    facade = get_capability("whatsapp.query")

    from app.auth.context import current_user_id

    uid = current_user_id()
    self_map = facade.self_chat_map()
    self_jid = self_map.get(uid)

    rows = _paged_fetch(facade.recent, session, since, until, _FETCH_CAP, _PAGE_SIZE["whatsapp"])
    if self_jid:
        rows = [r for r in rows if r.get("chat_id") != self_jid]
    if not rows:
        return []

    contact_names = facade.contact_names(session, user_id=uid)

    by_chat: dict[str, list[dict]] = {}
    for r in rows:
        ts = parse_iso_date(r.get("date"))
        if ts is None:
            continue
        by_chat.setdefault(r["chat_id"], []).append({**r, "_ts": ts})

    gap = timedelta(minutes=gap_minutes)
    groups: list[dict] = []
    for chat_id, msgs in by_chat.items():
        msgs.sort(key=lambda m: m["_ts"])
        burst: list[dict] = []
        for m in msgs:
            if burst and (m["_ts"] - burst[-1]["_ts"]) > gap:
                g = _build_whatsapp_group(chat_id, burst, contact_names)
                if g is not None:
                    groups.append(g)
                burst = []
            burst.append(m)
        if burst:
            g = _build_whatsapp_group(chat_id, burst, contact_names)
            if g is not None:
                groups.append(g)
    return groups


def _build_whatsapp_group(chat_id: str, burst: list[dict], contact_names: dict[str, str]) -> dict | None:
    if all(m.get("is_from_me") for m in burst):
        return None  # nothing to review — the caller talking to themselves

    title = contact_names.get(chat_id) or burst[-1].get("chat_name") or chat_id
    participants = _dedupe([m.get("sender") for m in burst])
    inbound_texts = [m.get("body") or m.get("caption") or "" for m in burst if not m.get("is_from_me")]

    def _one(m: dict) -> dict:
        text = m.get("body") or m.get("caption") or ""
        return {"sender": m.get("sender"), "text": _trim(text, _TEXT_CAP_LAST_FIRST)}

    messages = burst[-MAX_MESSAGES_PER_GROUP:]  # oldest-first within a burst already
    return {
        "source": "whatsapp",
        "key": chat_id,
        "title": title,
        "participants": participants,
        "span": {"first": burst[0]["date"], "last": burst[-1]["date"]},
        "message_count": len(burst),
        "from_me_count": sum(1 for m in burst if m.get("is_from_me")),
        "first_message": _one(burst[0]),
        "last_message": _one(burst[-1]),
        "has_question": any(_has_question(t) for t in inbound_texts),
        "mentions_me": None,
        "messages": [
            {
                "ts": m.get("date"),
                "sender": m.get("sender"),
                "from_me": bool(m.get("is_from_me")),
                "text": _trim(m.get("body") or m.get("caption") or "", _TEXT_CAP_MESSAGE),
            }
            for m in messages
        ],
    }


# ─── Gmail: thread grouping (native, free) ─────────────────────────────────


def _gmail_own_addresses(session: Session, user_id: int) -> set[str]:
    from app.models.tokens import OAuthToken

    rows = (
        session.query(OAuthToken.account_email)
        .filter(OAuthToken.provider == "google", OAuthToken.user_id == user_id)
        .all()
    )
    return {email.lower() for (email,) in rows if email}


def _addr(raw: str | None) -> str:
    from email.utils import parseaddr

    _, addr = parseaddr(raw or "")
    return addr.lower()


def _display_name(raw: str | None) -> str | None:
    from email.utils import parseaddr

    name, addr = parseaddr(raw or "")
    return name or (addr or None)


def _gmail_groups(session: Session, since: datetime, until: datetime, user_id: int) -> list[dict]:
    facade = get_capability("mail.query")
    rows = _paged_fetch(facade.recent, session, since, until, _FETCH_CAP, _PAGE_SIZE["mail"])
    if not rows:
        return []

    own = _gmail_own_addresses(session, user_id)

    by_thread: dict[str, list[dict]] = {}
    for r in rows:
        ts = parse_iso_date(r.get("date"))
        if ts is None:
            continue
        tid = r.get("thread_id")
        if not tid:
            continue
        r = {**r, "_ts": ts, "_from_me": _addr(r.get("from")) in own}
        by_thread.setdefault(tid, []).append(r)

    groups: list[dict] = []
    for thread_id, msgs in by_thread.items():
        msgs.sort(key=lambda m: m["_ts"])
        g = _build_gmail_group(thread_id, msgs)
        if g is not None:
            groups.append(g)
    return groups


def _build_gmail_group(thread_id: str, msgs: list[dict]) -> dict | None:
    if all(m["_from_me"] for m in msgs):
        return None

    subjects = [m.get("subject") for m in msgs if m.get("subject")]
    title = subjects[-1] if subjects else thread_id
    participants = _dedupe([_display_name(m.get("from")) for m in msgs])
    inbound_texts = [m.get("snippet") or "" for m in msgs if not m["_from_me"]]

    def _one(m: dict) -> dict:
        return {"sender": _display_name(m.get("from")), "text": _trim(m.get("snippet"), _TEXT_CAP_LAST_FIRST)}

    messages = msgs[-MAX_MESSAGES_PER_GROUP:]
    return {
        "source": "mail",
        "key": thread_id,
        "title": title,
        "participants": participants,
        "span": {"first": msgs[0]["date"], "last": msgs[-1]["date"]},
        "message_count": len(msgs),
        "from_me_count": sum(1 for m in msgs if m["_from_me"]),
        "first_message": _one(msgs[0]),
        "last_message": _one(msgs[-1]),
        "has_question": any(_has_question(t) for t in inbound_texts),
        "mentions_me": None,
        "messages": [
            {
                "ts": m.get("date"),
                "sender": _display_name(m.get("from")),
                "from_me": m["_from_me"],
                "text": _trim(m.get("snippet"), _TEXT_CAP_MESSAGE),
            }
            for m in messages
        ],
    }


# ─── entry point ────────────────────────────────────────────────────────────


def conversations_since(
    session: Session,
    user_id: int,
    since: datetime,
    until: datetime | None = None,
    *,
    sources: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Grouped WhatsApp/Gmail conversations for `user_id` since `since`.

    Deterministic — chat/thread identity plus a time-gap window, no
    embedding call, no LLM call. Runs inside the caller's own request scope:
    `mail.query`/`whatsapp.query`'s `recent()` handlers read
    `current_user_id()` themselves (via the DSL's auto-scoping), so this
    function must be called with that context already bound to `user_id`
    (see `tools.py::handle_conversations_since`, which does that via
    `use_user`/the MCP dispatch chokepoint the same as every other tool).

    Returns `{"since", "until", "sources", "burst_gap_minutes", "counts",
    "groups"}`. `groups` is sorted by `span.last` descending and capped at
    `limit`. See the module docstring for the grouping rule and exclusions.
    """
    until = until or datetime.now(timezone.utc)
    srcs = sources or list(ALL_SOURCES)
    unknown = sorted(set(srcs) - set(ALL_SOURCES))
    if unknown:
        raise ValueError(f"unknown sources: {unknown}")
    limit = min(max(int(limit), 1), MAX_LIMIT)

    gap_minutes = _burst_gap_minutes()

    groups: list[dict] = []
    if "whatsapp" in srcs:
        groups.extend(_whatsapp_groups(session, since, until, gap_minutes))
    if "mail" in srcs:
        groups.extend(_gmail_groups(session, since, until, user_id))

    groups.sort(key=lambda g: g["span"]["last"] or "", reverse=True)
    total_before_limit = len(groups)
    groups = groups[:limit]

    counts = {"whatsapp": 0, "mail": 0}
    for g in groups:
        counts[g["source"]] += 1

    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "sources": list(srcs),
        "burst_gap_minutes": gap_minutes,
        "counts": {**counts, "total": len(groups), "total_before_limit": total_before_limit},
        "groups": groups,
    }
