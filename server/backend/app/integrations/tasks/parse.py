"""Deterministic parse of `Task Backlog.md` into structured task records.

Promoted from `scripts/taskgraph_parse.py`, which said to do exactly this
"once the schema settles" — it has. The deterministic tier moves here
unchanged in behaviour; the script's optional LLM edge-inference tier stays
where it is, because inferred edges are a separate decision from importing
what the file actually states.

Everything here is exact rather than heuristic: checkbox state, priority
emoji, `📅` due dates, `✅` completion dates, `#tags`, `[[wikilinks]]`, the H1
category and the H2 section. Nothing is guessed, so every record produced
carries confidence 1.0 by construction.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

CODE_SPAN_RE = re.compile(r"`[^`]*`")

TASK_RE = re.compile(r"^- \[([ xX])\] (.+)$")
SUBBULLET_RE = re.compile(r"^\s+- (.+)$")
PRIORITY_RE = re.compile(r"[🔺⏫🔼🔽]")
DUE_DATE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
DONE_DATE_RE = re.compile(r"✅\s*(\d{4}-\d{2}-\d{2})")
TAG_RE = re.compile(r"#[\w/.-]+")
WIKILINK_RE = re.compile(r"\[\[([^|\]]+?)(?:\|[^\]]+)?\]\]")

# The file's four priority emoji, mapped onto the schema's five-value scale.
# `urgent` becomes `highest`; the schema keeps a `lowest` the file never uses.
PRIORITY_RANK = {"🔺": "highest", "⏫": "high", "🔼": "medium", "🔽": "low"}

# Tags describing *status or context* rather than subject matter. They become
# columns (context, energy) or are dropped; they never become topic links.
STATUS_TAGS = {"#blocked", "#focus", "#quick", "#deep", "#review", "#errand", "#sitdown"}

# `#sitdown` is a GTD context in all but name — the file's own heading calls it
# "A context, not a priority". `#errand` is the other one.
CONTEXT_TAGS = {"#sitdown": "sitdown", "#errand": "errand"}
ENERGY_TAGS = {"#quick": "quick", "#deep": "deep"}

# Headings that are page structure, not categories.
NON_CATEGORY_H1 = {"Task Backlog", "Backlog — by category"}

# Everything above this heading is the preamble: frontmatter, the intro, and
# the three ```tasks query lenses. Those are views over the list below, not
# entries in it, and the renderer emits them from a template.
BODY_HEADING = "Backlog — by category"


@dataclass
class ParsedTask:
    import_key: str
    title: str
    completed: bool
    priority: str | None
    due_date: str | None
    done_date: str | None
    category: str            # H1 — Home / Renovation / Kids / Finance / Admin
    section: str | None      # H2 — usually itself a [[wikilink]]
    section_note: str | None  # the wikilink target, when the H2 is one
    subsection: str | None   # H3, where a section has them (the PTA case)
    tags: list[str]
    context: str | None
    energy: str | None
    wikilinks: list[str]
    subbullets: list[str] = field(default_factory=list)
    line_number: int = 0
    # Position in the file. The order is a person's ordering of their own
    # work, so it is content, not presentation.
    order: int = 0

    @property
    def description(self) -> str | None:
        """Sub-bullets are where the constraints live, and they are the bulk of
        the file's 14,592 words. Joined as markdown, not flattened."""
        return "\n".join(f"- {b}" for b in self.subbullets) or None


def _mask_code_spans(text: str) -> str:
    """Blank the contents of `inline code`, keeping length and the backticks.

    ⚠️ Metadata inside code is being *quoted*, not declared. The line that
    found this says so outright: a task whose text reads "the original stale
    `📅 2026-06-11` is deliberately dropped rather than carried forward as a
    7-week-overdue date" had that very date parsed as its due date — the
    importer resurrecting the thing the note records as deliberately dropped.
    Same for a `#tag` or a priority glyph mentioned in prose.

    Masking preserves offsets, so matches found here index correctly into the
    original string.
    """
    return CODE_SPAN_RE.sub(lambda m: "`" + " " * (len(m.group()) - 2) + "`", text)


def _strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """Remove the given [start, end) ranges from `text`."""
    out, prev = [], 0
    for start, end in sorted(spans):
        out.append(text[prev:start])
        prev = end
    out.append(text[prev:])
    return "".join(out)


def import_key(text: str) -> str:
    """Content-addressed key over the *normalised* task text.

    Not the task's identity — `uid` (TASK-0042) is that, and it must survive an
    edit to the wording. This exists so a re-run of the import recognises a row
    it already created rather than duplicating it, the same job financier's
    content-addressed transaction ids do for overlapping statement exports.

    ⚠️ Editing a task's title in the file therefore produces a new key. That is
    the correct trade for a one-way import: it is better to notice an unmatched
    row than to silently rewrite the wrong one.
    """
    norm = _mask_code_spans(text)
    norm = PRIORITY_RE.sub("", norm)
    norm = TAG_RE.sub("", norm)
    norm = DUE_DATE_RE.sub("", norm)
    norm = DONE_DATE_RE.sub("", norm)
    norm = re.sub(r"[^\w\s]", "", norm)
    norm = re.sub(r"\s+", " ", norm).strip().lower()
    return "T-" + hashlib.sha1(norm.encode()).hexdigest()[:10]


def clean_title(text: str) -> str:
    """Strip *metadata* from a task line, and nothing else.

    ⚠️ An earlier version also unwrapped `[[wikilinks]]` and `**bold**`. The
    render round-trip showed why that is wrong: those are the file's own
    notation and carry meaning a person put there — a wikilink is a typed
    reference to a note, and it is the highest-confidence signal in the file.
    Unwrapping them made the title unrenderable without guessing where they
    had been. Only the priority emoji, dates and tags come out, because those
    become columns and would otherwise be stored twice.
    """
    masked = _mask_code_spans(text)
    spans = [
        m.span()
        for pattern in (PRIORITY_RE, DUE_DATE_RE, DONE_DATE_RE, TAG_RE)
        for m in pattern.finditer(masked)
    ]
    out = _strip_spans(text, spans)
    out = re.sub(r"^~~(.*)~~$", r"\1", out.strip())   # done-item strikethrough
    return re.sub(r"[ \t]+", " ", out).strip()


@dataclass
class ParsedSection:
    """An H2 and the prose under it that belongs to no single task."""

    title: str
    note_target: str | None
    body_note: str | None
    order: int


def parse_backlog(content: str) -> list[ParsedTask]:
    """Parse backlog markdown into task records, preserving structure."""
    return parse_document(content)[0]


def parse_document(content: str) -> tuple[list[ParsedTask], list[ParsedSection]]:
    """Parse into tasks *and* section prose.

    Two returns because the file carries editorial content that is not a task:
    five blockquote lines and two paragraphs under section headings. A render
    that knew only about tasks would delete them from the only copy.
    """
    tasks: list[ParsedTask] = []
    sections: list[ParsedSection] = []
    category = ""
    section: str | None = None
    subsection: str | None = None
    in_code_block = False
    current: ParsedTask | None = None
    pending_note: list[str] = []
    seen_task_in_section = False
    in_body = False

    for line_num, line in enumerate(content.split("\n"), start=1):
        stripped = line.strip()

        # ```tasks blocks are the Obsidian plugin's query lenses over this same
        # list — views, not data. Parsing them would import the query text.
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        if stripped.strip("# ").strip() == BODY_HEADING and stripped.startswith("# "):
            in_body = True
            section = None
            subsection = None
            pending_note = []
            current = None
            continue
        if not in_body:
            continue

        if stripped.startswith("### "):
            subsection = stripped[4:].strip()
            current = None
            continue
        if stripped.startswith("## "):
            _flush_section(sections, section, pending_note)
            section = stripped[3:].strip()
            subsection = None
            pending_note = []
            seen_task_in_section = False
            current = None
            continue
        if stripped.startswith("# "):
            _flush_section(sections, section, pending_note)
            heading = stripped[2:].strip()
            category = "" if heading in NON_CATEGORY_H1 else heading
            section = None
            subsection = None
            pending_note = []
            seen_task_in_section = False
            current = None
            continue

        # Sub-bullets belong to the task above them and carry its constraints.
        if current is not None and SUBBULLET_RE.match(line):
            current.subbullets.append(SUBBULLET_RE.match(line).group(1).strip())
            continue

        if not line.startswith("- ["):
            # Prose before the section's first task belongs to the heading.
            if stripped and section and not seen_task_in_section:
                pending_note.append(stripped)
            # A blank line does not end a task (the file has stray ones mid-
            # list); a non-blank, non-quote line does.
            if stripped and not stripped.startswith(">"):
                current = None
            continue

        match = TASK_RE.match(stripped)
        if not match:
            continue

        checkbox, text = match.groups()
        # A date, tag or glyph inside `code` is quoted prose, not this task's
        # metadata — see _mask_code_spans().
        searchable = _mask_code_spans(text)
        pri = PRIORITY_RE.search(searchable)
        due = DUE_DATE_RE.search(searchable)
        done = DONE_DATE_RE.search(searchable)
        all_tags = TAG_RE.findall(searchable)
        section_links = WIKILINK_RE.findall(section) if section else []

        current = ParsedTask(
            import_key=import_key(text),
            title=clean_title(text),
            completed=checkbox.lower() == "x",
            priority=PRIORITY_RANK.get(pri.group()) if pri else None,
            due_date=due.group(1) if due else None,
            done_date=done.group(1) if done else None,
            category=category,
            section=WIKILINK_RE.sub(r"\1", section).strip() if section else None,
            section_note=section_links[0] if section_links else None,
            subsection=subsection,
            # Every tag, in file order. The status ones ALSO become columns
            # (context, energy) — but filtering them out of the stored list
            # meant a re-render dropped #quick from the line it came from.
            # Deriving a column from a tag must not consume the tag.
            tags=all_tags,
            context=next((v for t, v in CONTEXT_TAGS.items() if t in all_tags), None),
            energy=next((v for t, v in ENERGY_TAGS.items() if t in all_tags), None),
            wikilinks=WIKILINK_RE.findall(text),
            line_number=line_num,
            order=len(tasks),
        )
        tasks.append(current)
        seen_task_in_section = True

    _flush_section(sections, section, pending_note)
    return tasks, sections


def _flush_section(
    sections: list[ParsedSection], title: str | None, note_lines: list[str],
) -> None:
    if not title:
        return
    links = WIKILINK_RE.findall(title)
    sections.append(ParsedSection(
        title=WIKILINK_RE.sub(r"\1", title).strip(),
        note_target=links[0] if links else None,
        body_note="\n".join(note_lines) or None,
        order=len(sections),
    ))
