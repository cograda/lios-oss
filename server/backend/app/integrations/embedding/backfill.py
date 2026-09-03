"""The single re-enqueue pass — Phase 4 of the gemini-1536 migration.

Two jobs that look similar and are not:

**reclean()** — the text changed. New cleaners produce different text, so the
content hash changes, so every space must re-embed. This is what a
CLEANER_VERSION bump requires.

**fill_space()** — the text is fine, one space is missing a vector for it. This
is what turning on a new embedding provider requires. It must NOT re-embed the
spaces that already have the row: a chunk that already has a local vector needs
a gemini vector, not both again.

Keeping these apart is the whole reason vectors live in per-space tables — the
second job is an anti-join, and would otherwise be a scan for NULLs across a
wide row.

## Why this exists at all rather than "bump the hash and wait"

Dedup is **pull-based**: it fires only when a producer offers content again.
Bumping CLEANER_VERSION is *permission* to re-embed, not a trigger. Producers
differ — `whatsapp` re-offers every window each cycle (and so did rebuild all
10,512 rows by itself), while `historical_corpus` and `coffee` are event-driven
and would never re-offer anything, and `google_mail.embed_messages` selects
*unembedded* rows only. Without this pass those sources keep v1 text forever.

## chunk_text is a provenance copy, not the authoritative input

For most sources re-offering `embeddings.chunk_text` is correct — it holds the
raw text as ingested. **Mail is the exception**: `import_timemachine_mail.py`
writes Embedding rows directly, bypassing the enqueue chokepoint, and stores
`body[:4000]` with no subject. Re-offering that would silently drop the subject
from every mail chunk — measured at -34.4% on 10-NN agreement, several times
larger than everything cleaning buys. So mail is re-derived through
`google_mail`'s facade instead. Any future writer that bypasses the chokepoint
needs the same treatment, which is why `_authoritative_text` is a lookup table
rather than a bare `row.chunk_text`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement

from app.integrations.embedding.cleaning import BoilerplateFilter, clean
from app.integrations.embedding.models import (
    VECTOR_MODELS,
    Embedding,
    EmbeddingQueue,
)

logger = logging.getLogger(__name__)

# Where the BoilerplateFilter may be applied. TWO independent constraints, and
# both had to be measured rather than reasoned about.
#
# 1. **Which producers never re-offer their content.** The live enqueue path has
#    no fitted filter, so a source whose producer re-offers every cycle would get
#    filtered text here and unfiltered text on the next sync, flipping the hash
#    back and forth and re-embedding forever.
#
#      historical_corpus  event-driven ingest, never re-offered    -> safe
#      email              embed_messages() selects unembedded only -> safe
#      whatsapp           re-offers every window every 30 min      -> UNSAFE
#      vault              re-offers a file whenever it changes     -> unsafe
#
# 2. **Which chunk shapes tolerate line-frequency filtering at all.** Fitting on
#    12,000 real corpus chunks, the filter blocked 161 lines — and the most
#    frequent were *content*, not furniture:
#
#      529  ## unpriced / allowance items          -> subsection (bill of quantities)
#      336  ## priced items (subtotal €#,#.#)      -> subsection; digit
#                                                     normalisation collapses every
#                                                     distinct subtotal into one key
#      128  context: (#) roof › boarding and ...   -> line_item, ALL 3,338 of them:
#                                                     the breadcrumb naming which
#                                                     section a 171-char row belongs
#                                                     to, i.e. its whole disambiguator
#
#    versus the genuine boilerplate it also found, all in free-flowing extracted text:
#
#      192  riai construction contract august # edition ...  -> pdf_page_chunk
#      184  the ownership and copyright of this document ... -> pdf_page_chunk
#      128  docusign envelope id: ...                        -> pdf_page_chunk
#
#    Structured documents repeat their scaffolding *because that scaffolding is
#    what locates each row*. Frequency cannot distinguish that from a letterhead,
#    so the chunk type has to.
BOILERPLATE_SOURCES = frozenset({"historical_corpus", "email"})

# Corpus chunk types eligible for filtering: free-flowing extracted prose only.
# Everything absent from this set is either structured (line_item, subsection,
# trade_summary), conversational (claude_*, conversation_*), or too short to
# carry furniture (email_thread_summary, voice_memo_summary).
BOILERPLATE_CHUNK_TYPES = frozenset({
    "pdf_page_chunk",
    "pdf_low_yield",
    "docx_chunk",
    "email_message",
    "email_thread",
})


def _chunk_type(row: Embedding) -> str | None:
    if not row.metadata_json:
        return None
    try:
        parsed = json.loads(row.metadata_json)
    except (ValueError, TypeError):
        return None
    return parsed.get("chunk_type") if isinstance(parsed, dict) else None


# Chunk shapes whose first line is a subject hoisted by clean_email. Their
# line 1 is exempt from filtering: a subject recurring across 68 chunks of one
# thread blocks like any footer, but that recurrence is what a thread *is*, and
# a subject is the highest-signal line in a mail. Not applied to PDFs, where
# line 1 is usually the letterhead — the main thing worth removing.
_SUBJECT_LED = frozenset({"email_message", "email_thread"})


def boilerplate_eligible(row: Embedding) -> bool:
    """Whether this chunk may have the fitted filter applied to it."""
    if row.source not in BOILERPLATE_SOURCES:
        return False
    if row.source == "email":
        return True  # google_mail's own chunks carry no chunk_type
    return _chunk_type(row) in BOILERPLATE_CHUNK_TYPES


def _post_clean_for(row: Embedding, filt: BoilerplateFilter | None):
    """The post-clean callable for this row, or None."""
    if filt is None or not boilerplate_eligible(row):
        return None
    protect = row.source == "email" or _chunk_type(row) in _SUBJECT_LED
    return lambda text: filt.apply(text, protect_first_line=protect)

# Fit on at most this many documents. BoilerplateFilter.min_docs is an absolute
# count (25), so sampling would change what counts as boilerplate — the cap is a
# memory guard for pathological corpora, not a sampling strategy, and it is set
# above the real corpus size so it never engages here.
MAX_FIT_DOCS = 200_000


def _authoritative_text(session: Session, row: Embedding) -> str | None:
    """The text this chunk should be rebuilt from.

    Defaults to `chunk_text`, which is the raw ingested text for every source
    that goes through the enqueue chokepoint. Mail is special-cased because its
    importer does not — see the module docstring.
    """
    if row.source == "email":
        from app.integrations.google_mail.facade import FACADE as mail

        text = mail.embedding_text(session, row.source_id, row.user_id)
        # Fall through to chunk_text if the message row is gone: a stale
        # embedding is still better than dropping the chunk entirely, and the
        # mismatch is visible as a source_id with no mail_messages row.
        if text:
            return text
        logger.warning(
            "no mail_messages row for embedding source_id=%s user_id=%s — "
            "falling back to chunk_text (subject will be missing)",
            row.source_id, row.user_id,
        )
    return row.chunk_text


PAGE = 500


def _iter_rows(
    session: Session, sources: list[str] | None, limit: int | None
) -> Iterator[Embedding]:
    """Walk `embeddings` in id order without holding a cursor open.

    Keyset pagination rather than `yield_per`. The obvious version — a
    server-side cursor streaming 58k rows — dies with
    `psycopg2.ProgrammingError: named cursor isn't valid anymore` the moment the
    caller commits, and the caller MUST commit periodically or a 58k-row
    enqueue is one unbounded transaction that loses everything on failure.

    Worth noting how this got through: the dry run never commits, so it could
    not have exercised the interaction at all. A read-only rehearsal of a write
    path is not a rehearsal of the write path.

    Paging on `id > last` also makes the walk restartable and immune to rows
    being inserted underneath it.
    """
    last_id = 0
    seen = 0
    while True:
        q = select(Embedding).where(Embedding.id > last_id).order_by(Embedding.id)
        if sources:
            q = q.where(Embedding.source.in_(sources))
        page_size = PAGE if limit is None else min(PAGE, limit - seen)
        if page_size <= 0:
            return
        rows = list(session.scalars(q.limit(page_size)))
        if not rows:
            return
        # Read the cursor position BEFORE yielding: the caller commits mid-page,
        # which expires these instances, and reading `.id` afterwards would
        # issue a refresh per row (or fail outright if one was deleted).
        page_last = rows[-1].id
        for row in rows:
            yield row
        seen += len(rows)
        last_id = page_last
        if limit is not None and seen >= limit:
            return


def fit_boilerplate(
    session: Session, sources: list[str] | None = None, limit: int | None = None
) -> BoilerplateFilter:
    """Fit a BoilerplateFilter over the *cleaned* text of eligible sources.

    Cleaned, not raw: fitting on raw text would count HTML and envelope lines,
    which the cleaners already remove, and those would crowd out the real
    repeated content (letterheads, signature blocks, legal footers) that this
    filter exists to catch.
    """
    eligible = [s for s in (sources or list(BOILERPLATE_SOURCES)) if s in BOILERPLATE_SOURCES]
    if not eligible:
        return BoilerplateFilter()

    docs: list[str] = []
    for row in _iter_rows(session, eligible, limit):
        # Fit on exactly what will be filtered. Including ineligible chunks
        # would let a structured document's repeated scaffolding (`context: …`
        # on all 3,338 line_items) reach min_docs and block that line wherever
        # it also appears in an eligible chunk.
        if not boilerplate_eligible(row):
            continue
        text = _authoritative_text(session, row)
        if not text:
            continue
        ct = _chunk_type(row)
        docs.append(clean(text, row.source, {"chunk_type": ct} if ct else None))
        if len(docs) >= MAX_FIT_DOCS:
            logger.warning("boilerplate fit capped at %d docs", MAX_FIT_DOCS)
            break

    filt = BoilerplateFilter().fit(docs)
    logger.info(
        "boilerplate filter fitted on %d docs from %s — %d blocked lines",
        len(docs), sorted(eligible), filt.n_blocked,
    )
    return filt


def reclean(
    session: Session,
    *,
    sources: list[str] | None = None,
    limit: int | None = None,
    use_boilerplate: bool = True,
    dry_run: bool = True,
) -> dict:
    """Re-offer every chunk's authoritative text through the enqueue chokepoint.

    Enqueue does the deciding: content whose cleaned text and cleaner version
    are unchanged hashes identically and is skipped, so this is safe to re-run
    and cheap when there is nothing to do.

    Returns counts. `dry_run` walks and reports without queueing anything.
    """
    from app.services.embedding import EmbeddingService

    filt = fit_boilerplate(session) if (use_boilerplate and not dry_run) else None
    # In a dry run we still want the fit, for the report — but not the cost of
    # a second full walk, so only fit when it will actually be reported on.
    if use_boilerplate and dry_run:
        filt = fit_boilerplate(session, limit=limit)

    stats = {
        "scanned": 0,
        "queued": 0,
        "unchanged": 0,
        "no_text": 0,
        "by_source": {},
        "boilerplate_lines_blocked": filt.n_blocked if filt else 0,
        "dry_run": dry_run,
    }

    for row in _iter_rows(session, sources, limit):
        stats["scanned"] += 1
        text = _authoritative_text(session, row)
        if not text:
            stats["no_text"] += 1
            continue

        post = _post_clean_for(row, filt)

        if dry_run:
            # Mirror enqueue's decision without writing: same prepare, same hash.
            from app.services.embedding import _content_hash, _prepare_content

            prepared = _prepare_content(text, row.source, row.metadata_json, post)
            changed = _content_hash(prepared) != row.content_hash
        else:
            changed = EmbeddingService.enqueue(
                session,
                source=row.source,
                source_id=row.source_id,
                content=text,
                metadata_json=row.metadata_json,
                user_id=row.user_id,
                post_clean=post,
            )

        bucket = stats["by_source"].setdefault(row.source, {"scanned": 0, "queued": 0})
        bucket["scanned"] += 1
        if changed:
            stats["queued"] += 1
            bucket["queued"] += 1
        else:
            stats["unchanged"] += 1

        if not dry_run and stats["scanned"] % 500 == 0:
            session.commit()
            logger.info("reclean: %(scanned)d scanned, %(queued)d queued", stats)

    if not dry_run:
        session.commit()
    return stats


def _embeddable() -> ColumnElement[bool]:
    """Rows with text worth vectorising.

    Blank chunks are excluded from both the gap count and the fill loop, and
    the two MUST use the same predicate: `fill_space` re-runs its anti-join
    every batch, so a row that is selected but not written is selected again
    forever. Skipping a blank inside the loop is an infinite loop; excluding it
    from the query is not. Mismatched predicates are the same bug wearing a
    hat — a gap that counts rows the loop refuses to write never reaches zero.
    """
    return func.btrim(func.coalesce(Embedding.chunk_text, "")) != ""


def prune_blank_chunks(session: Session, *, dry_run: bool = True) -> int:
    """Delete embedding rows whose text cleans to nothing.

    Separate from `fill_space` on purpose: filling a space is additive and safe
    to re-run, deleting rows is neither, so it does not happen as a side effect
    of asking for vectors. `EmbeddingService.enqueue` prevents new ones; this
    clears what accumulated before that guard existed.
    """
    q = session.query(Embedding).filter(~_embeddable())
    n = q.count()
    if n and not dry_run:
        q.delete(synchronize_session=False)
        session.commit()
    return n


def space_gap(session: Session, provider_id: str) -> int:
    """How many embeddable chunks have no vector in this space — the anti-join."""
    vec = VECTOR_MODELS[provider_id]
    return session.scalar(
        select(func.count(Embedding.id)).where(
            _embeddable(),
            ~select(vec.embedding_id)
            .where(vec.embedding_id == Embedding.id)
            .exists()
        )
    ) or 0


def fill_space(
    session: Session,
    provider_id: str,
    *,
    batch_size: int = 64,
    limit: int | None = None,
    dry_run: bool = True,
) -> dict:
    """Embed existing chunks into ONE space, leaving the others untouched.

    This is the turn-on path for a new provider. It deliberately does not go
    through the queue: the queue's worker writes every available space, which
    would re-embed rows that already have a local vector — 57,917 wasted local
    embeds to gain 57,917 gemini ones.

    Text is taken from `chunk_text` as-is. That is correct *for this job*: the
    text is not being changed, only vectorised again in a second space. Changing
    the text is `reclean`'s job and must happen first, or this space gets built
    from the old text.
    """
    from app.plugin.embedding_provider import _PROVIDERS
    from app.services.embedding import _embed_with

    vec_model = VECTOR_MODELS[provider_id]
    provider = _PROVIDERS[provider_id]()
    if not provider.available():
        raise RuntimeError(
            f"provider {provider_id!r} is not available — check its API key"
        )

    gap = space_gap(session, provider_id)
    stats = {"provider": provider_id, "gap": gap, "embedded": 0, "dry_run": dry_run}
    if dry_run or not gap:
        return stats

    while True:
        rows = list(session.scalars(
            select(Embedding)
            .where(
                _embeddable(),
                ~select(vec_model.embedding_id)
                .where(vec_model.embedding_id == Embedding.id)
                .exists()
            )
            .order_by(Embedding.id)
            .limit(batch_size)
        ))
        if not rows:
            break

        vectors = _embed_with(provider, [r.chunk_text or "" for r in rows])
        # strict: a short result list silently pairs some rows with nothing and
        # commits a partial batch that looks complete. GeminiEmbeddingProvider
        # asserts its own count for this reason; this covers every provider.
        for row, v in zip(rows, vectors, strict=True):
            session.add(vec_model(
                embedding_id=row.id, embedding=v, model_name=provider.model_name,
            ))
        session.commit()
        stats["embedded"] += len(rows)
        logger.info("fill_space %s: %d/%d", provider_id, stats["embedded"], gap)

        if limit and stats["embedded"] >= limit:
            break

    return stats


def pending_queue_depth(session: Session) -> int:
    return session.scalar(
        select(func.count(EmbeddingQueue.id)).where(EmbeddingQueue.status == "pending")
    ) or 0
