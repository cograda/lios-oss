"""Export timemachine's Gmail archive into a portable dump for comar.

timemachine (cograda/timemachine) holds ~127k Gmail messages parsed from a
Google Takeout mbox, spanning 2004→2026 with full bodies — against comar's
13.7k from 2021 with no bodies stored. It also holds 24.5k embeddings produced
by FastEmbed + BAAI/bge-small-en-v1.5, byte-identical to comar's own embedding
stack, so those vectors transfer rather than needing ~24k re-embeds.
`scripts/verify_timemachine_vectors.py` proves that reproducibility; run it
first.

This runs on the Mac (timemachine's SQLite is local and gitignored) and writes
a dump that `app/scripts/import_timemachine_mail.py` consumes on the server.
Two stages rather than a direct write because Postgres is bound to
127.0.0.1:5433 on the server and is not reachable from here.

    python scripts/export_timemachine_mail.py --out ~/tm-mail-dump

Writes:
    messages.jsonl.gz   one row per message, comar's field names
    vectors.npy         (N, 384) float32, row-aligned with vector_ids.json
    vector_ids.json     source_id per vector row
    manifest.json       counts + provenance
"""

import argparse
import gzip
import json
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path

TM_DB = Path.home() / "Desktop/Code/timemachine/data/timemachine.db"
ACCOUNT = "rivers@gmail.com"
# timemachine holds two vector spaces, and which one comar wants depends on
# which embedder comar is running. Selected by --vectors; never inferred.
#
#   1536  message_vec            gemini-embedding-2, a sqlite-vec `vec0` virtual
#                                table (needs the sqlite_vec extension loaded)
#    384  message_vec_384d_old   BAAI/bge-small-en-v1.5, a plain BLOB table
#
# The 384d table was simply `message_vec` until timemachine migrated on
# 2026-08-03. That rename silently invalidated this script, which is why the
# width is now asserted against the blob at runtime rather than assumed.
VECTOR_SPACES = {
    384: ("message_vec_384d_old", "BAAI/bge-small-en-v1.5", False),
    1536: ("message_vec", "gemini-embedding-2", True),
}

# Takeout writes human-readable labels; comar's existing rows (and the analysis
# built on them) use Gmail API label constants. Normalise so both eras of data
# answer the same query.
LABEL_MAP = {
    "Category updates": "CATEGORY_UPDATES",
    "Category personal": "CATEGORY_PERSONAL",
    "Category promotions": "CATEGORY_PROMOTIONS",
    "Category social": "CATEGORY_SOCIAL",
    "Category forums": "CATEGORY_FORUMS",
    "Category purchases": "CATEGORY_PURCHASES",
    "Important": "IMPORTANT",
    "Starred": "STARRED",
    "Sent": "SENT",
    "Inbox": "INBOX",
    "Archived": "ARCHIVED",
    "Opened": "OPENED",
    "Unread": "UNREAD",
    "Spam": "SPAM",
    "Trash": "TRASH",
    "Draft": "DRAFT",
    "Chat": "CHAT",
}


def norm_labels(labels: list[str]) -> list[str]:
    """Map Takeout label names to API constants, keeping user labels verbatim."""
    return [LABEL_MAP.get(l, l) for l in labels]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=TM_DB)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--account", default=ACCOUNT)
    ap.add_argument("--vectors", type=int, choices=sorted(VECTOR_SPACES), default=1536,
                    help="which timemachine vector space to export (default: 1536)")
    args = ap.parse_args()

    import numpy as np

    vec_table, vec_model, needs_ext = VECTOR_SPACES[args.vectors]
    dim = args.vectors

    args.out.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    if needs_ext:
        # `vec0` virtual tables are unreadable without the extension — including
        # by the sqlite3 CLI, which is how the 2026-08-03 rename first showed up.
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

    # ---- messages ---------------------------------------------------------
    rows = conn.execute(
        """SELECT m.id, m.ts, m.body, m.dedupe_key, m.raw_meta, m.thread_id,
                  h.handle AS sender
             FROM message m
             LEFT JOIN handle h ON h.id = m.sender_handle_id
            WHERE m.source = 'gmail'
            ORDER BY m.ts"""
    )

    n = 0
    earliest = latest = None
    path = args.out / "messages.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for r in rows:
            meta = json.loads(r["raw_meta"]) if r["raw_meta"] else {}
            labels = norm_labels(meta.get("labels") or [])
            to = meta.get("to") or []
            rec = {
                # `tm:` prefix keeps provenance legible in the DB and guarantees
                # no collision with a real Gmail API id.
                "google_message_id": f"tm:{r['dedupe_key']}",
                "rfc_message_id": meta.get("message_id"),
                "thread_id": f"tm:{r['thread_id']}" if r["thread_id"] else f"tm:{r['dedupe_key']}",
                "account_email": args.account,
                "subject": meta.get("subject"),
                "sender": r["sender"],
                "to": ", ".join(to) if isinstance(to, list) else to,
                "date": r["ts"],
                "body_text": r["body"],
                "snippet": (r["body"] or "")[:200],
                "labels": ",".join(labels),
                # Takeout marks read state as an "Opened" label; absence means
                # unread. There is no UNREAD label to test for.
                "is_read": "OPENED" in labels,
                "is_starred": "STARRED" in labels,
                "has_attachments": False,  # not recoverable from the parsed archive
                "is_personal": bool(meta.get("personal", 1)),
            }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
            earliest = min(earliest or r["ts"], r["ts"])
            latest = max(latest or r["ts"], r["ts"])
    print(f"messages: {n:,} -> {path.name}  ({earliest[:10]} .. {latest[:10]})")

    # ---- vectors ----------------------------------------------------------
    vrows = conn.execute(
        f"""SELECT m.dedupe_key, v.embedding
             FROM message m
             JOIN {vec_table} v ON v.message_id = m.id
            WHERE m.source = 'gmail'
            ORDER BY m.id"""
    ).fetchall()
    if not vrows:
        raise SystemExit(f"{vec_table} holds no gmail vectors — wrong --vectors?")

    # Assert the stored width instead of trusting the table name. This is the
    # check whose absence let a table rename in another repo invalidate the
    # import silently; `struct.unpack` would also catch it, but not by name.
    got = len(vrows[0]["embedding"]) // 4
    if got != dim:
        raise SystemExit(
            f"{vec_table} holds {got}-dim vectors, expected {dim} "
            f"({vec_model}) — timemachine's schema has moved again."
        )

    vecs = np.array([struct.unpack(f"{dim}f", r["embedding"]) for r in vrows], dtype=np.float32)
    ids = [f"tm:{r['dedupe_key']}" for r in vrows]
    np.save(args.out / "vectors.npy", vecs)
    (args.out / "vector_ids.json").write_text(json.dumps(ids))
    norms = np.linalg.norm(vecs, axis=1)
    print(f"vectors:  {len(ids):,} x {vecs.shape[1]}  "
          f"norms {norms.min():.4f}..{norms.max():.4f} -> vectors.npy")

    (args.out / "manifest.json").write_text(json.dumps({
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_db": str(args.db),
        "account": args.account,
        "messages": n,
        "vectors": len(ids),
        "model": vec_model,
        "dim": dim,
        "vector_table": vec_table,
        "date_from": earliest,
        "date_to": latest,
    }, indent=2))
    print(f"\nwrote {args.out}")
    print(f"cutover timestamp for the API backfill: after:{(latest or '')[:10].replace('-', '/')}")


if __name__ == "__main__":
    main()
