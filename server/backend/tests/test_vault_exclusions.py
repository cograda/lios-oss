"""unit-tier tests for which vault paths are indexable.

`.stversions` — Syncthing's rolling snapshot of every save — was indexed for
months because it was missing from `SKIP_DIRS`. The damage was not merely a
bigger bill: a snapshot outranked the live file it was a snapshot of in a real
search (0.7651 vs 0.7600, measured 2026-08-13), so search was preferring a
record of past belief over current truth.

These tests pin the exclusion itself, and — more importantly — the shape that
stops the next one recurring: a single predicate rather than the rule
open-coded at each call site.
"""

from pathlib import Path

import pytest

from app.integrations.obsidian.sync import SKIP_DIRS, is_indexable

pytestmark = pytest.mark.unit


class TestIsIndexable:
    @pytest.mark.parametrize(
        "path",
        [
            "Task Backlog.md",
            "Daily Notes/Alex/2026-08-14.md",
            "Household/Renovation/Comar House.md",
            "Projects/lios/Backlog.md",
        ],
    )
    def test_real_vault_paths_are_indexable(self, path):
        assert is_indexable(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            ".stversions/Task Backlog~20260801-112211.md",
            ".stversions/Reference/Sam — Electronics~20260801-112211.md",
            ".obsidian/plugins/notes.md",
            ".trash/deleted.md",
            "Templates/Daily.md",
            "Attachments/scan.md",
        ],
    )
    def test_excluded_directories_are_not_indexable(self, path):
        assert is_indexable(path) is False

    def test_exclusion_applies_at_any_depth(self):
        """The directory need not be the first segment.

        Syncthing creates `.stversions` at the sync root, but a nested vault
        folder that is itself synced would put it deeper. A prefix-only check
        would miss that.
        """
        assert is_indexable("Household/.stversions/Budget~20260801.md") is False

    def test_non_markdown_is_not_indexable(self):
        assert is_indexable("Attachments/photo.png") is False
        assert is_indexable("Notes/data.csv") is False

    def test_accepts_path_objects_as_well_as_strings(self):
        """The watchers hold `Path`; the push endpoint holds `str`."""
        assert is_indexable(Path("Daily Notes/Alex/2026-08-14.md")) is True
        assert is_indexable(Path(".stversions/x~1.md")) is False

    def test_empty_path_is_not_indexable(self):
        assert is_indexable("") is False

    def test_stversions_is_in_the_skip_set(self):
        """Pins the specific regression, not just the predicate's behaviour."""
        assert ".stversions" in SKIP_DIRS


class TestNoOpenCodedSkipChecks:
    """The rule must live in one place, because it already failed by not doing.

    `SKIP_DIRS` was hand-copied into three modules and the membership test was
    re-written at each site — so `POST /api/v1/vault/push`, which never wrote
    one, indexed anything a daemon sent it. Adding `.stversions` to three lists
    would have fixed the symptom and left that hole open.

    The client keeps its own copy by necessity (separate distribution, cannot
    import server code), but it is an optimisation: the server enforces the
    same predicate inside `index_single_file`.
    """

    def test_server_modules_do_not_reimplement_the_membership_test(self):
        import app.integrations.obsidian.sync as sync_mod
        import app.integrations.obsidian.watcher as watcher_mod

        for mod in (sync_mod, watcher_mod):
            source = Path(mod.__file__).read_text(encoding="utf-8")
            # Count code lines only. An earlier version of this test matched
            # the whole file and tripped on a docstring that *described* the
            # old inline check — a guard that fires on prose is a guard people
            # learn to edit around.
            occurrences = sum(
                1
                for line in source.splitlines()
                if "in SKIP_DIRS for part in" in line
                and not line.lstrip().startswith("#")
            )
            # The one legitimate occurrence is inside `is_indexable` itself.
            expected = 1 if mod is sync_mod else 0
            assert occurrences == expected, (
                f"{mod.__name__} re-implements the SKIP_DIRS membership test; "
                f"call is_indexable() instead"
            )

    def test_watcher_imports_the_shared_predicate(self):
        import app.integrations.obsidian.watcher as watcher_mod

        assert hasattr(watcher_mod, "is_indexable")


class TestPathNormalisation:
    """One file must be one indexed document, whatever the filesystem calls it.

    macOS stores filenames decomposed: `Gráda` lands on APFS as `Gra` + U+0301,
    and renaming the file to NFC does not stick — the filesystem decomposes it
    again on write. Linux stores the bytes it is handed. So the Mac daemon
    pushes an NFD path while the server's own scan sees NFC, both get used raw
    as `source_id`, and one note becomes two documents: it matches *itself* at
    cosine 1.0, takes two slots in every search, and one copy never updates.

    Observed 2026-08-15 on `Wine — Rosés to Try.md` and on
    `CV - Alex O'Gráda 2026-06.md` within minutes of it being created.
    """

    def test_nfd_and_nfc_paths_collapse_to_one_key(self):
        import unicodedata
        from app.integrations.obsidian.sync import normalise_vault_path

        nfc = unicodedata.normalize("NFC", "Notes/CV - Alex O'Gráda 2026-06.md")
        nfd = unicodedata.normalize("NFD", "Notes/CV - Alex O'Gráda 2026-06.md")
        assert nfc != nfd, "test is meaningless if the two forms are equal"
        assert normalise_vault_path(nfd) == normalise_vault_path(nfc) == nfc

    def test_ascii_paths_are_untouched(self):
        from app.integrations.obsidian.sync import normalise_vault_path

        for p in ("Task Backlog.md", "Daily Notes/Alex/2026-08-15.md"):
            assert normalise_vault_path(p) == p

    def test_the_em_dash_case_is_not_affected(self):
        """Only *decomposable* characters matter — an em-dash is identical in both."""
        import unicodedata
        from app.integrations.obsidian.sync import normalise_vault_path

        p = "Reference/Guides/Runbook — Repointing Tines off ntfy.md"
        assert unicodedata.normalize("NFD", p) == p
        assert normalise_vault_path(p) == p

    def test_both_index_entry_points_normalise(self):
        """Scan and push must agree, or the split simply moves rather than closes."""
        from pathlib import Path
        import app.integrations.obsidian.sync as mod

        src = Path(mod.__file__).read_text(encoding="utf-8")
        code = [ln for ln in src.splitlines()
                if "normalise_vault_path(" in ln and not ln.lstrip().startswith("#")]
        # def + scan path + push path
        assert len(code) >= 3, f"expected both entry points to normalise, saw {code}"
