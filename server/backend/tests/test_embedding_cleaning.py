"""Per-source / per-chunk-type text cleaning (unit tier).

These are the rules that decide what an embedding model actually sees. They had
no tests at all until now, which is not a coincidence: `cleaning.py` was
imported by nothing between being written (2026-07-31) and being wired into the
enqueue chokepoint (2026-08-05), so nothing exercised it and its
`historical_corpus` misrouting went unnoticed for a week.
"""

import pytest

from app.integrations.embedding import cleaning
from app.integrations.embedding.cleaning import (
    CLEANER_VERSION,
    CORPUS_CHUNK_CLEANERS,
    clean,
    clean_document,
    clean_email,
)

# The chunk types actually present in the production corpus on 2026-08-06, with
# their row counts. Pinned so that deleting a mapping is a test failure rather
# than a silent downgrade to clean_generic — an unmapped type still *works*,
# which is exactly why its loss would go unnoticed.
PRODUCTION_CHUNK_TYPES = {
    "claude_conversation_turn": 9142,
    "line_item": 3338,
    "claude_conversation": 2565,
    "pdf_page_chunk": 2196,
    "claude_conversation_summary": 1735,
    "conversation_window": 813,
    "email_message": 620,
    "subsection": 608,
    "docx_chunk": 530,
    "email_thread": 413,
    "voice_memo": 152,
    "email_thread_summary": 149,
    "pdf_low_yield": 21,
    "trade_summary": 16,
    "conversation_header": 11,
}

CORPUS_EMAIL = """Subject: Green House - Malahide

From: Sam Rivers <sam@example.com>
To: Declan Scullion <dscullion@scullion.ie>
Date: 2024-11-29T08:21:15+00:00

Hi Declan, the portal is at https://planning.wicklowcoco.ie/online/app?id=99.

On Fri 28 Nov 2024, Declan wrote:
> a reply already embedded under its own id
"""


# ---------------------------------------------------------------------------
# Routing — the actual defect
# ---------------------------------------------------------------------------

def test_corpus_email_is_cleaned_as_email_not_as_chat():
    """The bug this phase fixes.

    `historical_corpus` routed wholesale to `clean_chat`, whose four rules
    (WhatsApp banner, `[timestamp] speaker:`, WA system message, URL) match
    nothing in an email. 620 chunks averaging 8,181 chars kept their entire RFC
    envelope and quoted reply chain.
    """
    out = clean(CORPUS_EMAIL, "historical_corpus", {"chunk_type": "email_message"})

    assert "Green House - Malahide" in out       # subject hoisted
    assert "sam@example.com" not in out      # envelope gone
    assert "To:" not in out and "Date:" not in out
    assert "already embedded under its own id" not in out  # reply chain truncated
    assert out == clean(CORPUS_EMAIL, "email")     # same shape, same treatment


def test_corpus_routing_is_by_chunk_type_not_source():
    """Two chunks of the same source get different cleaners."""
    doc = "Report\n\n7\n"
    assert clean(doc, "historical_corpus", {"chunk_type": "pdf_page_chunk"}) == "Report"
    # line_item is 171 chars on average — dense already, so it is left alone.
    row = "Structural Elements,Wall & Ceiling,Roof overhang design,,,,"
    assert clean(row, "historical_corpus", {"chunk_type": "line_item"}) == row


@pytest.mark.parametrize("chunk_type", sorted(PRODUCTION_CHUNK_TYPES))
def test_every_production_chunk_type_is_explicitly_routed(chunk_type):
    assert chunk_type in CORPUS_CHUNK_CLEANERS, (
        f"{chunk_type} ({PRODUCTION_CHUNK_TYPES[chunk_type]} rows) has no explicit "
        "cleaner — it would silently fall back to clean_generic"
    )


def test_unknown_chunk_type_falls_back_to_generic_not_to_a_guess():
    """A new corpus shape getting URL-handling only is a far smaller error than
    getting a cleaner written for some other shape — which is how this broke."""
    text = "Subject: keep me\n\nbody"
    assert clean(text, "historical_corpus", {"chunk_type": "something_new"}) == text
    assert clean(text, "historical_corpus", None) == text
    assert clean(text, "historical_corpus", {}) == text


def test_malformed_metadata_never_breaks_routing():
    from app.services.embedding import _parse_metadata

    for bad in ["not json", "[1,2,3]", '"a string"', "", None]:
        assert _parse_metadata(bad) is None


# ---------------------------------------------------------------------------
# Email envelope handling
# ---------------------------------------------------------------------------

def test_quoted_thread_hoists_its_subject_only_once():
    """Found by reading real output, not by reasoning about the code.

    A quoted thread carries one `Subject:` per reply. Joining them all put the
    same title six times at the head of a real 20 KB solicitor thread — 29% of
    the cleaned chunk was its own title, which is worse than not hoisting.
    """
    thread = (
        "Subject: Loan Offer Issued\n\nFrom: a@b.ie\n\nCall me at 3:30.\n\n"
        "Subject: Re: Loan Offer Issued\n\nFrom: c@d.ie\n\nearlier\n\n"
        "Subject: Re: Loan Offer Issued\n\nFrom: e@f.ie\n\nearliest\n"
    )
    out = clean_email(thread)
    assert out.count("Loan Offer Issued") == 1


def test_inline_image_references_are_removed_whole():
    """Stripping the address inside `[cid:image001.png@01DC.D119]` leaves
    `[cid: ]`, which survives the punctuation-residue filter because "cid"
    is alphanumeric."""
    out = clean_email("Hi\n\n[cid:image001.png@01DC2323.D1198390]\n\nBye")
    assert "cid" not in out


def test_reply_chain_is_truncated_not_merely_marked():
    """Everything after the marker is already embedded under its own id, so
    keeping it embeds the same conversation once per reply."""
    out = clean_email("New question here.\n\nOn Fri 28 Nov 2024, X wrote:\n> old content\n")
    assert "New question here." in out
    assert "old content" not in out


# ---------------------------------------------------------------------------
# WhatsApp
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["chat", "group"])
def test_whatsapp_banner_is_stripped_for_both_window_kinds(kind):
    """`_format_segment` emits `chat:` for 1:1 and `group:` for groups.

    Matching only `chat:` left every group window's banner in place — 2,263
    production rows, each carrying a raw JID when the group has no name. That
    identifier repeats in every chunk of the conversation, which is the
    documented cause of WhatsApp windows sitting in a 0.79-cosine cone: the
    model clusters by conversation instead of by subject.
    """
    text = f"[WhatsApp {kind}: 353863042412-1621924270@g.us]\nAmazing photos, thanks"
    out = clean(text, "whatsapp")
    assert out == "Amazing photos, thanks"
    assert "g.us" not in out


def test_whatsapp_timestamp_and_speaker_prefixes_are_stripped():
    text = "[WhatsApp group: Family]\n[2025-10-03 20:32] me: on my way\n[2025-10-03 20:33] Sam: grand"
    assert clean(text, "whatsapp") == "on my way\ngrand"


# ---------------------------------------------------------------------------
# URLs — keep the domain, drop the path
# ---------------------------------------------------------------------------

def test_url_keeps_domain_and_drops_path():
    """Deleting URLs outright made "the link to the planning portal"
    unfindable. The host is topical; the path and query are not."""
    out = clean("see https://planning.wicklowcoco.ie/online/app?id=99 now", "coffee")
    assert "planning.wicklowcoco.ie" in out
    assert "online" not in out and "id=99" not in out


def test_www_prefix_is_dropped_from_the_kept_domain():
    assert clean("at www.revenue.ie/en/vat", "coffee") == "at revenue.ie"


# ---------------------------------------------------------------------------
# Vault tags — keep the word, drop the punctuation
# ---------------------------------------------------------------------------

def test_vault_tags_keep_their_word():
    """These are hand-curated domain labels, unlike a hashtag in chat —
    the most deliberately chosen topical word in the note. v1 deleted them."""
    out = clean("Fix the #renovation gutter", "vault")
    assert "renovation" in out
    assert "#" not in out


def test_hierarchical_tag_splits_into_words():
    out = clean("chase #person/finn today", "vault")
    assert "person finn" in out


def test_vault_still_drops_frontmatter_path_line_and_markup():
    note = "---\ntitle: X\n---\nHousehold/Renovation/Snags.md\n# Heading\n- [ ] task\n"
    out = clean(note, "vault")
    assert "title: X" not in out
    assert "Snags.md" not in out
    assert out.strip() == "Heading\ntask"


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def test_document_rejoins_words_hyphenated_across_a_line_break():
    assert "registration" in clean_document("please regist-\nration here")


def test_document_drops_standalone_page_numbers_but_keeps_numeric_content():
    """A number alone on a line is furniture; the same number in a sentence
    is content. The rule has to be positional, not lexical."""
    out = clean_document("Intro\n\n12\n\nPage 3 of 9\n\nWe need 12 doors\n")
    assert out.split("\n") == ["Intro", "", "We need 12 doors"]


def test_document_does_not_strip_letterheads():
    """Explicitly NOT this cleaner's job — no regex anticipates them.

    Pinned so the omission reads as a decision rather than an oversight:
    repeated headers are `BoilerplateFilter`'s job, and it needs a corpus-wide
    fit() that this one-item-at-a-time chokepoint cannot provide.
    """
    out = clean_document("Event Pack\nArrival and Registration: 11.00")
    assert "Event Pack" in out


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source", ["email", "whatsapp", "vault", "coffee", "unknown"])
def test_cleaning_never_grows_text_or_returns_none(source):
    samples = ["", "plain text", "# md\n\n> quote\n", "a" * 500]
    for s in samples:
        out = clean(s, source)
        assert isinstance(out, str)
        assert len(out) <= len(s) + 2  # clean_email may prepend a hoisted subject


@pytest.mark.parametrize("cleaner", sorted(set(CORPUS_CHUNK_CLEANERS.values()), key=id))
def test_every_corpus_cleaner_handles_empty_and_whitespace(cleaner):
    assert cleaner("") == ""
    assert cleaner("   \n\n  ") == ""


def test_cleaner_version_is_ahead_of_the_uncleaned_era():
    """Bumped to 2 with this phase's routing/tag/URL changes.

    The version is stored per row (`embeddings.cleaner_version`), so it must
    move whenever output changes — otherwise a half-rewritten corpus is
    indistinguishable from a consistent one, which is exactly the state this
    phase found itself cleaning up after.
    """
    assert CLEANER_VERSION >= 2
    assert cleaning.CLEANER_VERSION == CLEANER_VERSION


def test_chunk_cap_admits_the_most_capable_embedders_context():
    """523 production corpus chunks exceeded the old 8,000 cap (longest
    138,809) and lost everything past it. 30,000 is gemini-embedding-2's
    ~8k-token ceiling; bge-small self-truncates at 512 tokens regardless.

    Still a mitigation, not a fix — a 138,809-char chunk is an upstream
    chunking failure and should be ~17 chunks with their own vectors.
    """
    from app.plugin.embedding_provider import GeminiEmbeddingProvider
    from app.services.embedding import MAX_CHUNK_CHARS

    assert MAX_CHUNK_CHARS <= GeminiEmbeddingProvider.max_chars, (
        "the cap must not exceed what the provider itself will accept, or the "
        "provider truncates silently where we would have logged it"
    )
    assert MAX_CHUNK_CHARS >= 30_000


def test_local_provider_trims_to_what_it_can_read():
    """bge-small is a 512-token BERT. Measured against a 32,000-char input,
    cosine vs the full text is 0.9891 at 1,500 chars, 0.9998 at 2,500, and
    exactly 1.000000 from 4,000 — so anything past that is cost with no vector
    change. Raising the shared cap to 30,000 for gemini sent 5x the text
    through this model and a 500-item batch exhausted a 7.8 GB server.

    The shared cap is the maximum ANY provider can use; each provider trims to
    what it can actually read.
    """
    from app.plugin.embedding_provider import FastEmbedProvider, GeminiEmbeddingProvider
    from app.services.embedding import MAX_CHUNK_CHARS

    assert FastEmbedProvider.max_chars >= 4_000   # measured saturation point
    assert FastEmbedProvider.max_chars < MAX_CHUNK_CHARS
    assert MAX_CHUNK_CHARS <= GeminiEmbeddingProvider.max_chars


def test_subprocess_embed_trims_before_crossing_the_process_boundary(monkeypatch):
    """The batch worker uses the subprocess path, not FastEmbedProvider.embed,
    so trimming only in the latter would leave the hot path untrimmed."""
    from app.plugin.embedding_provider import FastEmbedProvider
    from app.services import embedding as emb

    captured = {}

    class _Proc:
        returncode = 0
        stdout = "[[0.0]]"
        stderr = ""

    def fake_run(cmd, input=None, **kw):
        captured["payload"] = input
        return _Proc()

    monkeypatch.setattr(emb.subprocess, "run", fake_run)
    emb._embed_via_subprocess(["x" * 50_000])

    import json as _json
    assert len(_json.loads(captured["payload"])[0]) == FastEmbedProvider.max_chars
