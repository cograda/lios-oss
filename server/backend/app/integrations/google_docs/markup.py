"""Markdown <-> Google Docs conversion, both directions.

Two independent halves, deliberately asymmetric because the two directions
have completely different mechanics:

`markdown_to_html()` — the *write* direction. Google's Drive importer turns
uploaded HTML into a native Google Doc (headings become real Heading styles,
`<table>` becomes a real table, `<ul>` becomes real bullets), so producing
HTML is the whole job. This is why there is no index arithmetic anywhere in
this package's write path: the alternative — building the same document with
Docs API `batchUpdate` requests — means tracking a character offset that
every insertion shifts.

`document_to_markdown()` — the *read* direction. Here the Docs API is the
right tool, because it works on any document the account can see, including
ones a human created by hand (Drive's export-to-markdown is a `drive.file`
call and so only reaches documents comar created). It walks the
`documents.get` response's `body.content` and flattens it.

Neither direction is a general-purpose markdown implementation, and it is
not trying to be. It covers what household documents actually contain:
headings, paragraphs, bold/italic/code/strikethrough, links, nested
bullet/numbered lists, blockquotes, fenced code, pipe tables, and rules.
Anything else passes through as literal text rather than being silently
dropped.

No markdown library is used because `server/backend/requirements.txt` has
none, and the root CLAUDE.md's bar for adding a dependency is high — comar
vendors coglib into every deployed image, and a new dep here would ship to
production for one conversion function.
"""

from __future__ import annotations

import html
import re
from typing import Any

# ---------------------------------------------------------------------------
# markdown -> HTML (the write direction)
# ---------------------------------------------------------------------------

_CODE_SPAN = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"(?:\*\*|__)(.+?)(?:\*\*|__)")
_ITALIC = re.compile(r"(?<![\*\w])[\*_]([^\*_]+)[\*_](?![\*\w])")
_STRIKE = re.compile(r"~~(.+?)~~")

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HR = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_UL_ITEM = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OL_ITEM = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_FENCE = re.compile(r"^\s*```+\s*(\S*)\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")

# Inline monospace: `<code>` alone is unreliable through the Drive importer,
# an explicit font-family span is not.
_MONO = "font-family:'Courier New',monospace"
_CELL = "border:1px solid #999999;padding:6px;vertical-align:top"

_PLACEHOLDER = "\x00{}\x00"


def _inline(text: str) -> str:
    """Convert inline markdown in one run of text to HTML.

    Code spans are pulled out first and reinserted last so their contents
    are never re-scanned for bold/italic — ``**`` inside `` `code` `` is
    literal, which is what every markdown implementation does and what a
    document full of shell snippets depends on.
    """
    stash: list[str] = []

    def _stash(match: re.Match) -> str:
        stash.append(match.group(1))
        return _PLACEHOLDER.format(len(stash) - 1)

    text = _CODE_SPAN.sub(_stash, text)
    text = html.escape(text, quote=False)

    text = _LINK.sub(
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        text,
    )
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)

    for index, raw in enumerate(stash):
        text = text.replace(
            _PLACEHOLDER.format(index),
            f'<span style="{_MONO}">{html.escape(raw, quote=False)}</span>',
        )
    return text


def _split_row(line: str) -> list[str]:
    """Split one pipe-table row into cells, tolerating optional edge pipes."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


class _Builder:
    """Accumulates HTML while tracking which blocks are currently open.

    The list handling is the only non-obvious part. A nested list must be
    written *inside* its parent `<li>`, not after it — `<ol><li>a</li><ul>…`
    is invalid HTML, and while most importers indent it anyway, "most" is
    not a contract worth relying on for a document someone else will read.
    So each open level records whether its `<li>` is still open, and an
    `<li>` is closed only when the next sibling arrives or the level itself
    closes. That is what makes a child list land inside the parent item.
    """

    def __init__(self) -> None:
        self.out: list[str] = []
        # (indent, tag, is_li_open) per open list level, outermost first.
        self._lists: list[list] = []
        self._para: list[str] = []
        self._quote: list[str] = []

    # -- paragraph / quote accumulation ------------------------------------
    def flush_para(self) -> None:
        if self._para:
            self.out.append(f"<p>{_inline(' '.join(self._para))}</p>")
            self._para = []

    def flush_quote(self) -> None:
        if self._quote:
            body = _inline(" ".join(self._quote))
            self.out.append(f"<blockquote><p>{body}</p></blockquote>")
            self._quote = []

    def close_lists(self, to_indent: int = -1) -> None:
        """Close every open level deeper than `to_indent`, innermost first."""
        while self._lists and self._lists[-1][0] > to_indent:
            _, tag, li_open = self._lists.pop()
            if li_open:
                self.out.append("</li>")
            self.out.append(f"</{tag}>")

    def flush_all(self) -> None:
        self.flush_para()
        self.flush_quote()
        self.close_lists()

    def add_quote_line(self, text: str) -> None:
        self._quote.append(text)

    def add_para_line(self, text: str) -> None:
        self._para.append(text)

    # -- blocks ------------------------------------------------------------
    def add_list_item(self, indent: int, tag: str, text: str) -> None:
        self.flush_para()
        self.flush_quote()
        self.close_lists(indent)

        if not self._lists or self._lists[-1][0] < indent:
            # Opening a nested level. The parent's <li> stays open on
            # purpose, so this list is written inside it.
            self.out.append(f"<{tag}>")
            self._lists.append([indent, tag, False])
        elif self._lists[-1][1] != tag:
            # Same indent, different marker (bullets -> numbers): close the
            # old list and start the right kind rather than emitting an <li>
            # into a list of the wrong type.
            _, old_tag, li_open = self._lists.pop()
            if li_open:
                self.out.append("</li>")
            self.out.append(f"</{old_tag}>")
            self.out.append(f"<{tag}>")
            self._lists.append([indent, tag, False])

        level = self._lists[-1]
        if level[2]:
            self.out.append("</li>")
        self.out.append(f"<li>{_inline(text)}")
        level[2] = True


def markdown_to_html(markdown: str, *, title: str | None = None) -> str:
    """Render markdown as HTML suitable for Drive's Google Docs importer.

    `title`, if given, is emitted as an `<h1>` at the top *and* as the
    document's `<title>`. Pass it only when the caller wants a visible
    heading — `writer.py` does not, because the Drive file name already
    carries the title and a duplicated one reads as a mistake in the doc.
    """
    builder = _Builder()
    lines = markdown.replace("\r\n", "\n").split("\n")
    index = 0

    if title:
        builder.out.append(f"<h1>{_inline(title)}</h1>")

    while index < len(lines):
        line = lines[index]

        fence = _FENCE.match(line)
        if fence:
            builder.flush_all()
            index += 1
            block: list[str] = []
            while index < len(lines) and not _FENCE.match(lines[index]):
                block.append(lines[index])
                index += 1
            index += 1  # consume the closing fence (or fall off the end)
            body = html.escape("\n".join(block), quote=False)
            builder.out.append(f'<pre style="{_MONO}">{body}</pre>')
            continue

        if not line.strip():
            builder.flush_para()
            builder.flush_quote()
            builder.close_lists()
            index += 1
            continue

        if _HR.match(line):
            builder.flush_all()
            builder.out.append("<hr>")
            index += 1
            continue

        heading = _HEADING.match(line)
        if heading:
            builder.flush_all()
            level = len(heading.group(1))
            builder.out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            index += 1
            continue

        # Table: a row of pipes whose *next* line is the separator. Checking
        # the separator is what stops a paragraph that merely contains a "|"
        # from being read as a table.
        if "|" in line and index + 1 < len(lines) and _TABLE_SEP.match(lines[index + 1]):
            builder.flush_all()
            header = _split_row(line)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_split_row(lines[index]))
                index += 1
            builder.out.append('<table style="border-collapse:collapse">')
            builder.out.append("<tr>" + "".join(
                f'<th style="{_CELL}">{_inline(cell)}</th>' for cell in header
            ) + "</tr>")
            for row in rows:
                # Pad/trim to the header width so a ragged row can't shift
                # every following cell one column left.
                cells = (row + [""] * len(header))[: len(header)]
                builder.out.append("<tr>" + "".join(
                    f'<td style="{_CELL}">{_inline(cell)}</td>' for cell in cells
                ) + "</tr>")
            builder.out.append("</table>")
            continue

        ordered = _OL_ITEM.match(line)
        unordered = _UL_ITEM.match(line)
        if ordered or unordered:
            match = ordered or unordered
            assert match is not None
            builder.add_list_item(
                len(match.group(1).expandtabs(4)),
                "ol" if ordered else "ul",
                match.group(2),
            )
            index += 1
            continue

        quote = _QUOTE.match(line)
        if quote:
            builder.flush_para()
            builder.close_lists()
            builder.add_quote_line(quote.group(1))
            index += 1
            continue

        builder.flush_quote()
        builder.add_para_line(line.strip())
        index += 1

    builder.flush_all()
    body = "\n".join(builder.out)
    head = f"<title>{html.escape(title or '', quote=False)}</title>" if title else ""
    return f"<!DOCTYPE html><html><head><meta charset=\"utf-8\">{head}</head><body>{body}</body></html>"


# ---------------------------------------------------------------------------
# Google Docs document -> markdown (the read direction)
# ---------------------------------------------------------------------------

_NAMED_STYLE_PREFIX = {
    "TITLE": "# ",
    "SUBTITLE": "",
    "HEADING_1": "# ",
    "HEADING_2": "## ",
    "HEADING_3": "### ",
    "HEADING_4": "#### ",
    "HEADING_5": "##### ",
    "HEADING_6": "###### ",
}


def _run_to_markdown(element: dict[str, Any]) -> str:
    """One `textRun` back to markdown, re-applying its character styling."""
    run = element.get("textRun")
    if not run:
        # Non-text inline elements: an inline image, a footnote reference, a
        # horizontal rule, a page break. There is no text to recover, so say
        # so rather than silently returning "" — a reader diffing the
        # markdown against the doc needs to know something was there.
        for kind, label in (
            ("inlineObjectElement", "[image]"),
            ("footnoteReference", "[footnote]"),
            ("horizontalRule", "\n---\n"),
        ):
            if kind in element:
                return label
        return ""

    text = run.get("content", "")
    # Trailing newlines are structural (they terminate the paragraph), so
    # style markers have to go inside them or the markdown breaks.
    stripped = text.rstrip("\n")
    trailing = text[len(stripped):]
    if not stripped.strip():
        return text

    style = run.get("textStyle", {})
    font = (style.get("weightedFontFamily") or {}).get("fontFamily", "")
    if "Courier" in font or "Mono" in font:
        stripped = f"`{stripped}`"
    else:
        if style.get("bold"):
            stripped = f"**{stripped}**"
        if style.get("italic"):
            stripped = f"*{stripped}*"
    if style.get("strikethrough"):
        stripped = f"~~{stripped}~~"
    url = (style.get("link") or {}).get("url")
    if url:
        stripped = f"[{stripped}]({url})"
    return stripped + trailing


def _bullet_prefix(doc: dict[str, Any], bullet: dict[str, Any]) -> str:
    """Markdown list marker for a paragraph's bullet, at its nesting level.

    Ordered vs unordered is not on the paragraph — it is on the *list*, in
    `doc["lists"][listId]`, per nesting level. A level with a `glyphType` is
    numbered; one with a `glyphSymbol` is a bullet. Getting this from the
    paragraph alone is impossible, which is why the whole document is
    threaded through here.
    """
    level = bullet.get("nestingLevel", 0)
    indent = "  " * level
    list_id = bullet.get("listId")
    levels = (
        ((doc.get("lists") or {}).get(list_id) or {})
        .get("listProperties", {})
        .get("nestingLevels", [])
    )
    glyph = levels[level] if level < len(levels) else {}
    glyph_type = glyph.get("glyphType", "")
    if glyph_type and glyph_type != "GLYPH_TYPE_UNSPECIFIED":
        return f"{indent}1. "
    return f"{indent}- "


def _paragraph_to_markdown(doc: dict[str, Any], paragraph: dict[str, Any]) -> str:
    text = "".join(_run_to_markdown(el) for el in paragraph.get("elements", []))
    text = text.rstrip("\n")
    if not text.strip():
        return ""

    bullet = paragraph.get("bullet")
    if bullet is not None:
        return _bullet_prefix(doc, bullet) + text.strip()

    style = paragraph.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
    return _NAMED_STYLE_PREFIX.get(style, "") + text


def _table_to_markdown(doc: dict[str, Any], table: dict[str, Any]) -> str:
    """Flatten a Docs table to a GFM pipe table.

    The first row is treated as the header because Docs has no header-row
    concept to read — a table's first row is the header by convention, and
    guessing from cell styling would be worse than a stated convention.
    """
    rows: list[list[str]] = []
    for table_row in table.get("tableRows", []):
        cells: list[str] = []
        for cell in table_row.get("tableCells", []):
            parts = [
                _paragraph_to_markdown(doc, item["paragraph"])
                for item in cell.get("content", [])
                if "paragraph" in item
            ]
            # A pipe inside a cell would break the row; escape it.
            cells.append(" ".join(p for p in parts if p).replace("|", "\\|"))
        rows.append(cells)

    if not rows:
        return ""
    width = max(len(row) for row in rows)
    lines = []
    for position, row in enumerate(rows):
        padded = row + [""] * (width - len(row))
        lines.append("| " + " | ".join(padded) + " |")
        if position == 0:
            lines.append("|" + "|".join([" --- "] * width) + "|")
    return "\n".join(lines)


def document_to_markdown(doc: dict[str, Any]) -> str:
    """Flatten a `documents.get` response to markdown.

    Blank paragraphs collapse to a single blank line rather than being
    preserved one-for-one — Docs is full of empty spacer paragraphs that
    would otherwise dominate the output.
    """
    blocks: list[str] = []
    for item in (doc.get("body", {}) or {}).get("content", []):
        if "paragraph" in item:
            rendered = _paragraph_to_markdown(doc, item["paragraph"])
            if rendered:
                blocks.append(rendered)
        elif "table" in item:
            rendered = _table_to_markdown(doc, item["table"])
            if rendered:
                blocks.append(rendered)
        elif "tableOfContents" in item:
            blocks.append("<!-- table of contents -->")

    out: list[str] = []
    for block in blocks:
        # Keep consecutive list items adjacent (a blank line between them
        # splits one list into several in most renderers); separate
        # everything else with a blank line.
        is_item = block.lstrip().startswith(("- ", "1. "))
        prev_is_item = bool(out) and out[-1].lstrip().startswith(("- ", "1. "))
        if out and not (is_item and prev_is_item):
            out.append("")
        out.append(block)
    return "\n".join(out).strip() + "\n"


def end_index(doc: dict[str, Any]) -> int:
    """The insertion index for appending to the end of a document's body.

    A Docs body's last structural element ends at an index one past the
    final newline, and inserting *at* that index is rejected — content has
    to go at `endIndex - 1`. This is the only index arithmetic in the
    package, and it is deliberately the only place that knows the rule.
    """
    content = (doc.get("body", {}) or {}).get("content", [])
    if not content:
        return 1
    return max(1, int(content[-1].get("endIndex", 2)) - 1)
