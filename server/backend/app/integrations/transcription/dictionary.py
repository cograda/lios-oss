"""Build the proper-noun prompt from the vault.

Transcription models mangle names they have never seen — the recurring failure
in this household's recordings is people, contractors and place names. OpenAI's
audio endpoint accepts a `prompt` that biases decoding toward supplied terms, so
the fix is a good list of terms.

The list should not be hand-maintained. `vault/CLAUDE.md` already specifies that
People notes' `aliases` frontmatter carries "known transcription errors from
voice memos" — so every time someone corrects a mangled name in the vault, the
dictionary that would have prevented it improves. Reading it from there means it
never goes stale and it names exactly the people who actually recur.

(`sandbox/voice-memos/transcribe_openai.py` did this with a hand-written
`custom_dictionary.txt` — 17 terms, last touched June. That file is the thing
this replaces.)

Frontmatter is parsed with a deliberately small reader rather than a YAML
dependency: only two flat keys are needed (`title`, `aliases`), the files are
generated to a documented schema, and a malformed note must degrade to "fewer
terms" rather than break transcription.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# Cap the prompt. Past a point extra terms stop helping and start crowding out
# the model's own language priors — and the request has a size limit.
# Raised from 200 when the wider-vault source landed. Measured against a real
# 10m53s memo on 2026-08-29: of 47 proper nouns in its transcript, People notes
# alone covered 9; this covers 18. Going further (400 terms, min_notes=4) added
# nothing — the remainder are ordinary first names the model gets right unaided,
# or words that simply are not in the vault.
MAX_TERMS = 350

# Single-word terms this short are more likely to hurt than help (they collide
# with ordinary words), so they're dropped from the dictionary.
MIN_TERM_CHARS = 3

_LIST_ITEM = re.compile(r"^\s*-\s*(.+?)\s*$")
_INLINE_LIST = re.compile(r"^\s*\[(.*)\]\s*$")


def _parse_people_note(text: str) -> list[str]:
    """Return the canonical title plus every alias declared in frontmatter."""
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end == -1:
        return []
    frontmatter = text[3:end]

    terms: list[str] = []
    current_key: str | None = None

    for raw_line in frontmatter.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        # A nested list item belonging to the key we're inside.
        if line.startswith((" ", "\t", "-")) and current_key == "aliases":
            match = _LIST_ITEM.match(line)
            if match:
                terms.append(match.group(1))
                continue

        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        current_key = key.strip()
        value = value.strip().strip('"').strip("'")

        if current_key == "title" and value:
            terms.append(value)
        elif current_key == "aliases" and value:
            inline = _INLINE_LIST.match(value)
            if inline:
                terms.extend(
                    part.strip().strip('"').strip("'")
                    for part in inline.group(1).split(",")
                )
            else:
                terms.append(value)

    return [t for t in terms if t]


def collect_vault_terms(vault_root: Path) -> list[str]:
    """Every name and alias from `People/` notes under this vault root."""
    people_dir = vault_root / "People"
    if not people_dir.is_dir():
        logger.debug("[transcription] no People/ dir under %s", vault_root)
        return []

    terms: list[str] = []
    for note in sorted(people_dir.glob("*.md")):
        try:
            terms.extend(_parse_people_note(note.read_text(encoding="utf-8", errors="replace")))
        except OSError as exc:
            logger.warning("[transcription] cannot read %s: %s", note.name, exc)
        # Filename is the canonical name by vault convention, so it belongs in
        # the dictionary even if the frontmatter `title` is missing.
        terms.append(note.stem)

    return terms


def _clean(terms: list[str]) -> list[str]:
    """De-duplicate case-insensitively, drop noise, preserve first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        term = term.strip().strip('"').strip("'")
        if not term or term in ("[]", "-"):
            continue
        # Multi-word terms are kept regardless of length ("Ó Sé" is worth having);
        # single short tokens are the ones that collide with ordinary words.
        if " " not in term and len(term) < MIN_TERM_CHARS:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
    return out


# --------------------------------------------------------------------------
# Wider-vault mining
#
# `collect_vault_terms` reads People notes, which was the right first source —
# people are what recordings mangle most, and `aliases` frontmatter already
# records known mis-transcriptions. But it is ~1% of the vault, and measuring a
# real 10-minute memo on 2026-08-29 showed what that misses: of 51 proper nouns
# in the transcript, 40 were absent from the dictionary. `Wicklow` occurs in 51
# vault notes, `UniFi` in 42, `Polestar` in 36, `Niall` in 22 — all recurring
# heavily, none with a People note, so none reachable.
#
# The module docstring's principle was always "don't hand-maintain it, read it
# from the vault". This reads more of the vault.
# --------------------------------------------------------------------------

# A word the model would mangle is, almost by definition, not an ordinary
# English word — so ordinary words are dropped. This is what separates
# `Crannarc` and `Malahide` from `Focus`, `Backlog` and `Sleep`, which are just
# this vault's own section headings and need no biasing.
# Several paths because this runs on macOS (dev) and Debian (the image), and
# the Debian package puts it behind an alternatives symlink. The image installs
# `wamerican` explicitly — see server/Dockerfile and the warning below for why
# a missing wordlist must not pass quietly.
_WORDLIST_PATHS = (
    Path("/usr/share/dict/words"),
    Path("/usr/share/dict/american-english"),
    Path("/usr/share/dict/british-english"),
)

# Inflections, because the system wordlist is mostly singular/base forms and
# `Notes`/`Meetings`/`Roasted` would otherwise sail through as "not English".
# Plural and past forms only. `("ing","e")` and `("ly","")` were tried and
# removed: they map "Aisling" -> "aisle" and would silently eat a name, which
# is precisely the class of word this dictionary exists to protect.
_SUFFIXES = (("s", ""), ("es", ""), ("ed", ""), ("ed", "e"))

_DATE_TOKENS = frozenset(
    "Jan Feb Mar Apr Jun Jul Aug Sep Sept Oct Nov Dec "
    "Mon Tue Tues Wed Thu Thur Thurs Fri Sat Sun".split()
)

# ⚠️ `.stversions` must stay excluded. Syncthing keeps snapshots of every note
# there, and they once outranked the live files they were snapshots of in the
# vault index (see comar's 2026-08-14 changelog). Here they would simply
# inflate every term's document frequency by its number of historical
# revisions, which quietly rewards notes that churn.
_SKIP_DIRS = frozenset({".obsidian", ".stversions", ".trash", ".git", "Attachments", "Document Store"})

# How many distinct notes a term must appear in. Low enough to catch a place
# that matters, high enough to skip one-off typos and a single note's jargon.
CORPUS_MIN_NOTES = 5
CORPUS_MAX_TERMS = 260

_WORD_RE = re.compile(r"[A-Z][a-zA-Zá-úÁ-Ú-]{2,}")
_CODE_RE = re.compile(r"```.*?```", re.S)
_URL_RE = re.compile(r"https?://\S+")
_POSSESSIVE_RE = re.compile(r"['’]s$")

_corpus_cache: dict[str, list[str]] = {}


@lru_cache(maxsize=1)
def _english_words() -> frozenset[str]:
    """The English wordlist, or empty if the host has none.

    ⚠️ Empty means wider-vault mining is skipped entirely — the dictionary
    silently falls back to People notes alone. That is the correct failure
    (shipping 2,000 unfiltered words into the prompt would be worse), but it
    is invisible, so it logs at WARNING rather than INFO. `python:3.12-slim`
    ships no wordlist; the image installs `wamerican` for exactly this, and
    this warning is what tells you the install went missing.
    """
    for path in _WORDLIST_PATHS:
        try:
            words = frozenset(
                w.strip().lower() for w in path.read_text(errors="replace").splitlines() if w.strip()
            )
        except OSError:
            continue
        if words:
            logger.debug("[transcription] wordlist: %s (%d words)", path, len(words))
            return words
    logger.warning(
        "[transcription] no English wordlist found in %s — wider-vault proper-noun "
        "mining is DISABLED and the dictionary falls back to People notes only. "
        "Install `wamerican` (see server/Dockerfile).",
        [str(p) for p in _WORDLIST_PATHS],
    )
    return frozenset()


def _is_ordinary_english(word: str) -> bool:
    words = _english_words()
    low = word.lower()
    if low in words:
        return True
    return any(low.endswith(suf) and (low[: -len(suf)] + repl) in words for suf, repl in _SUFFIXES)


def collect_corpus_terms(vault_root: Path, *, min_notes: int = CORPUS_MIN_NOTES,
                         limit: int = CORPUS_MAX_TERMS) -> list[str]:
    """Recurring proper nouns from the whole vault, commonest first.

    Document frequency, not raw count: a term repeated forty times in one note
    is that note's jargon; a term appearing once each in forty notes is part of
    the household's vocabulary. Only the second kind is worth biasing toward.

    Cached per vault root — the walk is ~650 notes and transcription is called
    once per file. The cache is process-lifetime, which is the right trade for
    a cron worker: a term added to the vault today reaches the dictionary at
    the next restart, and nothing here is worth invalidating on.
    """
    if not _english_words():
        return []
    key = str(vault_root)
    if key in _corpus_cache:
        return _corpus_cache[key]

    freq: Counter[str] = Counter()
    for note in vault_root.rglob("*.md"):
        if any(part in _SKIP_DIRS for part in note.parts):
            continue
        try:
            text = note.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = _URL_RE.sub("", _CODE_RE.sub("", text))
        seen: set[str] = set()
        # Every token, including the first of each line. Skipping line-initial
        # words (to avoid grammar capitalisation) was tried and abandoned: a
        # vault is mostly bullets and headings, so it discarded the first word
        # of nearly every line — "Instagram" appears in 14 notes and never
        # registered once. The English filter below already removes what that
        # skip was aiming at, since grammar-capitalised words are by definition
        # ordinary words.
        for raw in text.split():
                word = _POSSESSIVE_RE.sub("", raw.strip("*_#[]()<>.,;:!?\"'`|-"))
                if not _WORD_RE.fullmatch(word) or word in _DATE_TOKENS:
                    continue
                if _is_ordinary_english(word):
                    continue
                seen.add(word)
        freq.update(seen)

    terms = [w for w, count in freq.most_common() if count >= min_notes][:limit]
    _corpus_cache[key] = terms
    logger.debug("[transcription] %d corpus terms from %s", len(terms), vault_root)
    return terms


def build_prompt(vault_root: Path | None, extra_terms: list[str] | None = None) -> str:
    """Render the transcription prompt, or a neutral one if there are no terms.

    Order is precedence, because `_clean` keeps first-seen and `MAX_TERMS`
    truncates: People notes first (authoritative — their `aliases` are actual
    recorded mis-transcriptions), then operator-supplied extras, then mined
    corpus terms. The mined ones are the guesses, so they lose the tie.
    """
    terms: list[str] = []
    if vault_root is not None:
        terms.extend(collect_vault_terms(vault_root))
    terms.extend(extra_terms or [])
    if vault_root is not None:
        terms.extend(collect_corpus_terms(vault_root))
    terms = _clean(terms)[:MAX_TERMS]

    if not terms:
        return "This is transcribed audio from a personal voice note."
    return (
        "This is a personal voice note. It may contain these proper nouns: "
        + ", ".join(terms)
        + "."
    )
