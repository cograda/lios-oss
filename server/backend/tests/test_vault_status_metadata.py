"""unit-tier tests for indexing a note's frontmatter `status`.

Status is filterable *without* being embedded. It rides `metadata_json` on the
`embeddings` row, never `chunk_text`, because putting "status: active" into the
vector would let a note's lifecycle bleed into its semantic position — which is
not what anyone means by similarity. The column already existed and vectors live
in separate per-space tables, so this cost no migration and no re-embedding.

The behavioural decision these tests pin: `vault_search` **labels rather than
hides**. It does not exclude done/superseded by default. A search that silently
drops results produces confidently wrong "there is nothing about that" answers —
the same failure mode as the `.stversions` bug, which was damaging precisely
because it was invisible.
"""

import json

import pytest

from app.integrations.obsidian.sync import (
    _chunk_metadata,
    frontmatter_status,
    is_history_chunk,
)

pytestmark = pytest.mark.unit


class TestFrontmatterStatus:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("---\nstatus: active\n---\nbody", "active"),
            ('---\ntitle: X\nstatus: "Shipped"\n---\n', "shipped"),
            ("---\nstatus: 'done'\n---\n", "done"),
            ("---\nstatus:   next   \n---\n", "next"),
            ("---\nstatus: 🧊 parked\n---\n", "🧊 parked"),
        ],
    )
    def test_reads_the_value(self, raw, expected):
        assert frontmatter_status(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "no frontmatter here\nstatus: active\n",
            "---\ntitle: X\n---\n\nstatus: active mentioned in prose\n",
            "---\ntitle: X\n---\n\n```yaml\nstatus: active\n```\n",
            "---\ntitle: X\n---\n",
        ],
    )
    def test_returns_none_when_there_is_no_frontmatter_status(self, raw):
        assert frontmatter_status(raw) is None

    def test_a_status_in_prose_cannot_be_mistaken_for_frontmatter(self):
        """The regex is anchored to the leading block, not any `status:` line."""
        assert frontmatter_status("---\ntitle: X\n---\nstatus: superseded") is None

    def test_unknown_values_are_recorded_not_rejected(self):
        """The index records what the note says; vocabulary is a vault convention.

        Silently dropping an unrecognised value would hide exactly the drift
        the convention exists to catch — the vault had 16 distinct status
        values when this was written.
        """
        assert frontmatter_status("---\nstatus: sent-for-review\n---\n") == "sent-for-review"


class TestChunkMetadata:
    def test_status_is_included_when_present(self):
        m = json.loads(_chunk_metadata("2026-08-15T00:00:00", ["H1"], "active", False))
        assert m["status"] == "active"
        assert m["modified_at"] == "2026-08-15T00:00:00"
        assert m["heading_path"] == ["H1"]

    def test_key_is_absent_rather_than_null_when_there_is_no_status(self):
        """Absent, not null — a JSONB `->> 'status'` IN (...) filter should
        simply not match, without callers having to special-case None."""
        m = json.loads(_chunk_metadata("2026-08-15T00:00:00", [], None, False))
        assert "status" not in m

    def test_existing_metadata_keys_are_preserved(self):
        """modified_at/heading_path predate this and other code reads them."""
        m = json.loads(_chunk_metadata("2026-08-15T00:00:00", ["A", "B"], "done", True))
        assert set(m) == {"modified_at", "source_date", "heading_path", "status", "is_history"}

    def test_source_date_mirrors_modified_at(self):
        """R4: a new key alongside `modified_at`, not a rename — nothing
        reading `modified_at` today should need to change."""
        m = json.loads(_chunk_metadata("2026-08-15T00:00:00", [], None, False))
        assert m["source_date"] == m["modified_at"] == "2026-08-15T00:00:00"

    def test_is_history_flag_is_carried_through(self):
        m = json.loads(_chunk_metadata("2026-08-15T00:00:00", [], "done", True))
        assert m["is_history"] is True
        m2 = json.loads(_chunk_metadata("2026-08-15T00:00:00", [], "active", False))
        assert m2["is_history"] is False


class TestIsHistoryChunk:
    """R4: which chunks count as a superseded/archived record."""

    @pytest.mark.parametrize("status", ["done", "superseded"])
    def test_history_statuses(self, status):
        assert is_history_chunk("Notes/whatever.md", status) is True

    @pytest.mark.parametrize("status", ["active", "next", "parked", None])
    def test_non_history_statuses(self, status):
        assert is_history_chunk("Notes/whatever.md", status) is False

    @pytest.mark.parametrize("path", [
        "Archive/2024/old-plan.md",
        "Projects/lios/Archive/superseded.md",
        ".stversions/Notes/whatever.md",
    ])
    def test_history_path_prefixes_win_regardless_of_status(self, path):
        assert is_history_chunk(path, "active") is True

    def test_ordinary_path_and_status_is_not_history(self):
        assert is_history_chunk("Projects/lios/Plan.md", "active") is False


class TestSearchLabelsRatherThanHides:
    def test_status_is_not_a_required_argument(self):
        """Default behaviour must be unfiltered — see the module docstring."""
        from app.integrations.obsidian import ObsidianIntegration

        tool = [t for t in ObsidianIntegration().mcp_tools()
                if t["name"] == "vault_search"][0]
        assert "status" in tool["inputSchema"]["properties"]
        assert "status" not in tool["inputSchema"].get("required", [])

    def test_every_result_carries_a_status_field(self):
        """Even when None — the caller must be able to tell 'unset' from 'stale'."""
        from pathlib import Path
        import app.integrations.obsidian.tools as mod

        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert '"status": _status_of(r)' in src
