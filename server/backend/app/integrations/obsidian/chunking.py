"""Section/bullet chunking for large, list-structured vault files.

Motivation (2026-08-13 retrieval measurement): a 12-query retrieval sweep
found a bullet that exists verbatim inside `Task Backlog.md` (~340 lines)
was unretrievable by semantic search. The whole file was embedded as ONE
chunk (`_prepare_chunk` truncated to the first 2000 chars past frontmatter),
so a single bullet's signal was diluted by roughly three hundred unrelated
lines, and anything past the 2000-char cut never got a vector at all.

Design:

- **Only files that need it.** Most vault files (daily notes, meeting notes)
  are prose-shaped and comfortably fit the old single-chunk cap — chunking
  them by heading/bullet would add noise, not signal, and risks fragmenting
  a narrative note into un-interpretable pieces. This module only engages
  for files that are BOTH longer than the old whole-file cap
  (`CHUNK_THRESHOLD_CHARS`) AND look list-structured (at least
  `MIN_BULLET_LINES` top-level bullets) — `should_chunk()`. Everything else
  keeps the exact prior whole-file behaviour (single chunk, `source_id`
  is the bare file path).
- **Boundary = heading path + top-level bullet.** A block starts at a
  heading line or an unindented list item; every indented line after it
  (a nested sub-bullet, wrapped continuation text) is folded into that same
  block, never split out as its own chunk — the existing task format keeps
  real detail in indented children (promotion notes, quotes, dates), and a
  child chunked away from its parent bullet is uninterpretable on its own.
- **Floor and ceiling, not per-bullet chunks.** A bare one-line bullet like
  `- [ ] Empty the dryer` embeds poorly and reads as noise without context,
  so consecutive blocks *under the same heading* are packed together up to
  `MAX_CHUNK_CHARS`, and packing only stops early once the accumulated chunk
  already has `MIN_CHUNK_CHARS`. Blocks never merge across a heading
  boundary — that would blur which section a chunk answers for. A single
  block (bullet + its children) that alone exceeds the ceiling is still
  emitted whole: splitting inside one item would break the parent/child
  rule above.
- **Every chunk carries its heading path.** `Task Backlog.md > Home >
  Malahide House` prefixes the embedded text, so a bare bullet is never
  presented without knowing what backlog/domain it belongs to.

`CHUNKER_VERSION` exists for the same reason `CLEANER_VERSION` exists in the
embedding pipeline: bumping it must force every file to be treated as
changed on the next index run, without a schema migration. `sync.py` folds
it into the stored file-hash rather than adding a column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Bump whenever the boundary rule below changes in a way that would produce
# different chunks for existing content — see module docstring.
CHUNKER_VERSION = 1

# Below this, a file keeps the old whole-file single-chunk behaviour even if
# it happens to contain a few bullets (e.g. a short daily note with one or
# two task lines). Matches the previous `_prepare_chunk` cap exactly, so a
# file just under this line is byte-for-byte what it always was.
CHUNK_THRESHOLD_CHARS = 2000

# A file must have at least this many *top-level* (unindented) bullet lines
# to be treated as list-structured. Below this, a long file is more likely a
# meeting note or a rambling daily entry — those stay whole-file too.
MIN_BULLET_LINES = 3

# Merge floor: keep packing consecutive blocks under the same heading until
# the accumulated chunk reaches at least this many characters. Well above a
# single short bullet's ~30-80 chars, so a lone "- [ ] Empty the dryer" never
# ships alone — it merges with whatever else lives under the same heading.
MIN_CHUNK_CHARS = 200

# Split ceiling: stop adding more blocks to a chunk once it would exceed
# this many characters (the block that would have pushed it over starts the
# next chunk instead). A single oversized block is still emitted whole — see
# module docstring on why children never get split from their parent.
MAX_CHUNK_CHARS = 1500

# Whole-file (non-chunked) text is still capped the same way `_prepare_chunk`
# capped it historically.
WHOLE_FILE_CAP = CHUNK_THRESHOLD_CHARS  # kept as a separate name for clarity

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_TOP_BULLET_RE = re.compile(r"^[-*+]\s|^\d+[.)]\s")


@dataclass
class VaultChunkResult:
    source_id: str
    text: str
    heading_path: list[str] = field(default_factory=list)


def strip_frontmatter(content: str) -> str:
    """Drop a leading `---\\n...\\n---` YAML frontmatter block, if present."""
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            return content[end + 3:].strip()
    return content


def _count_top_level_bullets(content: str) -> int:
    count = 0
    in_fence = False
    for line in content.split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line[:1].isspace():
            continue
        if _TOP_BULLET_RE.match(line):
            count += 1
    return count


def should_chunk(stripped_content: str) -> bool:
    """Whether a (frontmatter-stripped) file qualifies for section/bullet
    chunking rather than the old whole-file single chunk."""
    if len(stripped_content) <= CHUNK_THRESHOLD_CHARS:
        return False
    return _count_top_level_bullets(stripped_content) >= MIN_BULLET_LINES


def _parse_blocks(content: str) -> list[tuple[tuple[str, ...], str]]:
    """Split content into (heading_path, text) blocks.

    A block is either a heading-updating line (which flushes and starts a
    new heading context with no text of its own) or a run of lines starting
    at an unindented bullet/paragraph line and continuing through every
    subsequent indented or wrapped line, until a blank line, a new
    unindented bullet, or a heading ends it. Fenced code blocks (```) are
    opaque — their contents never parse as headings or bullets.
    """
    blocks: list[tuple[tuple[str, ...], str]] = []
    heading_stack: list[str] = []
    current: list[str] = []
    in_fence = False

    def flush() -> None:
        nonlocal current
        if current:
            text = "\n".join(current).strip("\n")
            if text.strip():
                blocks.append((tuple(heading_stack), text))
            current = []

    for line in content.split("\n"):
        stripped_line = line.strip()

        if stripped_line.startswith("```"):
            in_fence = not in_fence
            current.append(line)
            continue
        if in_fence:
            current.append(line)
            continue

        heading_match = _HEADING_RE.match(line)
        if heading_match:
            flush()
            level = len(heading_match.group(1))
            heading_stack[level - 1:] = [heading_match.group(2).strip()]
            continue

        is_top_bullet = bool(_TOP_BULLET_RE.match(line)) and not line[:1].isspace()
        if is_top_bullet:
            flush()
            current = [line]
            continue

        if stripped_line == "":
            flush()
            continue

        # Indented continuation (nested sub-bullet, wrapped prose) or the
        # start/continuation of a plain paragraph — either way, part of the
        # current block.
        current.append(line)

    flush()
    return blocks


def _pack_blocks(
    blocks: list[tuple[tuple[str, ...], str]],
) -> list[tuple[tuple[str, ...], str]]:
    """Merge consecutive same-heading blocks up to the floor/ceiling rule.

    Never merges across a heading-path change. Always accepts at least one
    block into a fresh run outright, even if that block alone exceeds
    `MAX_CHUNK_CHARS` — see module docstring.
    """
    packed: list[tuple[tuple[str, ...], str]] = []
    run_path: tuple[str, ...] | None = None
    run_parts: list[str] = []
    run_len = 0

    def close_run() -> None:
        nonlocal run_parts, run_len
        if run_parts:
            packed.append((run_path, "\n\n".join(run_parts)))
            run_parts, run_len = [], 0

    for heading_path, text in blocks:
        if heading_path != run_path:
            close_run()
            run_path = heading_path

        if run_parts and run_len >= MIN_CHUNK_CHARS and run_len + len(text) > MAX_CHUNK_CHARS:
            close_run()

        run_parts.append(text)
        run_len += len(text)

    close_run()
    return packed


def chunk_file(rel_path: str, content: str) -> list[VaultChunkResult]:
    """Chunk one vault file's content into embeddable pieces.

    Returns at least one result. Files that don't qualify per `should_chunk`
    get exactly one result whose `source_id` is the bare `rel_path` (the
    historical whole-file behaviour, byte-compatible with the old
    `_prepare_chunk`). Qualifying files get one result per packed
    heading/bullet block, `source_id` formatted `{rel_path}#{index}`.
    """
    stripped = strip_frontmatter(content)

    if not should_chunk(stripped):
        return [VaultChunkResult(
            source_id=rel_path,
            text=f"{rel_path}\n\n{stripped[:WHOLE_FILE_CAP]}",
        )]

    blocks = _parse_blocks(stripped)
    packed = _pack_blocks(blocks)

    if not packed:
        # Defensive: should_chunk() said this file looks list-structured,
        # but parsing found nothing splittable. Fall back rather than lose
        # the file's content entirely.
        return [VaultChunkResult(
            source_id=rel_path,
            text=f"{rel_path}\n\n{stripped[:WHOLE_FILE_CAP]}",
        )]

    results = []
    for idx, (heading_path, text) in enumerate(packed):
        breadcrumb = " > ".join([rel_path, *heading_path]) if heading_path else rel_path
        results.append(VaultChunkResult(
            source_id=f"{rel_path}#{idx}",
            text=f"{breadcrumb}\n\n{text}",
            heading_path=list(heading_path),
        ))
    return results
