#!/usr/bin/env python3
"""Lift the vault task backlog into task-graph nodes and edges.

Phase-0 prototype for the task-bent knowledge graph. Deliberately a standalone
script rather than an `app/integrations/graph/` package: `app.plugin.discovery`
walks every directory under `app/integrations/` and fails boot validation on a
package without a valid `manifest.py`, so a half-formed integration would take
the server down. Promote this into a real package once the schema settles.

Two tiers, and the split is load-bearing:

  * Deterministic (free, exact, always runs) — checkbox state, priority emoji,
    `📅` dates, `✅` completion dates, tags, `[[wikilinks]]`, H1 category, H2
    section, sub-bullet context. Wikilinks are the highest-confidence signal in
    the file: a human typed them, so `concerns` edges derived from them get
    confidence 1.0 and no model ever second-guesses them.

  * Haiku (opt-in via --llm) — only the three predicates that exist solely as
    prose claims: `blocked_by`, `waiting_on`, and soft deadlines stated in text
    but never captured as `📅`. Sent as ONE request with every task visible, so
    "blocks the build" can resolve to a sibling task's uid.

Usage:
    python scripts/taskgraph_parse.py                      # deterministic only
    python scripts/taskgraph_parse.py --llm                # + Haiku pass
    python scripts/taskgraph_parse.py --llm --json out.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path

DEFAULT_BACKLOG = Path.home() / "Desktop/Code/lios/vault/Task Backlog.md"

# ─── Patterns (priority/date/tag maps mirror apple_reminders/backlog_sync.py —
# keep them in sync until this is promoted and the two share a module) ───

TASK_RE = re.compile(r"^- \[([ xX])\] (.+)$")
SUBBULLET_RE = re.compile(r"^\s+- (.+)$")
PRIORITY_RE = re.compile(r"[🔺⏫🔼🔽]")
DUE_DATE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
DONE_DATE_RE = re.compile(r"✅\s*(\d{4}-\d{2}-\d{2})")
TAG_RE = re.compile(r"#[\w/.-]+")
WIKILINK_RE = re.compile(r"\[\[([^|\]]+?)(?:\|[^\]]+)?\]\]")

PRIORITY_RANK = {"🔺": "urgent", "⏫": "high", "🔼": "medium", "🔽": "low"}

# Tags that describe *status* rather than subject matter — these become node
# attributes or edges, not `concerns` edges to a topic.
STATUS_TAGS = {"#blocked", "#focus", "#quick", "#deep", "#review", "#errand"}


@dataclass
class TaskNode:
    uid: str
    text: str
    raw_line: str
    completed: bool
    priority: str | None
    due_date: str | None
    done_date: str | None
    category: str            # H1 — Home / Renovation / Kids / Finance / Admin
    section: str | None      # H2 — often itself a [[wikilink]]
    tags: list[str]
    status_tags: list[str]
    subbullets: list[str] = field(default_factory=list)
    line_number: int = 0


@dataclass
class Edge:
    src: str                 # task uid
    predicate: str
    dst: str                 # entity name or task uid
    dst_kind: str            # person | project | section | category | task | date
    confidence: float
    provenance: str          # how this edge was derived


def _uid(text: str) -> str:
    """Stable id from normalised task text.

    Content-addressed so a re-run after an unrelated edit to the file keeps the
    same uid, and so a task that moves between sections keeps its identity —
    the same reasoning behind financier's content-addressed transaction ids.
    """
    norm = re.sub(r"\s+", " ", PRIORITY_RE.sub("", text)).strip().lower()
    norm = TAG_RE.sub("", norm)
    norm = DUE_DATE_RE.sub("", norm)
    norm = DONE_DATE_RE.sub("", norm)
    norm = re.sub(r"[^\w\s]", "", norm)
    norm = re.sub(r"\s+", " ", norm).strip()
    return "T-" + hashlib.sha1(norm.encode()).hexdigest()[:10]


def parse_backlog(content: str) -> list[TaskNode]:
    """Parse the backlog markdown into task nodes, keeping structure."""
    tasks: list[TaskNode] = []
    category = ""
    section: str | None = None
    in_code_block = False
    current: TaskNode | None = None

    for line_num, line in enumerate(content.split("\n"), start=1):
        stripped = line.strip()

        # Obsidian Tasks query blocks are lenses over this same list, not data.
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        if stripped.startswith("# "):
            category = stripped[2:].strip()
            section = None
            current = None
            continue
        if stripped.startswith("## "):
            section = stripped[3:].strip()
            current = None
            continue

        # Sub-bullets belong to the task above them and carry the constraints.
        if current is not None and SUBBULLET_RE.match(line):
            current.subbullets.append(SUBBULLET_RE.match(line).group(1).strip())
            continue

        if not line.startswith("- ["):
            if stripped and not stripped.startswith(">"):
                current = None
            continue

        match = TASK_RE.match(stripped)
        if not match:
            continue

        checkbox, text = match.groups()
        pri = PRIORITY_RE.search(text)
        due = DUE_DATE_RE.search(text)
        done = DONE_DATE_RE.search(text)
        all_tags = TAG_RE.findall(text)

        current = TaskNode(
            uid=_uid(text),
            text=_clean(text),
            raw_line=line,
            completed=checkbox.lower() == "x",
            priority=PRIORITY_RANK.get(pri.group()) if pri else None,
            due_date=due.group(1) if due else None,
            done_date=done.group(1) if done else None,
            category=category,
            section=section,
            tags=[t for t in all_tags if t not in STATUS_TAGS],
            status_tags=[t for t in all_tags if t in STATUS_TAGS],
            line_number=line_num,
        )
        tasks.append(current)

    return tasks


def _clean(text: str) -> str:
    """Human-readable task text: metadata stripped, wikilink display kept."""
    out = PRIORITY_RE.sub("", text)
    out = DUE_DATE_RE.sub("", out)
    out = DONE_DATE_RE.sub("", out)
    out = TAG_RE.sub("", out)
    out = re.sub(r"\[\[([^|\]]*\|)?([^\]]+)\]\]", r"\2", out)
    return re.sub(r"\s+", " ", out).strip()


# ─── Tier 1: deterministic edges ───


def _classify(name: str, people: set[str], notes: set[str]) -> str:
    """Resolve a wikilink target to an entity kind.

    Three-way, and each rung is exact rather than heuristic:

      person            — has a note in vault/People/
      project           — resolves to a note anywhere else in the vault
      person_candidate  — resolves to NO note at all, and reads like a name

    The last rung is the interesting one. Guessing from shape alone misfires
    badly ([[House Jobs]], [[Chore App]] and [[Family Charter]] are all two
    capitalised words), but checking the vault first removes every one of those:
    they resolve to real notes, so they are projects. What survives is people
    the backlog talks about who have no identity record — and therefore cannot
    be resolved against mail, WhatsApp or calendar in any later phase. That set
    is a finding, not noise.
    """
    if name in people:
        return "person"
    if name in notes:
        return "project"
    parts = name.split()
    looks_personal = (
        1 <= len(parts) <= 3
        and all(p[:1].isupper() for p in parts if p)
        and not any(c in name for c in "—-&/")
    )
    return "person_candidate" if looks_personal else "project"


def load_people(vault: Path) -> set[str]:
    """Canonical person names from vault/People/*.md filenames."""
    people_dir = vault / "People"
    if not people_dir.is_dir():
        return set()
    return {p.stem for p in people_dir.glob("*.md")}


def load_notes(vault: Path) -> set[str]:
    """Every note stem in the vault — the resolver for non-person wikilinks."""
    return {p.stem for p in vault.rglob("*.md") if ".trash" not in p.parts}


def deterministic_edges(tasks: list[TaskNode], people: set[str], notes: set[str]) -> list[Edge]:
    edges: list[Edge] = []

    for t in tasks:
        if t.category:
            edges.append(Edge(t.uid, "in_category", t.category, "category", 1.0, "h1-heading"))
        if t.section:
            target = WIKILINK_RE.findall(t.section)
            name = target[0] if target else t.section
            edges.append(Edge(t.uid, "part_of", name, "project", 1.0, "h2-heading"))

        # Wikilinks anywhere in the task body or its sub-bullets. Human-asserted,
        # so confidence 1.0 — an LLM never gets to overrule these.
        body = t.raw_line + "\n" + "\n".join(t.subbullets)
        for name in dict.fromkeys(WIKILINK_RE.findall(body)):
            edges.append(Edge(t.uid, "concerns", name, _classify(name, people, notes), 1.0, "wikilink"))

        # `#person/isla` namespace → person edge without a People note.
        for tag in t.tags:
            if tag.startswith("#person/"):
                edges.append(
                    Edge(t.uid, "concerns", tag.split("/", 1)[1].title(), "person", 1.0, "person-tag")
                )
            else:
                edges.append(Edge(t.uid, "tagged", tag.lstrip("#"), "topic", 1.0, "tag"))

        if t.due_date:
            edges.append(Edge(t.uid, "due", t.due_date, "date", 1.0, "date-emoji"))
        if "#blocked" in t.status_tags:
            edges.append(Edge(t.uid, "blocked_flag", "unresolved", "task", 1.0, "blocked-tag"))

    return edges


# ─── Tier 2: Haiku for the three prose-only predicates ───

LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "uid of the task the edge starts from"},
                    "predicate": {"type": "string", "enum": ["blocked_by", "waiting_on", "soft_deadline"]},
                    "dst": {
                        "type": "string",
                        "description": (
                            "For blocked_by: the uid of the blocking task, or a short "
                            "phrase if it is not another task in this list. For "
                            "waiting_on: the person's name. For soft_deadline: an "
                            "ISO date (YYYY-MM-DD) if determinable, else the phrase."
                        ),
                    },
                    "evidence": {
                        "type": "string",
                        "description": "The exact substring of the task text supporting this edge.",
                    },
                    "confidence": {"type": "number"},
                },
                "required": ["src", "predicate", "dst", "evidence", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["edges"],
    "additionalProperties": False,
}

LLM_PROMPT = """You are extracting three specific relationship types from a household task backlog.

Extract ONLY these predicates. Do not extract anything else — topics, people
mentioned in passing, priorities and dates are already captured deterministically.

1. blocked_by — this task cannot start or finish until something else happens.
   Look for: "blocked on", "paused until", "once X responds", "gates the build",
   "blocks the build", "needs X first", "only then". If the blocker is another
   task in the list, use that task's uid as dst. Otherwise use a short phrase.

2. waiting_on — this task is waiting on a specific named PERSON to act.
   Not "I need to email David" (that is the user's action) — rather
   "once David responds", "Sam to re-share", "awaiting their reply".

3. soft_deadline — a real deadline stated in prose that has NO 📅 date marker.
   e.g. "before Sat 1 Aug", "week of 3 Aug", "before Saturday". Today is
   2026-08-02; resolve to an ISO date where the year is unambiguous.

Be conservative. An edge you are unsure about is worse than a missing edge —
a downstream verification step cannot recover a wrong blocker, but a human
reading the backlog will spot a missing one. Emit confidence below 0.7 for
anything inferred rather than stated.

Tasks:
"""


def ingest_llm_edges(path: Path, tasks: list[TaskNode]) -> list[Edge]:
    """Load prose edges produced out-of-band (subagent, Batch API) and validate.

    Kept separate from `llm_edges()` so extraction and parsing are decoupled:
    whatever produced the edges — a subagent, a batch job, a hand-written file —
    lands in the same validator. Nothing is trusted on the way in.
    """
    payload = json.loads(path.read_text())
    known = {t.uid for t in tasks}
    edges, dropped = [], []

    for e in payload.get("edges", []):
        if e.get("src") not in known:
            dropped.append(("unknown src", e.get("src")))
            continue
        if e.get("predicate") not in {"blocked_by", "waiting_on", "soft_deadline"}:
            dropped.append(("bad predicate", e.get("predicate")))
            continue
        # An edge without its supporting substring cannot be audited, so it is
        # not admissible regardless of how confident the model claims to be.
        if not e.get("evidence"):
            dropped.append(("no evidence", e.get("src")))
            continue
        dst_kind = (
            "task" if e["predicate"] == "blocked_by" and e["dst"] in known
            else "person" if e["predicate"] == "waiting_on"
            else "date" if e["predicate"] == "soft_deadline" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", e["dst"])
            else "phrase"
        )
        edges.append(Edge(e["src"], e["predicate"], e["dst"], dst_kind,
                          float(e.get("confidence", 0.5)),
                          f"llm:{e['evidence'][:70]}"))

    if dropped:
        print(f"\ndropped {len(dropped)} invalid edges:", file=sys.stderr)
        for reason, val in dropped:
            print(f"    {reason}: {val}", file=sys.stderr)
    return edges


def build_llm_prompt(tasks: list[TaskNode]) -> str:
    """The single prompt carrying every open task, uid-labelled.

    All tasks go in one request rather than one-per-task: `blocked_by` needs to
    resolve phrases like "gates the build" to a *sibling* task's uid, which is
    impossible if each task is extracted in isolation. At ~100 tasks this is
    well inside Haiku's 200K window, and it costs one request instead of 103.
    """
    lines = []
    for t in tasks:
        if t.completed:
            continue
        body = t.text
        if t.subbullets:
            body += " || " + " || ".join(t.subbullets)
        lines.append(f"{t.uid}: {body}")
    return LLM_PROMPT + "\n".join(lines)


def llm_edges(
    tasks: list[TaskNode], model: str = "claude-haiku-4-5", dry_run: bool = False
) -> list[Edge]:
    """One request, all tasks visible, so blocked_by can resolve to sibling uids."""
    prompt = build_llm_prompt(tasks)

    if dry_run:
        # ~3.7 chars/token is a rough English estimate; count_tokens needs a key.
        est_in = len(prompt) // 4
        est_out = 3000
        std = est_in / 1e6 * 1.0 + est_out / 1e6 * 5.0
        print(f"\n--- DRY RUN ({model}) ---", file=sys.stderr)
        print(f"prompt: {len(prompt)} chars ≈ {est_in} tokens", file=sys.stderr)
        print(f"est. cost: ${std:.4f} standard / ${std/2:.4f} batched", file=sys.stderr)
        print("-" * 60, file=sys.stderr)
        print(prompt[:1200] + "\n  […]\n" + prompt[-600:], file=sys.stderr)
        return []

    try:
        import anthropic
    except ImportError:
        sys.exit("anthropic SDK not installed: pip install anthropic")

    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("HOME_ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit(
            "No Anthropic credential found. This Mac has none — the key lives in\n"
            "the server's integration_config / .env. Either:\n"
            "  export ANTHROPIC_API_KEY=...   (or HOME_ANTHROPIC_API_KEY)\n"
            "  or run --dry-run to inspect the request without sending it."
        )
    client = anthropic.Anthropic(api_key=api_key)

    response = client.messages.create(
        model=model,
        max_tokens=16000,
        output_config={"format": {"type": "json_schema", "schema": LLM_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )

    if response.stop_reason == "refusal":
        sys.exit(f"model declined: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        print("WARNING: hit max_tokens — edge list is truncated", file=sys.stderr)

    text = next(b.text for b in response.content if b.type == "text")
    payload = json.loads(text)

    known = {t.uid for t in tasks}
    edges = []
    for e in payload["edges"]:
        if e["src"] not in known:
            continue  # model invented a uid — drop it rather than trust it
        dst_kind = (
            "task" if e["predicate"] == "blocked_by" and e["dst"] in known
            else "person" if e["predicate"] == "waiting_on"
            else "date" if e["predicate"] == "soft_deadline"
            else "phrase"
        )
        edges.append(
            Edge(e["src"], e["predicate"], e["dst"], dst_kind,
                 float(e["confidence"]), f"haiku:{e['evidence'][:60]}")
        )

    usage = response.usage
    cost = usage.input_tokens / 1e6 * 1.0 + usage.output_tokens / 1e6 * 5.0
    print(
        f"\nHaiku: {usage.input_tokens} in / {usage.output_tokens} out "
        f"≈ ${cost:.4f} (${cost/2:.4f} batched)",
        file=sys.stderr,
    )
    return edges


# ─── Report ───


def report(tasks: list[TaskNode], edges: list[Edge]) -> None:
    open_tasks = [t for t in tasks if not t.completed]
    print(f"\n{'='*70}")
    print(f"  {len(tasks)} tasks parsed  ({len(open_tasks)} open, "
          f"{len(tasks)-len(open_tasks)} done)   {len(edges)} edges")
    print("=" * 70)

    print("\nEDGES BY PREDICATE")
    for pred, n in Counter(e.predicate for e in edges).most_common():
        print(f"  {n:>4}  {pred}")

    print("\nTASKS BY CATEGORY")
    for cat, n in Counter(t.category for t in open_tasks).most_common():
        print(f"  {n:>4}  {cat or '(none)'}")

    by_task: dict[str, list[Edge]] = {}
    for e in edges:
        by_task.setdefault(e.src, []).append(e)

    # Context-less tasks are the phase-1 acceptance criterion: a task with no
    # concerns/part_of edge cannot be surfaced by any graph query.
    orphans = [
        t for t in open_tasks
        if not any(e.predicate in ("concerns", "part_of") for e in by_task.get(t.uid, []))
    ]
    print(f"\nCONTEXT-LESS  {len(orphans)}/{len(open_tasks)} open tasks have no "
          f"concerns/part_of edge")
    for t in orphans[:8]:
        print(f"    - {t.text[:72]}")
    if len(orphans) > 8:
        print(f"    … and {len(orphans)-8} more")

    people = Counter(e.dst for e in edges if e.dst_kind == "person")
    if people:
        print("\nPEOPLE BY TASK COUNT  (resolved against vault/People/)")
        for name, n in people.most_common(10):
            print(f"  {n:>4}  {name}")

    candidates = Counter(e.dst for e in edges if e.dst_kind == "person_candidate")
    if candidates:
        print(f"\nPERSON CANDIDATES — no People note, so unresolvable against "
              f"mail/WhatsApp/calendar ({len(candidates)})")
        for name, n in candidates.most_common():
            print(f"  {n:>4}  {name}")

    projects = Counter(e.dst for e in edges if e.predicate == "part_of")
    if projects:
        print("\nTOP PROJECTS")
        for name, n in projects.most_common(8):
            print(f"  {n:>4}  {name}")

    for pred in ("blocked_by", "waiting_on", "soft_deadline"):
        found = [e for e in edges if e.predicate == pred]
        if not found:
            continue
        print(f"\n{pred.upper()}  ({len(found)})")
        text_of = {t.uid: t.text for t in tasks}
        for e in sorted(found, key=lambda x: -x.confidence)[:10]:
            dst = text_of.get(e.dst, e.dst)[:44] if e.dst_kind == "task" else e.dst
            print(f"  [{e.confidence:.2f}] {text_of[e.src][:40]:<40} → {dst}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backlog", type=Path, default=DEFAULT_BACKLOG)
    ap.add_argument("--llm", action="store_true", help="run the Haiku prose pass")
    ap.add_argument("--model", default="claude-haiku-4-5")
    ap.add_argument("--llm-edges", type=Path,
                    help="ingest prose edges produced out-of-band (subagent / Batch API)")
    ap.add_argument("--dry-run", action="store_true",
                    help="build and print the Haiku request without sending it")
    ap.add_argument("--json", type=Path, help="write nodes+edges to this path")
    args = ap.parse_args()

    if not args.backlog.exists():
        sys.exit(f"backlog not found: {args.backlog}")

    vault = args.backlog.parent
    tasks = parse_backlog(args.backlog.read_text())
    edges = deterministic_edges(tasks, load_people(vault), load_notes(vault))

    if args.llm_edges:
        edges += ingest_llm_edges(args.llm_edges, tasks)
    if args.llm or args.dry_run:
        edges += llm_edges(tasks, args.model, dry_run=args.dry_run)

    report(tasks, edges)

    if args.json:
        args.json.write_text(json.dumps(
            {"tasks": [asdict(t) for t in tasks], "edges": [asdict(e) for e in edges]},
            indent=2, ensure_ascii=False,
        ))
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
