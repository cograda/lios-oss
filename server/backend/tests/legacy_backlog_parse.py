"""Test-only relic of the deterministic `Task Backlog.md` parser.

Moved out of `app/integrations/tasks/parse.py` on 2026-09-15 when the ledger
became the tasks system's sole source of truth and nothing in production may
read `Task Backlog.md` as input any more (see `server/CLAUDE.md`'s Known
Issues and `core/CLAUDE.md`'s one-way-render note). The one-off markdown
importer this fed is gone with it — Alex: "we can always batch import
through Claude" (`tasks_add` calls) — but a large slice of the existing test
suite (`test_tasks_render.py`, `test_tasks_tools.py`, `test_tasks_dupes.py`,
`test_tasks_loops.py`) uses `import_backlog`/`import_someday`/
`import_delegated` purely as a convenient way to seed a realistic set of
ledger rows from a markdown fixture string, not to test the importer
feature itself (that coverage lived in `test_tasks_import.py` and
`test_tasks_import_foldins.py`, both deleted the same day).

So this file and `legacy_backlog_importer.py` are verbatim copies of the
deleted production modules, kept ONLY as test fixtures — not imported by any
`app/` code, not reachable via any tool or route. Do not add new callers
outside `tests/`; a test that wants to create tasks going forward should call
`tasks_add_handler` instead, the same as production does.
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
    """Blank the contents of `inline code`, keeping length and the backticks."""
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
    """Content-addressed key over the *normalised* task text."""
    norm = _mask_code_spans(text)
    norm = PRIORITY_RE.sub("", norm)
    norm = TAG_RE.sub("", norm)
    norm = DUE_DATE_RE.sub("", norm)
    norm = DONE_DATE_RE.sub("", norm)
    norm = re.sub(r"[^\w\s]", "", norm)
    norm = re.sub(r"\s+", " ", norm).strip().lower()
    return "T-" + hashlib.sha1(norm.encode()).hexdigest()[:10]


def clean_title(text: str) -> str:
    """Strip *metadata* from a task line, and nothing else."""
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
    """Parse into tasks *and* section prose."""
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


def parse_grouped_list(content: str) -> list[ParsedTask]:
    """Parse the shape shared by `Someday.md`, `Delegated Tasks.md` and
    `Delegated Tasks - Done.md`: an H1 title, then H2 (and sometimes H3)
    group headings, then `- [ ]`/`- [x]` items with optional indented
    sub-bullets."""
    tasks: list[ParsedTask] = []
    in_code_block = False
    seen_h1 = False
    h2: str | None = None
    h3: str | None = None
    current: ParsedTask | None = None

    for line_num, line in enumerate(content.split("\n"), start=1):
        stripped = line.strip()

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        if stripped.startswith("# "):
            seen_h1 = True
            current = None
            continue
        if not seen_h1:
            continue

        if stripped.startswith("### "):
            h3 = stripped[4:].strip()
            current = None
            continue
        if stripped.startswith("## "):
            h2 = stripped[3:].strip()
            h3 = None
            current = None
            continue

        if current is not None and SUBBULLET_RE.match(line):
            current.subbullets.append(SUBBULLET_RE.match(line).group(1).strip())
            continue

        if not line.startswith("- ["):
            # A blank line does not end a task (mirrors parse_document); a
            # non-blank, non-blockquote line does.
            if stripped and not stripped.startswith(">"):
                current = None
            continue

        match = TASK_RE.match(stripped)
        if not match:
            continue

        checkbox, text = match.groups()
        searchable = _mask_code_spans(text)
        pri = PRIORITY_RE.search(searchable)
        due = DUE_DATE_RE.search(searchable)
        done = DONE_DATE_RE.search(searchable)
        all_tags = TAG_RE.findall(searchable)
        group = h3 or h2
        group_links = WIKILINK_RE.findall(group) if group else []

        current = ParsedTask(
            import_key=import_key(text),
            title=clean_title(text),
            completed=checkbox.lower() == "x",
            priority=PRIORITY_RANK.get(pri.group()) if pri else None,
            due_date=due.group(1) if due else None,
            done_date=done.group(1) if done else None,
            category="",
            section=WIKILINK_RE.sub(r"\1", group).strip() if group else None,
            section_note=group_links[0] if group_links else None,
            subsection=None,
            tags=all_tags,
            context=next((v for t, v in CONTEXT_TAGS.items() if t in all_tags), None),
            energy=next((v for t, v in ENERGY_TAGS.items() if t in all_tags), None),
            wikilinks=WIKILINK_RE.findall(text),
            line_number=line_num,
            order=len(tasks),
        )
        tasks.append(current)

    return tasks
