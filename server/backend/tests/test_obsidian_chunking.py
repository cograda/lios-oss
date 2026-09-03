"""Unit tests for `app.integrations.obsidian.chunking` — pure functions, no DB.

Covers the boundary rule (heading + top-level bullet), nested-bullet
grouping (children stay with their parent), the merge floor / split
ceiling, the whole-file threshold (small/prose files keep the old
single-chunk behaviour), and heading-path context on every chunk.
"""

from app.integrations.obsidian.chunking import (
    CHUNK_THRESHOLD_CHARS,
    MAX_CHUNK_CHARS,
    MIN_CHUNK_CHARS,
    chunk_file,
    should_chunk,
    strip_frontmatter,
)

FRONTMATTER = "---\ntitle: X\ntags: [a, b]\n---\n"


# ---------------------------------------------------------------------------
# strip_frontmatter
# ---------------------------------------------------------------------------

class TestStripFrontmatter:
    def test_strips_leading_frontmatter(self):
        content = FRONTMATTER + "# Heading\n\nbody"
        assert strip_frontmatter(content) == "# Heading\n\nbody"

    def test_no_frontmatter_passes_through(self):
        content = "# Heading\n\nbody"
        assert strip_frontmatter(content) == content

    def test_unterminated_frontmatter_passes_through(self):
        # No closing '---' — don't eat the whole file.
        content = "---\ntitle: X\nbody without a closing fence"
        assert strip_frontmatter(content) == content


# ---------------------------------------------------------------------------
# should_chunk — the size/structure threshold
# ---------------------------------------------------------------------------

class TestShouldChunk:
    def test_short_file_never_chunks(self):
        content = "\n".join(f"- item {i}" for i in range(20))
        assert len(content) < CHUNK_THRESHOLD_CHARS
        assert should_chunk(content) is False

    def test_long_prose_file_does_not_chunk(self):
        # Long, but no bullets — a meeting note / daily note shape.
        content = ("This is a long paragraph of prose. " * 200)
        assert len(content) > CHUNK_THRESHOLD_CHARS
        assert should_chunk(content) is False

    def test_long_file_with_few_bullets_does_not_chunk(self):
        # Long and has *some* bullets, but under MIN_BULLET_LINES.
        content = ("Prose padding. " * 200) + "\n- one\n- two\n"
        assert len(content) > CHUNK_THRESHOLD_CHARS
        assert should_chunk(content) is False

    def test_long_list_structured_file_chunks(self):
        content = "\n".join(f"- [ ] task number {i} with some detail text" for i in range(80))
        assert len(content) > CHUNK_THRESHOLD_CHARS
        assert should_chunk(content) is True

    def test_indented_bullets_dont_count_toward_threshold(self):
        # Only *top-level* (unindented) bullets count — three top bullets,
        # each with many indented children, should still qualify (it's the
        # indented children that make the file long).
        blocks = []
        for i in range(3):
            blocks.append(f"- [ ] item {i}")
            blocks.extend(f"  - detail {i}.{j} " + ("x" * 40) for j in range(15))
        content = "\n".join(blocks)
        assert len(content) > CHUNK_THRESHOLD_CHARS
        assert should_chunk(content) is True


# ---------------------------------------------------------------------------
# chunk_file — whole-file fallback path
# ---------------------------------------------------------------------------

class TestWholeFileFallback:
    def test_small_file_returns_single_bare_path_chunk(self):
        content = FRONTMATTER + "# Note\n\n- [ ] one thing to do\n"
        chunks = chunk_file("Notes/small.md", content)
        assert len(chunks) == 1
        assert chunks[0].source_id == "Notes/small.md"
        assert chunks[0].heading_path == []

    def test_whole_file_text_includes_path_and_body(self):
        content = "# Note\n\nsome body text"
        chunks = chunk_file("Notes/small.md", content)
        assert chunks[0].text.startswith("Notes/small.md\n\n")
        assert "some body text" in chunks[0].text

    def test_whole_file_strips_frontmatter(self):
        content = FRONTMATTER + "body only"
        chunks = chunk_file("x.md", content)
        assert "title: X" not in chunks[0].text
        assert "body only" in chunks[0].text

    def test_long_prose_file_stays_whole(self):
        content = "# Meeting\n\n" + ("Discussion point in prose form. " * 200)
        chunks = chunk_file("Meetings/x.md", content)
        assert len(chunks) == 1
        assert chunks[0].source_id == "Meetings/x.md"


# ---------------------------------------------------------------------------
# chunk_file — section/bullet boundary rule
# ---------------------------------------------------------------------------

def _big_list_file(n_headings: int = 3, items_per_heading: int = 10) -> str:
    lines = ["# Backlog"]
    for h in range(n_headings):
        lines.append(f"\n## Section {h}")
        for i in range(items_per_heading):
            lines.append(
                f"- [ ] **Task {h}-{i}** — a moderately long description of "
                f"the work involved, enough text to be realistic #tag{h}"
            )
    return "\n".join(lines)


class TestSectionBulletChunking:
    def test_multi_chunk_when_qualifying(self):
        content = _big_list_file()
        assert should_chunk(content)
        chunks = chunk_file("Task Backlog.md", content)
        assert len(chunks) > 1

    def test_chunk_source_ids_are_indexed_and_unique(self):
        content = _big_list_file()
        chunks = chunk_file("Task Backlog.md", content)
        ids = [c.source_id for c in chunks]
        assert ids == [f"Task Backlog.md#{i}" for i in range(len(chunks))]
        assert len(set(ids)) == len(ids)

    def test_every_chunk_carries_heading_breadcrumb(self):
        content = _big_list_file()
        chunks = chunk_file("Task Backlog.md", content)
        for c in chunks:
            assert c.text.startswith("Task Backlog.md")
            if c.heading_path:
                assert " > ".join(["Task Backlog.md", *c.heading_path]) in c.text.split("\n\n")[0]

    def test_nested_children_stay_with_parent_bullet(self):
        content = (
            "# Backlog\n\n"
            "## Home\n\n"
            + "- [ ] padding item " + ("x" * 600) + "\n"
            + "- [ ] padding item 2 " + ("y" * 600) + "\n"
            "- [ ] **Fix bedroom sensor** — over-reports heat\n"
            "  - Promoted to high priority on 6 Aug because it blocks a medical question.\n"
            "  - Same enclosure problem as other boards in the fleet.\n"
            + "- [ ] padding item 3 " + ("z" * 600) + "\n"
        )
        assert should_chunk(content)
        chunks = chunk_file("Task Backlog.md", content)
        # The parent bullet and both its indented children must land in the
        # SAME chunk, never split across chunks.
        matches = [c for c in chunks if "Fix bedroom sensor" in c.text]
        assert len(matches) == 1
        chunk = matches[0]
        assert "Promoted to high priority" in chunk.text
        assert "Same enclosure problem" in chunk.text

    def test_heading_hierarchy_produces_nested_breadcrumb(self):
        content = (
            "# Backlog — by category\n\n"
            "# Home\n\n"
            "## [[Malahide House]]\n\n"
            + "\n".join(
                f"- [ ] **Item {i}** — long enough description text here to add bulk"
                for i in range(40)
            )
        )
        assert should_chunk(content)
        chunks = chunk_file("Task Backlog.md", content)
        assert any(c.heading_path == ["Home", "[[Malahide House]]"] for c in chunks)
        target = next(c for c in chunks if c.heading_path == ["Home", "[[Malahide House]]"])
        assert target.text.startswith("Task Backlog.md > Home > [[Malahide House]]")

    def test_tiny_bullets_are_merged_up_to_the_floor(self):
        # Many short bullets under one heading — none alone would clear
        # MIN_CHUNK_CHARS, so consecutive ones must be packed together.
        content = (
            "# Backlog\n\n## Chores\n\n"
            + "\n".join(f"- [ ] chore number {i} needs doing" for i in range(80))
        )
        assert should_chunk(content)
        chunks = chunk_file("Task Backlog.md", content)
        chores_chunks = [c for c in chunks if c.heading_path == ["Backlog", "Chores"]]
        assert chores_chunks, "expected at least one Chores chunk"
        # None of the merged chunks should be a single 4-char bullet on its own
        # (unless it's the very last remainder in the section).
        small_singleton_chunks = [
            c for c in chores_chunks if c.text.count("- [ ]") == 1
        ]
        assert len(small_singleton_chunks) <= 1

    def test_oversized_single_bullet_kept_whole_not_split(self):
        # One bullet whose own children exceed MAX_CHUNK_CHARS must still be
        # emitted as a single chunk — never split mid-item.
        big_children = "\n".join(
            f"  - detail line {i} " + ("z" * 60) for i in range(40)
        )
        content = (
            "# Backlog\n\n## Section\n\n"
            "- [ ] padding\n"
            f"- [ ] **Huge item**\n{big_children}\n"
            "- [ ] more padding " + ("q" * 300) + "\n"
        )
        assert len(content) > CHUNK_THRESHOLD_CHARS
        chunks = chunk_file("Task Backlog.md", content)
        huge = [c for c in chunks if "Huge item" in c.text]
        assert len(huge) == 1
        assert "detail line 39" in huge[0].text  # last child present, not truncated

    def test_chunks_dont_wildly_exceed_ceiling(self):
        content = _big_list_file(n_headings=2, items_per_heading=30)
        chunks = chunk_file("Task Backlog.md", content)
        # Allow one item's own size above the ceiling (single-block chunks
        # are never split), but packed multi-block chunks should respect it.
        for c in chunks:
            body = c.text.split("\n\n", 1)[1] if "\n\n" in c.text else c.text
            if body.count("- [ ]") > 1:
                assert len(body) <= MAX_CHUNK_CHARS + 200  # small slack for join overhead

    def test_blocks_never_merge_across_heading_boundary(self):
        content = (
            "# Backlog\n\n"
            "## Section A\n\n- [ ] a\n"
            "## Section B\n\n- [ ] b\n"
        )
        chunks = chunk_file("x.md", content) if should_chunk(content) else None
        # Force through the chunker directly regardless of the whole-file
        # threshold, to check the merge rule in isolation.
        from app.integrations.obsidian.chunking import _pack_blocks, _parse_blocks
        blocks = _parse_blocks(content)
        packed = _pack_blocks(blocks)
        heading_paths = [hp for hp, _ in packed]
        assert ("Backlog", "Section A") in heading_paths
        assert ("Backlog", "Section B") in heading_paths
        # No packed chunk should contain text from both sections.
        for _, text in packed:
            assert not ("- [ ] a" in text and "- [ ] b" in text)


# ---------------------------------------------------------------------------
# Fenced code blocks are opaque
# ---------------------------------------------------------------------------

class TestCodeFences:
    def test_headings_inside_code_fence_are_not_parsed(self):
        content = (
            "# Real Heading\n\n"
            "- [ ] item one\n"
            "  ```\n"
            "  # not a real heading\n"
            "  - not a real bullet\n"
            "  ```\n"
        )
        from app.integrations.obsidian.chunking import _parse_blocks
        blocks = _parse_blocks(content)
        # Only one heading context should ever appear.
        heading_paths = {hp for hp, _ in blocks}
        assert heading_paths == {("Real Heading",)}
