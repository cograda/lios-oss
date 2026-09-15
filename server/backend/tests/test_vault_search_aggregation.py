"""unit-tier tests for collapsing vault_search results per note.

A result list is a list of *notes*, not chunk offsets. The vault averages ~10
chunks per file, so without aggregation a heavily chunked note answers every
query about its subject with itself, repeatedly: a live query for "comar
architecture plan" returned six hits that were six chunks of one file — one
document where six were asked for.

`vault_similar` already aggregated this way. Search was the inconsistent one.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.integrations.obsidian.tools import handle_search

pytestmark = pytest.mark.unit


def _hit(source_id, score, preview="text", status=None):
    return {
        "source_id": source_id,
        "score": score,
        "preview": preview,
        "metadata": json.dumps({"status": status}) if status else None,
        "created_at": None,
        "space": "gemini-embedding-2",
        "source": "vault",
    }


def _run(hits, arguments):
    session = MagicMock()
    session.query.return_value.filter.return_value.all.return_value = []
    with patch("app.integrations.obsidian.tools.EmbeddingService.search",
               return_value=hits), \
         patch("app.integrations.obsidian.tools.current_user_id", return_value=1):
        return json.loads(handle_search(session, arguments))


class TestPerNoteCollapse:
    def test_chunks_of_one_note_become_one_result(self):
        out = _run(
            [_hit("Task Backlog.md#3", 0.71), _hit("Task Backlog.md#9", 0.83),
             _hit("Task Backlog.md#1", 0.66)],
            {"query": "tasks"},
        )
        assert len(out) == 1
        assert out[0]["path"] == "Task Backlog.md"

    def test_the_best_chunk_supplies_score_and_preview(self):
        out = _run(
            [_hit("A.md#0", 0.60, "weak match"), _hit("A.md#1", 0.90, "strong match")],
            {"query": "x"},
        )
        assert out[0]["score"] == 0.90
        assert out[0]["preview"] == "strong match"

    def test_chunks_matched_counts_the_hits(self):
        """Real signal: one match is a mention, nine is being about the subject."""
        out = _run([_hit("A.md#0", 0.9), _hit("A.md#1", 0.8), _hit("B.md", 0.7)],
                   {"query": "x"})
        by = {r["path"]: r for r in out}
        assert by["A.md"]["chunks_matched"] == 2
        assert by["B.md"]["chunks_matched"] == 1

    def test_results_stay_sorted_by_best_score(self):
        out = _run([_hit("Low.md", 0.10), _hit("High.md#2", 0.95), _hit("Mid.md", 0.50)],
                   {"query": "x"})
        assert [r["path"] for r in out] == ["High.md", "Mid.md", "Low.md"]

    def test_limit_applies_to_notes_not_chunks(self):
        hits = [_hit(f"N{i}.md#{j}", 0.9 - i / 100) for i in range(6) for j in range(3)]
        out = _run(hits, {"query": "x", "limit": 2})
        assert len(out) == 2
        assert len({r["path"] for r in out}) == 2

    def test_fewer_than_limit_is_acceptable_when_one_note_dominates(self):
        """Returning fewer real notes beats padding with the same file again."""
        out = _run([_hit("Only.md#%d" % i, 0.9) for i in range(20)],
                   {"query": "x", "limit": 10})
        assert len(out) == 1

    def test_unchunked_paths_pass_through_unchanged(self):
        out = _run([_hit("Notes/Plain.md", 0.8)], {"query": "x"})
        assert out[0]["path"] == "Notes/Plain.md"
        assert out[0]["chunks_matched"] == 1


class TestOverFetch:
    def test_search_is_asked_for_more_rows_than_the_caller_wants(self):
        """Collapsing after a top-K fetch needs headroom, or K notes is unreachable."""
        session = MagicMock()
        session.query.return_value.filter.return_value.all.return_value = []
        with patch("app.integrations.obsidian.tools.EmbeddingService.search",
                   return_value=[]) as search, \
             patch("app.integrations.obsidian.tools.current_user_id", return_value=1):
            handle_search(session, {"query": "x", "limit": 5})
        assert search.call_args.kwargs["limit"] > 5

    def test_over_fetch_respects_the_services_own_ceiling(self):
        session = MagicMock()
        session.query.return_value.filter.return_value.all.return_value = []
        with patch("app.integrations.obsidian.tools.EmbeddingService.search",
                   return_value=[]) as search, \
             patch("app.integrations.obsidian.tools.current_user_id", return_value=1):
            handle_search(session, {"query": "x", "limit": 50})
        assert search.call_args.kwargs["limit"] <= 50
