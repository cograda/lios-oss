#!/usr/bin/env python3
"""Compare prose-extraction runs from different models over the same backlog.

Answers three questions a raw edge count cannot:

  1. Where do the models AGREE? Consensus edges are the safe set — an edge every
     tier independently found is the closest thing to ground truth available
     without hand-labelling all 103 tasks.

  2. Where do they CONTRADICT? Two models emitting `A blocked_by B` and
     `B blocked_by A` is a direct logical contradiction, and it localises the
     direction-inversion failure automatically. This is the single most valuable
     output here: it finds the bug class without a human reading every edge.

  3. What does each model find ALONE? A unique edge is either a real catch the
     others missed, or a hallucination. Either way it needs eyes, and it is a
     much shorter list than the full output.

Usage:
    python scripts/taskgraph_compare.py haiku=a.json opus=b.json sonnet=c.json
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from taskgraph_parse import parse_backlog, DEFAULT_BACKLOG  # noqa: E402


def norm_dst(dst: str) -> str:
    """Loose key for dst so 'David responds' ≈ 'once David responds'.

    Free-text blockers will never match exactly across models, so comparing
    raw strings would report false disagreement everywhere. Uid and ISO-date
    dsts stay exact; prose is reduced to a sorted content-word set.
    """
    if re.fullmatch(r"T-[0-9a-f]{10}", dst) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", dst):
        return dst
    words = re.findall(r"[a-z0-9]+", dst.lower())
    stop = {"the", "a", "an", "to", "of", "is", "be", "on", "in", "for", "and", "once"}
    return " ".join(sorted(w for w in words if w not in stop))[:60]


def load(path: Path) -> list[dict]:
    return json.loads(path.read_text()).get("edges", [])


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(__doc__)

    runs: dict[str, list[dict]] = {}
    for arg in sys.argv[1:]:
        label, _, path = arg.partition("=")
        runs[label] = load(Path(path))

    tasks = parse_backlog(DEFAULT_BACKLOG.read_text())
    text = {t.uid: t.text for t in tasks}
    known = set(text)

    def short(uid_or_text: str, n: int = 46) -> str:
        return text.get(uid_or_text, uid_or_text)[:n]

    # ─── validity ───
    print("=" * 78)
    print(f"{'MODEL':<10} {'EDGES':>6} {'blocked':>8} {'waiting':>8} {'deadline':>9} "
          f"{'bad-uid':>8} {'no-evid':>8}")
    print("=" * 78)
    for label, edges in runs.items():
        c = defaultdict(int)
        for e in edges:
            c[e.get("predicate")] += 1
        bad = sum(1 for e in edges if e.get("src") not in known)
        noev = sum(1 for e in edges if not e.get("evidence"))
        print(f"{label:<10} {len(edges):>6} {c['blocked_by']:>8} {c['waiting_on']:>8} "
              f"{c['soft_deadline']:>9} {bad:>8} {noev:>8}")

    # ─── agreement ───
    keyed: dict[str, set[tuple]] = {
        label: {(e["src"], e["predicate"], norm_dst(str(e["dst"]))) for e in edges}
        for label, edges in runs.items()
    }
    all_keys: dict[tuple, set[str]] = defaultdict(set)
    for label, ks in keyed.items():
        for k in ks:
            all_keys[k].add(label)

    n = len(runs)
    consensus = {k: v for k, v in all_keys.items() if len(v) == n}
    partial = {k: v for k, v in all_keys.items() if 1 < len(v) < n}
    solo = {k: v for k, v in all_keys.items() if len(v) == 1}

    print(f"\n{'─'*78}\nAGREEMENT   {len(all_keys)} distinct edges across {n} models")
    print(f"  {len(consensus):>3}  found by ALL {n}      (safe set)")
    print(f"  {len(partial):>3}  found by SOME")
    print(f"  {len(solo):>3}  found by ONE ONLY   (needs review)")

    # ─── contradictions: the direction-inversion detector ───
    blocked = defaultdict(set)
    for k, labels in all_keys.items():
        src, pred, dst = k
        if pred == "blocked_by" and dst in known:
            blocked[(src, dst)] |= labels

    contradictions = []
    for (a, b), la in blocked.items():
        if (b, a) in blocked:
            if (a, b) < (b, a):  # report each pair once
                contradictions.append((a, b, la, blocked[(b, a)]))

    print(f"\n{'─'*78}\nDIRECTION CONTRADICTIONS  {len(contradictions)}")
    if not contradictions:
        print("  none — no model pair inverted the same task-to-task blocker")
    for a, b, la, lb in contradictions:
        print(f"\n  ⚠ {sorted(la)} say:  {short(a)}")
        print(f"       blocked_by  {short(b)}")
        print(f"    {sorted(lb)} say the REVERSE")

    print(f"\n{'─'*78}\nCONSENSUS EDGES  ({len(consensus)})")
    for (src, pred, dst) in sorted(consensus, key=lambda k: k[1]):
        arrow = short(dst, 40) if dst in known else dst[:40]
        print(f"  {pred:<14} {short(src, 40):<40} → {arrow}")

    print(f"\n{'─'*78}\nSOLO EDGES  ({len(solo)}) — one model only, review these")
    for (src, pred, dst), labels in sorted(solo.items(), key=lambda kv: sorted(kv[1])):
        arrow = short(dst, 36) if dst in known else dst[:36]
        print(f"  [{sorted(labels)[0]:<7}] {pred:<14} {short(src, 34):<34} → {arrow}")


if __name__ == "__main__":
    main()
