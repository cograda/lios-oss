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

from app.integrations.obsidian.sync import _chunk_metadata, frontmatter_status

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
        m = json.loads(_chunk_metadata("2026-08-15T00:00:00", ["H1"], "active"))
        assert m["status"] == "active"
        assert m["modified_at"] == "2026-08-15T00:00:00"
        assert m["heading_path"] == ["H1"]

    def test_key_is_absent_rather_than_null_when_there_is_no_status(self):
        """Absent, not null — a JSONB `->> 'status'` IN (...) filter should
        simply not match, without callers having to special-case None."""
        m = json.loads(_chunk_metadata("2026-08-15T00:00:00", [], None))
        assert "status" not in m

    def test_existing_metadata_keys_are_preserved(self):
        """modified_at/heading_path predate this and other code reads them."""
        m = json.loads(_chunk_metadata("2026-08-15T00:00:00", ["A", "B"], "done"))
        assert set(m) == {"modified_at", "heading_path", "status"}


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
