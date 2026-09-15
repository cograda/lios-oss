"""Per-source text cleaning, applied before embedding.

An embedding model spends its (fixed, 512- or 8192-token) budget on whatever
you hand it. Comar hands it a lot of scaffolding: RFC headers, HTML furniture,
`[timestamp] sender:` prefixes on every chat line, quoted reply chains, and
unsubscribe footers. That text is *identical across thousands of documents*, so
it contributes no discriminative signal while consuming both context and
similarity mass.

This is measurable in the existing index, not a theory. Topic modelling over
the current vectors (2026-07-31) produced topics labelled `span / div / class`,
`zwnj / zwnj zwnj`, and `style / width / table / role presentation` — the model
had clustered documents by *markup*. Separately, WhatsApp windows sit at 0.79
mean pairwise cosine against a 0.60 corpus baseline, a tight cone caused by the
chat banner and per-line timestamps being ~a third of the tokens in a short
window.

Two complementary strategies:

* **Rule-based, per source** (`clean`) — structure we know is structure.
* **Data-driven** (`BoilerplateFilter`) — lines that recur across many
  documents are boilerplate by definition, whatever they look like. This
  catches per-sender signatures, legal footers and disclaimers that no regex
  anticipates, and adapts as the corpus changes.

`CLEANER_VERSION` must be bumped on any behavioural change: vectors are only
comparable to other vectors produced by the same cleaner, so it is stored
alongside them.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

CLEANER_VERSION = 2

# --- shared -----------------------------------------------------------------
# Captures the host so the path/query can be dropped while the domain survives.
# A bare `https://\S+` deleted outright made "the link to the planning portal"
# unfindable; the host is 1-3 tokens of real topical signal, the path and query
# are 10-20 tokens of none.
RE_URL = re.compile(r"https?://([^\s/?#]+)\S*|www\.([^\s/?#]+)\S*")


def _urls_to_domains(text: str) -> str:
    def repl(m: re.Match) -> str:
        host = (m.group(1) or m.group(2) or "").removeprefix("www.")
        return f" {host} " if host else " "

    return RE_URL.sub(repl, text)
RE_MAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b")
# Zero-width and invisible padding — marketing email uses runs of these as
# layout spacers, and they tokenise into hundreds of junk tokens.
RE_INVISIBLE = re.compile("[​-‏⁠﻿͏­  ]")
RE_WS = re.compile(r"[ \t]+")
RE_BLANKS = re.compile(r"\n{3,}")

# --- email ------------------------------------------------------------------
RE_EMAIL_HEADER = re.compile(r"^(From|To|Cc|Bcc|Subject|Date|Sent|Reply-To):\s*(.*)$", re.M)
RE_HTML_BLOCK = re.compile(r"<(style|script|head)[^>]*>.*?</\1>", re.S | re.I)
RE_HTML_TAG = re.compile(r"<[^>]{1,400}>")
RE_HTML_ENTITY = re.compile(r"&(?:#x?[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]{1,10});")
RE_CSS_PROP = re.compile(r"[-a-z]+\s*:\s*[^;{}\n]{1,80}[;}]", re.I)
# Reply chains: everything from "On <date>, <person> wrote:" onward is a copy
# of a message already embedded under its own id.
RE_REPLY_INTRO = re.compile(
    r"^\s*(-{2,}\s*Original Message\s*-{2,}|_{5,}|From:.{0,200}Sent:.{0,200})",
    re.M | re.I)
# "On <date> <person> wrote:" is NOT anchored to line start: HTML-to-text
# conversion routinely collapses the intro into the middle of a line, which an
# `^`-anchored pattern misses (457 such emails in the current corpus). The
# `\d` requirement keeps it from firing on prose like "on what he wrote:".
# DOTALL, not [^\n]: the sender address inside the intro is frequently soft-
# wrapped ("<\r\nsales@example.com> wrote:"), which a newline-free pattern
# misses. Length bounds plus the required digit keep it from running away.
RE_REPLY_ON_WROTE = re.compile(r"\bOn\b.{0,140}?\d.{0,140}?\bwrote:", re.I | re.S)
RE_QUOTED = re.compile(r"^\s*>.*$", re.M)
RE_SIGNATURE = re.compile(r"^--\s*$", re.M)
RE_FOOTER = re.compile(
    r"^.{0,200}\b(unsubscribe|manage your (email )?preferences|view (this|in) (email|browser)|"
    r"you (are )?receiv(e|ing) this (email|message)|privacy policy|all rights reserved|"
    r"do not reply to this|this email was sent to|update your preferences|"
    r"opt out|sent from my i(phone|pad)|confidentiality notice)\b.{0,200}$",
    re.M | re.I)
RE_BASE64ISH = re.compile(r"\b[A-Za-z0-9+/]{60,}={0,2}\b")
RE_CID = re.compile(r"\[cid:[^\]]*\]", re.I)

# --- chat -------------------------------------------------------------------
# `_format_segment` emits "[WhatsApp chat: <label>]" for 1:1 and
# "[WhatsApp group: <label>]" for groups. Matching only `chat:` left every
# group window's banner in place — 2,263 rows in production, each carrying a
# raw JID like `353863042412-1621924270@g.us` when the group has no name.
# That identifier repeats in every chunk of that chat, which is the documented
# cause of WhatsApp windows sitting in a 0.79-cosine cone: the model clusters
# by conversation rather than by subject. The chat is still recoverable —
# `chat_id`/`chat_name` are in the chunk's metadata, where an identifier belongs.
# `note to self` has no `: <name>` part — it names a *kind* of chat, not a
# counterparty — but it is boilerplate for exactly the same reason and must be
# stripped for exactly the same reason. Missing it here does more than leave
# noise: the banner then survives into RE_CHAT_PREFIX's window below, which
# matches across the newline and eats the real message's timestamp instead,
# leaving text that starts mid-timestamp ("22] me: …").
RE_WA_BANNER = re.compile(
    r"^\[WhatsApp (?:(?:chat|group):[^\]]*|note to self)\]\s*$", re.M
)
# "[2025-10-03 20:32] me:" / "[2026-01-19T14:47] Alex:" — the speaker label is
# dropped along with the timestamp: who said it is metadata, and leaving names
# in makes the model cluster by participant rather than subject.
RE_CHAT_PREFIX = re.compile(r"^\[[^\]]{4,40}\]\s*[^:\n]{0,40}:\s*", re.M)
RE_WA_SYSTEM = re.compile(
    r"^.{0,80}\b(<Media omitted>|This message was deleted|You deleted this message|"
    r"Missed (voice|video) call|Messages and calls are end-to-end encrypted|"
    r"changed the subject|changed this group's icon|joined using this group's invite link|"
    r"image omitted|video omitted|audio omitted|sticker omitted|GIF omitted|"
    r"document omitted|null)\b.{0,80}$", re.M | re.I)

# --- vault ------------------------------------------------------------------
RE_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.S)
RE_MD_SYNTAX = re.compile(r"[#*_`~>|]+")
RE_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
RE_MDLINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
RE_CHECKBOX = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*", re.M)
# Tags keep their WORD and lose their punctuation: `#renovation` -> `renovation`.
# Deleting them outright (CLEANER_VERSION 1) threw away the most deliberately
# chosen topical label in the whole vault — these are curated by hand, unlike
# a hashtag in chat. `/` becomes a space so `#person/finn` yields two words.
RE_TAG = re.compile(r"(?<!\w)#([\w/-]+)")
RE_EMOJI_PRIORITY = re.compile("[\U0001F300-\U0001FAFF←-⇿☀-➿]")

# --- documents (PDF / DOCX extraction artefacts) -----------------------------
# A page number on its own line, with or without a "Page" prefix or an "of N".
RE_PAGE_NUMBER = re.compile(r"^\s*(?:page\s+)?\d{1,4}(?:\s*(?:/|of)\s*\d{1,4})?\s*$", re.M | re.I)
# PDF text extraction breaks words across line ends: "regist-\nration".
RE_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")


def _collapse(text: str) -> str:
    text = RE_INVISIBLE.sub(" ", text)
    text = RE_WS.sub(" ", text)
    # Strip lines FIRST: removing invisibles/tags leaves whitespace-only lines
    # that aren't yet empty, so collapsing blank runs before stripping misses
    # them and the blank runs survive into the embedder.
    lines = [line.strip() for line in text.split("\n")]
    # Drop lines that are pure punctuation residue left by stripped links/tags
    # (e.g. "( )", "|", "---", "*").
    lines = ["" if l and not any(c.isalnum() for c in l) else l for l in lines]
    return RE_BLANKS.sub("\n\n", "\n".join(lines)).strip()


def clean_email(text: str) -> str:
    # Keep the Subject line's words — a subject is the single most topical line
    # in a mail — but drop the rest of the envelope.
    #
    # ONLY the first. A quoted thread carries one `Subject:` per quoted reply,
    # and joining them all repeated the same title six times at the head of a
    # real 20 KB solicitor thread — 29% of the cleaned chunk was its own title,
    # which is worse than not hoisting at all. The topmost header is the
    # message's own; the rest belong to replies this function truncates below.
    first_subject = next(
        (m.group(2).strip() for m in RE_EMAIL_HEADER.finditer(text)
         if m.group(1).lower() == "subject" and m.group(2).strip()),
        "",
    )
    subjects = first_subject
    text = RE_EMAIL_HEADER.sub("", text)
    # Outlook inline-image references: `[cid:image001.png@01DC2323.D1198390]`.
    # The address-stripping below leaves `[cid: ]` behind, which survives the
    # punctuation-residue filter because "cid" is alphanumeric.
    text = RE_CID.sub(" ", text)
    text = RE_HTML_BLOCK.sub(" ", text)
    text = RE_HTML_TAG.sub(" ", text)
    text = RE_CSS_PROP.sub(" ", text)
    text = RE_HTML_ENTITY.sub(" ", text)
    # Truncate at the first reply-chain marker rather than deleting the marker:
    # everything after it is a copy of a message already embedded under its own
    # id, so leaving it in embeds the same conversation once per reply.
    cuts = [m.start() for m in (RE_REPLY_INTRO.search(text),
                                RE_REPLY_ON_WROTE.search(text)) if m]
    if cuts:
        text = text[: min(cuts)]
    text = RE_QUOTED.sub("", text)
    sig = RE_SIGNATURE.search(text)
    if sig:
        text = text[: sig.start()]
    text = RE_FOOTER.sub("", text)
    text = RE_BASE64ISH.sub(" ", text)
    text = _urls_to_domains(text)
    text = RE_MAIL.sub(" ", text)
    return _collapse(f"{subjects}\n\n{text}" if subjects else text)


def clean_chat(text: str) -> str:
    text = RE_WA_BANNER.sub("", text)
    text = RE_CHAT_PREFIX.sub("", text)
    text = RE_WA_SYSTEM.sub("", text)
    text = _urls_to_domains(text)
    return _collapse(text)


def clean_markdown(text: str) -> str:
    """Markdown prose — Claude conversation transcripts, voice-memo notes.

    Deliberately keeps code blocks: in these transcripts the code or the CSV
    line very often IS the substance ("here's the roof overhang line item").
    Only the fence characters go, via RE_MD_SYNTAX.
    """
    text = RE_CHECKBOX.sub("", text)
    text = RE_MDLINK.sub(r"\1", text)
    text = RE_TAG.sub(lambda m: " " + m.group(1).replace("/", " "), text)
    text = RE_EMOJI_PRIORITY.sub(" ", text)
    text = RE_MD_SYNTAX.sub(" ", text)
    text = _urls_to_domains(text)
    return _collapse(text)


def clean_vault(text: str) -> str:
    text = RE_FRONTMATTER.sub("", text)
    # Line 1 of a vault chunk is the note path, which repeats the folder
    # hierarchy in every chunk of that file.
    lines = text.split("\n")
    if lines and lines[0].endswith(".md"):
        text = "\n".join(lines[1:])
    text = RE_WIKILINK.sub(lambda m: m.group(2) or m.group(1), text)
    return clean_markdown(text)


def clean_document(text: str) -> str:
    """Extracted PDF/DOCX text.

    Handles only the artefacts of extraction itself: form feeds, words
    hyphenated across a line break, and standalone page numbers.

    It does NOT remove letterheads or running headers ("Event Pack" repeating
    on every page of the sample that motivated this). Those look different in
    every document, so no regex anticipates them — that is `BoilerplateFilter`'s
    job, and it needs a corpus-wide `fit()` it cannot get from this
    one-item-at-a-time chokepoint. Wired up in the Phase 4 re-enqueue traversal.
    """
    text = text.replace("\f", "\n")
    text = RE_HYPHEN_BREAK.sub(r"\1\2", text)
    text = RE_PAGE_NUMBER.sub("", text)
    text = _urls_to_domains(text)
    return _collapse(text)


def clean_generic(text: str) -> str:
    return _collapse(_urls_to_domains(text))


CLEANERS = {
    "email": clean_email,
    "whatsapp": clean_chat,
    "vault": clean_vault,
    "coffee": clean_generic,
    # NB no "historical_corpus" entry — it routes on chunk_type instead, see
    # CORPUS_CHUNK_CLEANERS and clean().
}

# historical_corpus is not one shape of document, so one cleaner cannot serve it.
# CLEANER_VERSION 1 routed the whole source to `clean_chat` on a comment reading
# "largely chat exports and transcripts". That was right about provenance and
# wrong about shape: the source holds 16 distinct chunk types, and clean_chat's
# four rules (WhatsApp banner, `[timestamp] speaker:` prefix, WA system message,
# URL) match almost none of them. The concrete damage was 620 `email_message`
# chunks averaging 8,181 chars keeping their entire RFC envelope and quoted
# reply chains, and 2,196 `pdf_page_chunk`s keeping their extraction artefacts.
#
# Route on `chunk_type` from the chunk's own metadata — the corpus already
# records it — rather than on the source string.
CORPUS_CHUNK_CLEANERS = {
    # Real email, the same shape google_mail produces.
    "email_message": clean_email,
    "email_thread": clean_email,
    "email_thread_summary": clean_email,
    # Claude conversation transcripts: markdown prose with `Speaker:` turns and
    # no bracketed timestamps, so clean_chat's prefix rule never fired on them.
    "claude_conversation": clean_markdown,
    "claude_conversation_turn": clean_markdown,
    "claude_conversation_summary": clean_markdown,
    # Genuine chat exports — the shape clean_chat was actually written for.
    "conversation_window": clean_chat,
    "conversation_header": clean_chat,
    # Extracted documents.
    "pdf_page_chunk": clean_document,
    "pdf_low_yield": clean_document,
    "docx_chunk": clean_document,
    "subsection": clean_document,
    # Transcribed speech — prose, sometimes lightly marked up.
    "voice_memo": clean_markdown,
    "voice_memo_summary": clean_markdown,
    # Spreadsheet rows and short generated summaries: already dense (line_item
    # averages 171 chars), so anything beyond URL handling risks removing
    # content rather than furniture.
    "line_item": clean_generic,
    "trade_summary": clean_generic,
}


def clean(text: str, source: str, metadata: dict | None = None) -> str:
    """Clean `text` according to its source, and its chunk type where it has one.

    `metadata` is the chunk's own metadata dict (parsed from `metadata_json`).
    Only `historical_corpus` uses it today — that source mixes email, chat,
    Claude transcripts, PDFs and spreadsheet rows under one name, so its
    `chunk_type` is the real routing key. An unrecognised chunk type falls back
    to `clean_generic` rather than to a guess: a new corpus chunk shape getting
    URL-stripping only is a much smaller error than getting a cleaner written
    for some other shape.
    """
    if not text:
        return ""
    if source == "historical_corpus":
        chunk_type = (metadata or {}).get("chunk_type")
        return CORPUS_CHUNK_CLEANERS.get(chunk_type, clean_generic)(text)
    return CLEANERS.get(source, clean_generic)(text)


@dataclass
class BoilerplateFilter:
    """Strips lines that recur across many documents.

    Rule-based cleaning only removes structure someone anticipated. Sender
    signatures, legal disclaimers and per-service footers are boilerplate too,
    but every one looks different. The general property is *repetition*: a line
    appearing in hundreds of otherwise-unrelated documents carries no
    information distinguishing them.

    Fit on a corpus, then apply. `min_docs` is deliberately an absolute count
    rather than a fraction — a footer shared by 300 of 13,000 emails is still
    boilerplate even at 2%. Short lines are exempt because "Thanks", "Yes" and
    "ok" recur constantly and *are* the content in chat.
    """

    min_docs: int = 25
    min_len: int = 25
    counts: Counter = field(default_factory=Counter)
    blocked: set[str] = field(default_factory=set)

    @staticmethod
    def _key(line: str) -> str:
        # Normalise digits so "Order #12345" and "Order #67890" collapse to one
        # pattern — templated lines differ only in their variable parts.
        return re.sub(r"\d+", "#", line.strip().lower())

    def fit(self, docs: list[str]) -> "BoilerplateFilter":
        for doc in docs:
            # Per document, not per occurrence: a line repeated 50 times inside
            # one document is that document's own noise, not corpus boilerplate.
            seen = {self._key(l) for l in doc.split("\n") if len(l.strip()) >= self.min_len}
            self.counts.update(seen)
        self.blocked = {k for k, n in self.counts.items() if n >= self.min_docs}
        return self

    def apply(self, text: str, protect_first_line: bool = False) -> str:
        """Drop blocked lines.

        `protect_first_line` exempts line 1, for text whose first line is a
        hoisted email subject. Measured: `re: aib mortgage application` recurs
        across 68 chunks of one thread and so blocks like any footer — but a
        subject is the highest-signal line in a mail (dropping subjects costs
        -34.4% on 10-NN agreement), and its recurrence is what a thread *is*,
        not evidence that it is furniture.

        Off by default because for an extracted PDF page the opposite holds:
        line 1 is frequently the letterhead, which is the main thing worth
        removing.
        """
        if not self.blocked:
            return text
        lines = text.split("\n")
        head, rest = (lines[:1], lines[1:]) if protect_first_line else ([], lines)
        kept = [
            l for l in rest
            if len(l.strip()) < self.min_len or self._key(l) not in self.blocked
        ]
        return _collapse("\n".join(head + kept))

    @property
    def n_blocked(self) -> int:
        return len(self.blocked)
