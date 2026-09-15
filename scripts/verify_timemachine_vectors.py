"""Verify timemachine's stored Gmail vectors are reproducible before copying them.

timemachine embedded with FastEmbed + BAAI/bge-small-en-v1.5 (384-dim), which
is byte-for-byte the stack comar uses. That makes a bulk vector copy possible
instead of ~24k re-embeds — but only if the stored vectors still match what
today's FastEmbed produces for the same text. A version bump that changed
tokenisation, pooling or normalisation would silently poison the whole import:
the vectors would load fine, search would return plausible-looking rubbish, and
nothing would error.

So: re-embed a sample and require near-perfect cosine agreement.

    python scripts/verify_timemachine_vectors.py [--n 8]
"""

import argparse
import sqlite3
import struct
import sys
from pathlib import Path

TM_DB = Path.home() / "Desktop/Code/timemachine/data/timemachine.db"
MODEL = "BAAI/bge-small-en-v1.5"
DIM = 384
# timemachine moved to a 1536-dim embedder and renamed the bge-small vectors to
# `message_vec_384d_old`; `message_vec` is now a sqlite-vec `vec0` virtual table
# holding the new ones. Only the 384-dim table is dimension-compatible with
# comar, and being a plain BLOB table it needs no sqlite-vec extension to read.
VEC_TABLE = "message_vec_384d_old"
# Below this, assume the embedding stack has drifted and refuse the bulk copy.
THRESHOLD = 0.999


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--db", type=Path, default=TM_DB)
    args = ap.parse_args()

    import numpy as np
    from fastembed import TextEmbedding

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    rows = conn.execute(
        f"""SELECT m.id, m.body, v.embedding
             FROM message m
             JOIN {VEC_TABLE} v ON v.message_id = m.id
            WHERE m.source = 'gmail'
            ORDER BY m.id
            LIMIT ?""",
        (args.n,),
    ).fetchall()
    if not rows:
        print("no embedded gmail messages found", file=sys.stderr)
        return 2

    stored = np.array([struct.unpack(f"{DIM}f", r[2]) for r in rows], dtype=np.float32)
    print(f"loaded {len(rows)} stored vectors, dim={stored.shape[1]}")
    print(f"stored L2 norms: min={np.linalg.norm(stored,axis=1).min():.4f} "
          f"max={np.linalg.norm(stored,axis=1).max():.4f}")

    print(f"re-embedding with {MODEL} (first run downloads the model)…")
    model = TextEmbedding(model_name=MODEL)
    fresh = np.array(list(model.embed([r[1] for r in rows])), dtype=np.float32)

    a = stored / np.clip(np.linalg.norm(stored, axis=1, keepdims=True), 1e-12, None)
    b = fresh / np.clip(np.linalg.norm(fresh, axis=1, keepdims=True), 1e-12, None)
    cos = (a * b).sum(axis=1)

    print(f"\n{'msg_id':>10s}  {'cosine':>8s}  body[:52]")
    for (mid, body, _), c in zip(rows, cos):
        flag = "" if c >= THRESHOLD else "  <-- MISMATCH"
        print(f"{mid:>10d}  {c:8.6f}  {body[:52]!r}{flag}")

    worst = float(cos.min())
    print(f"\nworst cosine {worst:.6f} (threshold {THRESHOLD})")
    if worst < THRESHOLD:
        print("FAIL — stored vectors are not reproducible; re-embed instead of copying.")
        return 1
    print("PASS — stored vectors are reproducible; bulk copy is safe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
