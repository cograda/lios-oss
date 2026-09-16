"""The re-enqueue pass (db tier) — Phase 4.

The properties worth pinning here are all "does it do the *right* work",
not "does it run": re-offering the wrong text, or re-embedding a space that
already has the row, both succeed quietly and cost either quality or money.
"""

import json

import pytest

from app.integrations.embedding import backfill
from app.integrations.embedding.cleaning import CLEANER_VERSION
from app.services import embedding as emb
from app.services.embedding import (
    Embedding,
    EmbeddingQueue,
    EmbeddingService,
    EmbeddingVecBgeSmall384,
    EmbeddingVecGemini1536,
)

pytestmark = pytest.mark.db


def _vec(x: float = 1.0) -> list[float]:
    v = [0.0] * emb.VECTOR_DIM
    v[0] = x
    return v


@pytest.fixture
def fake_embedder(monkeypatch):
    def embed(texts):
        return [_vec() for _ in texts]

    monkeypatch.setattr(emb, "_embed_via_subprocess", embed)
    return embed


# ---------------------------------------------------------------------------
# Authoritative text
# ---------------------------------------------------------------------------

def test_mail_is_rebuilt_from_mail_messages_not_from_chunk_text(db_session, fake_embedder):
    """The landmine this pass exists to avoid.

    `import_timemachine_mail.py` writes Embedding rows directly and stores
    `body[:4000]` — body only, no subject. Re-offering chunk_text would drop
    the subject from every mail chunk, measured at -34.4% on 10-NN agreement.
    """
    from app.integrations.google_mail.models import MailMessage

    db_session.add(MailMessage(
        google_message_id="m1", thread_id="t1", account_email="a@b.ie", user_id=1,
        subject="Loan Offer Issued", body_text="Call me at 3:30.",
    ))
    row = Embedding(
        source="email", source_id="m1", user_id=1,
        chunk_text="Call me at 3:30.",  # body only, as the importer wrote it
        content_hash="stale",
    )
    db_session.add(row)
    db_session.commit()

    text = backfill._authoritative_text(db_session, row)
    assert "Loan Offer Issued" in text
    assert "Call me at 3:30." in text


def test_mail_without_a_message_row_falls_back_rather_than_dropping(db_session):
    row = Embedding(
        source="email", source_id="orphan", user_id=1,
        chunk_text="body survives", content_hash="h",
    )
    db_session.add(row)
    db_session.commit()
    assert backfill._authoritative_text(db_session, row) == "body survives"


def test_non_mail_sources_use_chunk_text(db_session):
    row = Embedding(
        source="vault", source_id="a.md", user_id=None,
        chunk_text="raw note text", content_hash="h",
    )
    db_session.add(row)
    db_session.commit()
    assert backfill._authoritative_text(db_session, row) == "raw note text"


# ---------------------------------------------------------------------------
# reclean
# ---------------------------------------------------------------------------

def test_reclean_dry_run_writes_nothing_but_reports_what_would_change(db_session, fake_embedder):
    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello there")
    EmbeddingService.process_queue(db_session)
    # Force a stale hash so the row looks like it predates the current cleaner.
    db_session.query(Embedding).update({"content_hash": "definitely-stale"})
    db_session.commit()

    stats = backfill.reclean(db_session, dry_run=True)

    assert stats["dry_run"] is True
    assert stats["scanned"] == 1
    assert stats["queued"] == 1
    assert db_session.query(EmbeddingQueue).filter_by(status="pending").count() == 0


def test_reclean_is_a_noop_when_text_and_cleaner_are_unchanged(db_session, fake_embedder):
    """Safe to re-run: enqueue does the deciding, by hash."""
    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello there")
    EmbeddingService.process_queue(db_session)

    stats = backfill.reclean(db_session, dry_run=False)

    assert stats["queued"] == 0
    assert stats["unchanged"] == 1


def test_reclean_queues_when_the_cleaner_version_moves(db_session, fake_embedder, monkeypatch):
    """A CLEANER_VERSION bump is what makes previously-identical text hash
    differently — that is the mechanism this pass exists to act on."""
    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello there")
    EmbeddingService.process_queue(db_session)

    monkeypatch.setattr(emb, "CLEANER_VERSION", CLEANER_VERSION + 1)
    stats = backfill.reclean(db_session, dry_run=False)

    assert stats["queued"] == 1
    assert db_session.query(EmbeddingQueue).filter_by(status="pending").count() == 1


def test_reclean_can_be_scoped_to_one_source(db_session, fake_embedder):
    EmbeddingService.enqueue(db_session, "vault", "a.md", "note")
    EmbeddingService.enqueue(db_session, "coffee", "c1", "beans")
    EmbeddingService.process_queue(db_session)

    stats = backfill.reclean(db_session, sources=["vault"], dry_run=True)
    assert set(stats["by_source"]) == {"vault"}


# ---------------------------------------------------------------------------
# Boilerplate
# ---------------------------------------------------------------------------

def _corpus_doc(i, text, chunk_type="docx_chunk"):
    return Embedding(
        source="historical_corpus", source_id=f"d{i}", user_id=None,
        chunk_text=text, content_hash=f"h{i}",
        metadata_json=json.dumps({"chunk_type": chunk_type}),
    )


def test_boilerplate_is_only_applied_to_sources_that_are_never_re_offered():
    """Not "which sources have boilerplate" — which producers re-offer.

    whatsapp re-offers every window every 30 minutes. The live path has no
    fitted filter, so filtering it here would flip the hash back and forth on
    every sync and re-embed forever.
    """
    assert "whatsapp" not in backfill.BOILERPLATE_SOURCES
    assert "vault" not in backfill.BOILERPLATE_SOURCES
    assert backfill.BOILERPLATE_SOURCES == {"historical_corpus", "email"}


def test_structured_chunk_types_are_never_boilerplate_filtered(db_session):
    """Measured on 12,000 real chunks, not reasoned about.

    Fitting across the whole corpus, the most-blocked lines were content:
    `context: (#) roof › boarding and second fixings` on ALL 3,338 line_items
    (the breadcrumb naming which section a 171-char row belongs to — its entire
    disambiguator), and `## priced items (subtotal €#,#.#)` on subsections,
    where digit normalisation collapses every distinct subtotal into one key.
    Structured documents repeat their scaffolding *because that scaffolding
    locates each row*, and frequency cannot tell that from a letterhead.
    """
    for ct in ("line_item", "subsection", "trade_summary", "claude_conversation"):
        row = _corpus_doc(1, "context: (2) roof › boarding", chunk_type=ct)
        assert not backfill.boilerplate_eligible(row), ct
    for ct in ("pdf_page_chunk", "docx_chunk", "email_message"):
        assert backfill.boilerplate_eligible(_corpus_doc(1, "x", chunk_type=ct)), ct


def test_boilerplate_filter_strips_a_repeated_letterhead(db_session, fake_embedder):
    """A line in many otherwise-unrelated documents carries no information
    distinguishing them — which no per-source regex can know in advance."""
    footer = "The ownership and copyright of this document is exclusively that of the RIAI."
    # Bodies must differ in WORDS, not only digits — `_key` normalises digits,
    # so thirty lines differing only by an index are one line to the filter.
    subjects = [
        "roof", "gutters", "insulation", "windows", "drainage", "screed",
        "rafters", "flashing", "soffits", "render",
    ]
    for i in range(30):
        body = f"Revised {subjects[i % len(subjects)]} detail agreed on site with the architect."
        db_session.add(_corpus_doc(i, f"{body}\n{footer}"))
    db_session.commit()

    filt = backfill.fit_boilerplate(db_session)
    body = "Revised roof detail agreed on site with the architect."
    out = filt.apply(f"{body}\n{footer}")
    assert footer.lower() not in out.lower()
    assert "Revised roof detail" in out


def test_digit_normalisation_can_overblock_lines_that_differ_only_by_number(db_session, fake_embedder):
    """Pinned as a known limitation, found by a test I expected to pass.

    `_key` normalises digits so `Order #12345` and `#67890` collapse — which is
    the point for templated footers, and a hazard for content whose only
    variation is numeric. Containment is by chunk-type scope
    (`BOILERPLATE_CHUNK_TYPES` excludes the structured shapes where numbers
    carry the meaning), not by making the filter cleverer.
    """
    for i in range(30):
        db_session.add(_corpus_doc(i, f"Provisional sum carried forward: EUR {i}0,000.00"))
    db_session.commit()

    filt = backfill.fit_boilerplate(db_session)
    assert filt.apply("Provisional sum carried forward: EUR 40,000.00") == ""


def test_a_hoisted_email_subject_survives_recurring_across_a_thread(db_session, fake_embedder):
    """`re: aib mortgage application` recurs across 68 real chunks of one
    thread and blocks like any footer — but that recurrence is what a thread
    *is*, and dropping subjects costs -34.4% on 10-NN agreement."""
    subject = "Re: AIB mortgage application for the Malahide purchase"
    for i in range(30):
        db_session.add(_corpus_doc(i, f"{subject}\n\nMessage body {i} differs here entirely.",
                                   chunk_type="email_message"))
    db_session.commit()

    filt = backfill.fit_boilerplate(db_session)
    row = _corpus_doc(1, f"{subject}\n\nbody", chunk_type="email_message")
    post = backfill._post_clean_for(row, filt)
    assert subject in post(f"{subject}\n\nbody")

    # A PDF page gets the opposite treatment: its line 1 is usually the
    # letterhead, which is the main thing worth removing.
    pdf = _corpus_doc(1, "x", chunk_type="pdf_page_chunk")
    assert backfill._post_clean_for(pdf, filt) is not None


def test_boilerplate_keeps_short_lines_even_when_they_recur(db_session, fake_embedder):
    """"Thanks" recurs constantly and IS the content in a short message."""
    for i in range(30):
        db_session.add(_corpus_doc(i, f"Point {i} about the roof detail.\nThanks"))
    db_session.commit()

    filt = backfill.fit_boilerplate(db_session)
    assert filt.apply("Point 1 about the roof detail.\nThanks").endswith("Thanks")


# ---------------------------------------------------------------------------
# fill_space
# ---------------------------------------------------------------------------

def test_space_gap_counts_chunks_missing_that_space(db_session, fake_embedder):
    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello")
    EmbeddingService.process_queue(db_session)

    assert backfill.space_gap(db_session, "fastembed-bge-small") == 0
    assert backfill.space_gap(db_session, "gemini-embedding-2") == 1


def test_fill_space_writes_only_the_named_space(db_session, fake_embedder, monkeypatch):
    """The reason vectors live in per-space tables.

    Going through the queue instead would write EVERY available space, so
    filling gemini would re-embed 57,917 local vectors that already exist —
    paying twice to gain nothing.
    """
    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello")
    EmbeddingService.process_queue(db_session)
    local_before = db_session.query(EmbeddingVecBgeSmall384).one()
    created_before = local_before.created_at

    class _Remote:
        provider_id = "gemini-embedding-2"
        model_name = "gemini-embedding-2"
        dim = 1536

        def available(self):
            return True

        def embed(self, texts):
            return [[0.0] * 1536 for _ in texts]

    monkeypatch.setitem(
        __import__("app.plugin.embedding_provider", fromlist=["_PROVIDERS"])._PROVIDERS,
        "gemini-embedding-2", _Remote,
    )

    stats = backfill.fill_space(db_session, "gemini-embedding-2", dry_run=False)

    assert stats["embedded"] == 1
    assert db_session.query(EmbeddingVecGemini1536).count() == 1
    # The local space was left completely alone, not rewritten with the same value.
    assert db_session.query(EmbeddingVecBgeSmall384).count() == 1
    assert db_session.query(EmbeddingVecBgeSmall384).one().created_at == created_before


def _remote_provider():
    class _Remote:
        provider_id = "gemini-embedding-2"
        model_name = "gemini-embedding-2"
        dim = 1536

        def available(self):
            return True

        def embed(self, texts):
            # Mirrors the real API: an empty part is rejected outright, which is
            # how the 42 blank rows surfaced in production at all.
            if any(not t.strip() for t in texts):
                raise RuntimeError("contains an empty Part")
            return [[0.0] * 1536 for _ in texts]

    return _Remote


def test_blank_chunks_are_never_enqueued(db_session, fake_embedder):
    """Cleaning can empty an item; an empty item is not a retrievable unit."""
    assert EmbeddingService.enqueue(db_session, "vault", "blank.md", "   \n\n  ") is False
    assert db_session.query(EmbeddingQueue).count() == 0


def test_a_chunk_that_becomes_blank_drops_its_existing_row(db_session, fake_embedder):
    """The index must shed junk, not merely stop adding it.

    The source item still exists — it just has no embeddable content any more,
    so leaving the old row behind would keep a blank preview permanently
    searchable.
    """
    EmbeddingService.enqueue(db_session, "vault", "a.md", "real content")
    EmbeddingService.process_queue(db_session)
    assert db_session.query(Embedding).count() == 1

    EmbeddingService.enqueue(db_session, "vault", "a.md", "\n\n")

    assert db_session.query(Embedding).count() == 0
    # The vector went with it — ON DELETE CASCADE, not an orphan.
    assert db_session.query(EmbeddingVecBgeSmall384).count() == 0


def test_fill_space_skips_a_blank_row_that_predates_the_guard(
    db_session, fake_embedder, monkeypatch
):
    """The production failure, pinned: one blank row killed a 58k-row backfill.

    Reverting either half of the fix fails this — without the predicate the
    blank reaches the provider and the whole batch is rejected.

    The blank must be excluded from the *query* rather than skipped inside the
    loop, and `space_gap` must use the same predicate. fill_space re-runs its
    anti-join every batch, so a row that is selected but never written is
    selected again forever; a bare `continue` is the tempting fix and hangs the
    process. A gap that counts rows the loop won't write never reaches zero
    either. Hence one shared `_embeddable()`.
    """
    EmbeddingService.enqueue(db_session, "vault", "a.md", "real content")
    EmbeddingService.process_queue(db_session)
    # Bypass enqueue's guard to recreate a row from before it existed.
    db_session.add(Embedding(
        source="vault", source_id="blank.md", chunk_text="",
        content_hash="deadbeef", user_id=None,
    ))
    db_session.commit()

    monkeypatch.setitem(
        __import__("app.plugin.embedding_provider", fromlist=["_PROVIDERS"])._PROVIDERS,
        "gemini-embedding-2", _remote_provider(),
    )

    # Excluded from the gap...
    assert backfill.space_gap(db_session, "gemini-embedding-2") == 1
    # ...and from the fill, which terminates and never sends the empty string.
    stats = backfill.fill_space(db_session, "gemini-embedding-2", dry_run=False)
    assert stats["embedded"] == 1
    assert db_session.query(EmbeddingVecGemini1536).count() == 1


def test_prune_blank_chunks_reports_before_it_deletes(db_session, fake_embedder):
    EmbeddingService.enqueue(db_session, "vault", "a.md", "real content")
    EmbeddingService.process_queue(db_session)
    db_session.add(Embedding(
        source="vault", source_id="blank.md", chunk_text="",
        content_hash="deadbeef", user_id=None,
    ))
    db_session.commit()

    assert backfill.prune_blank_chunks(db_session, dry_run=True) == 1
    assert db_session.query(Embedding).count() == 2  # dry run wrote nothing

    assert backfill.prune_blank_chunks(db_session, dry_run=False) == 1
    assert db_session.query(Embedding).count() == 1


def test_fill_space_refuses_an_unavailable_provider(db_session):
    with pytest.raises(RuntimeError, match="not available"):
        backfill.fill_space(db_session, "gemini-embedding-2", dry_run=False)


def test_fill_space_dry_run_reports_the_gap_without_embedding(db_session, fake_embedder, monkeypatch):
    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello")
    EmbeddingService.process_queue(db_session)

    class _Remote:
        provider_id = model_name = "gemini-embedding-2"
        dim = 1536

        def available(self):
            return True

        def embed(self, texts):
            raise AssertionError("dry run must not embed")

    monkeypatch.setitem(
        __import__("app.plugin.embedding_provider", fromlist=["_PROVIDERS"])._PROVIDERS,
        "gemini-embedding-2", _Remote,
    )

    stats = backfill.fill_space(db_session, "gemini-embedding-2", dry_run=True)
    assert stats["gap"] == 1
    assert stats["embedded"] == 0
    assert db_session.query(EmbeddingVecGemini1536).count() == 0


# ---------------------------------------------------------------------------
# Iteration
# ---------------------------------------------------------------------------

def test_walk_survives_commits_mid_iteration(db_session, fake_embedder):
    """The failure that only the write path can produce.

    `reclean` must commit periodically or a 58k-row enqueue is one unbounded
    transaction that loses everything on failure. A `yield_per` server-side
    cursor dies the moment that happens —
    `psycopg2.ProgrammingError: named cursor isn't valid anymore` — and the dry
    run, which never commits, cannot exercise it. Hence keyset pagination.
    """
    for i in range(backfill.PAGE + 25):
        db_session.add(_corpus_doc(i, f"Document {i} about roofing and drainage detail."))
    db_session.commit()

    seen = 0
    for _row in backfill._iter_rows(db_session, None, None):
        seen += 1
        if seen % 100 == 0:
            db_session.commit()  # what killed the cursor
    assert seen == backfill.PAGE + 25


def test_walk_respects_limit_across_page_boundaries(db_session, fake_embedder):
    for i in range(backfill.PAGE + 25):
        db_session.add(_corpus_doc(i, f"Document {i}."))
    db_session.commit()

    assert len(list(backfill._iter_rows(db_session, None, 10))) == 10
    assert len(list(backfill._iter_rows(db_session, None, backfill.PAGE + 5))) == backfill.PAGE + 5
    assert len(list(backfill._iter_rows(db_session, None, None))) == backfill.PAGE + 25


def test_reclean_commits_as_it_goes(db_session, fake_embedder):
    """Progress must survive a failure partway through 58k rows."""
    for i in range(backfill.PAGE + 25):
        db_session.add(_corpus_doc(i, f"Document {i} about roofing and drainage detail."))
    db_session.commit()

    stats = backfill.reclean(db_session, use_boilerplate=False, dry_run=False)
    assert stats["scanned"] == backfill.PAGE + 25
    assert stats["queued"] == backfill.PAGE + 25
