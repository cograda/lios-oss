"""Alembic migration hygiene — unit tier, no database required.

Split out of `test_db_harness.py` (which is entirely `pytest.mark.db`)
because these checks are pure file/metadata reads. They are the kind of
thing that must run on every commit, including on a machine with no
Docker, since what they catch breaks the whole db tier at once.
"""

def test_no_duplicate_alembic_revision_ids():
    """Every migration's `revision` id must be unique.

    This repo hand-writes ids in a rotating hex-ish pattern (`a1b2c3d4e5f6`,
    `b1c2d3e4f5a6`, …) rather than using Alembic's random hashes, which makes
    collisions genuinely easy to produce. A duplicate doesn't fail politely:
    Alembic reports `CycleDetected` naming ~25 unrelated revisions, so the
    error points nowhere near the file that caused it. Catching it here turns
    a confusing 300-error test run into one clear failure.
    """
    import re
    from collections import Counter
    from pathlib import Path

    versions = Path(__file__).resolve().parent.parent / "alembic" / "versions"
    pattern = re.compile(r"^revision(?::\s*str)?\s*=\s*[\"']([^\"']+)[\"']", re.M)

    found: list[tuple[str, str]] = []
    for path in sorted(versions.glob("*.py")):
        match = pattern.search(path.read_text(encoding="utf-8"))
        if match:
            found.append((match.group(1), path.name))

    assert found, "no migrations discovered — is the versions path right?"

    counts = Counter(rev for rev, _ in found)
    dupes = {
        rev: [name for r, name in found if r == rev]
        for rev, n in counts.items()
        if n > 1
    }
    assert not dupes, f"duplicate alembic revision ids: {dupes}"


def test_alembic_has_exactly_one_head():
    """Two heads means a migration branched — usually a `down_revision` left
    pointing at something other than the real tip. Alembic refuses to upgrade
    until it's resolved, so catching it in CI beats catching it on deploy."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from pathlib import Path

    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert len(heads) == 1, f"expected a single alembic head, got {heads}"
