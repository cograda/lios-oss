"""unit-tier tests for keying change detection on the embeddable body.

Change detection used to hash the whole file while `chunk_file` embeds only
`strip_frontmatter(content)`. So any frontmatter-only edit looked like a content
change — bumping `modified:`, editing a tag, flipping `status:` — and re-chunked
the file plus ran the enqueue path over every chunk.

It never cost an API call (`EmbeddingService.enqueue` dedups on
`(source, source_id, user_id, content_hash)` and the cleaned text was
identical), which is exactly why it survived: pure wasted work with no bill
attached. `/daily-note` rewrites `modified:` every morning and `Task Backlog.md`
chunks into dozens of rows.

The trade this makes: a frontmatter change no longer re-indexes at all, so
`refresh_status_metadata()` becomes the load-bearing path for `status`.
"""

import pytest

from app.integrations.obsidian.sync import _body_hash, _versioned_hash

pytestmark = pytest.mark.unit

BODY = "# Note\n\nThe body that actually gets embedded.\n"


def _fm(**kw):
    lines = "\n".join(f"{k}: {v}" for k, v in kw.items())
    return f"---\n{lines}\n---\n\n{BODY}"


class TestFrontmatterIsExcluded:
    def test_bumping_modified_does_not_change_the_hash(self):
        a = _fm(title="X", modified="2026-08-14")
        b = _fm(title="X", modified="2026-08-15")
        assert _body_hash(a) == _body_hash(b)

    def test_editing_tags_does_not_change_the_hash(self):
        a = _fm(title="X", tags="[reference, project]")
        b = _fm(title="X", tags="[renovation]")
        assert _body_hash(a) == _body_hash(b)

    def test_flipping_status_does_not_change_the_hash(self):
        a = _fm(title="X", status="active")
        b = _fm(title="X", status="superseded")
        assert _body_hash(a) == _body_hash(b)

    def test_adding_frontmatter_to_a_bare_note_does_not_change_the_hash(self):
        assert _body_hash(BODY) == _body_hash(_fm(title="X"))


class TestBodyChangesStillCount:
    def test_editing_the_body_changes_the_hash(self):
        a = _fm(title="X")
        b = _fm(title="X").replace("actually gets embedded", "was rewritten")
        assert _body_hash(a) != _body_hash(b)

    def test_appending_a_line_changes_the_hash(self):
        a = _fm(title="X")
        assert _body_hash(a) != _body_hash(a + "\nOne more line.\n")

    def test_whitespace_only_body_edits_are_not_ignored(self):
        """strip_frontmatter strips surrounding whitespace, not internal text."""
        a = _fm(title="X")
        b = a.replace("The body", "The  body")
        assert _body_hash(a) != _body_hash(b)


class TestVersionedHashStillFolds:
    def test_chunker_version_is_still_part_of_the_stored_hash(self):
        """A CHUNKER_VERSION bump must still invalidate every file."""
        h = _versioned_hash(_body_hash(_fm(title="X")))
        assert ":cv" in h

    def test_two_notes_with_the_same_body_hash_the_same(self):
        """Identity is the body — a copy under a different name is not new
        content. It still gets its own embedding row, because `source_id` is
        part of the enqueue key; only the *change detection* collapses."""
        assert _body_hash(_fm(title="A")) == _body_hash(_fm(title="B"))
