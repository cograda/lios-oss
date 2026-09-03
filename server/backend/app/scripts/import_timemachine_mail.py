"""Import the timemachine Gmail archive, replacing comar's API-sourced mail.

Companion to `scripts/export_timemachine_mail.py` (which runs on the Mac).
Takes comar's email from 13.7k messages (2021+, ~40% complete even within the
years it covers, no bodies stored) to ~127k spanning 2004→2026 with full
bodies, plus 24.5k transferred vectors.

REPLACE, DON'T MERGE. The archive is complete up to its last message, so the
clean split is: archive owns everything up to the cutover, the Gmail API owns
everything after. That makes the two sources disjoint by construction and
removes dedup entirely — which matters because a Takeout-derived archive has
no Gmail API message id to dedup *on*. Merging would mean matching on
`rfc_message_id`, which is nullable, non-unique, and absent from some
malformed old mail.

Deletion is scoped to `(user_id, account_email)` so another user's mail, and
every other embedding source, are untouched. Run with --dry-run first; it
reports exactly what it would delete and insert.

    # on the server, dump copied to /tmp/tm-dump inside the container
    docker exec -it lios-core python -m app.scripts.import_timemachine_mail \
        --dump /tmp/tm-dump --user alex --dry-run
    docker exec -it lios-core python -m app.scripts.import_timemachine_mail \
        --dump /tmp/tm-dump --user alex --yes

Afterwards, backfill the gap the archive doesn't cover (its last message
onwards) via POST /api/integrations/google_mail/backfill?after_date=YYYY/MM/DD
— the manifest prints the right date.
"""

import argparse
import gzip
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from app.db import get_db
from app.integrations.embedding.models import Embedding
from app.integrations.google_mail.models import MailMessage
from app.models.users import User

MODEL = "BAAI/bge-small-en-v1.5"
# Matches what the API path stores in chunk_text (sync.py::embed_messages).
# The full body lives on mail_messages.body_text; this is the display/provenance
# copy alongside the vector.
CHUNK_CHARS = 4000
BATCH = 2000


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def scrub(value):
    """Strip NUL bytes from text on the way into Postgres.

    Postgres `text` cannot hold 0x00 — psycopg2 raises `ValueError: A string
    literal cannot contain NUL (0x00) characters` mid-executemany. SQLite has
    no such restriction, so a 24-year archive assembled there carries them
    quite happily; they only surface here, at the destination.

    Applied to every string field rather than the one that happened to fail:
    a corpus this old has NULs wherever a decoder once gave up, and finding
    them one exception at a time means one more full delete+reload each time.
    """
    if isinstance(value, str) and "\x00" in value:
        return value.replace("\x00", "")
    return value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", type=Path, required=True)
    ap.add_argument("--user", required=True, help="users.name, e.g. alex")
    ap.add_argument("--yes", action="store_true", help="actually write")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.yes and not args.dry_run:
        print("refusing to run: pass --dry-run or --yes", file=sys.stderr)
        return 2

    import numpy as np

    manifest = json.loads((args.dump / "manifest.json").read_text())
    account = manifest["account"]
    if manifest["model"] != MODEL:
        print(f"model mismatch: dump={manifest['model']} expected={MODEL}", file=sys.stderr)
        return 2

    vectors = np.load(args.dump / "vectors.npy")
    vector_ids = json.loads((args.dump / "vector_ids.json").read_text())
    if len(vector_ids) != len(vectors):
        print("vector_ids/vectors length mismatch", file=sys.stderr)
        return 2
    vec_of = {sid: i for i, sid in enumerate(vector_ids)}

    db = get_db()
    with db.session() as session:
        user = session.execute(select(User).where(User.name == args.user)).scalar_one_or_none()
        if user is None:
            print(f"no such user: {args.user}", file=sys.stderr)
            return 2
        # Snapshot before any commit — expire_on_commit would re-fetch on a
        # detached instance later in this function.
        user_id = user.id

        existing_mail = session.scalar(
            select(func.count()).select_from(MailMessage).where(
                MailMessage.user_id == user_id, MailMessage.account_email == account)
        )
        existing_emb = session.scalar(
            select(func.count()).select_from(Embedding).where(
                Embedding.source == "email", Embedding.user_id == user_id)
        )

        print(f"user {args.user} (id={user_id}), account {account}")
        print(f"  will DELETE  {existing_mail:,} mail_messages, {existing_emb:,} email embeddings")
        print(f"  will INSERT  {manifest['messages']:,} messages "
              f"({manifest['date_from'][:10]} .. {manifest['date_to'][:10]}), "
              f"{manifest['vectors']:,} vectors")

        if args.dry_run:
            print("\ndry run — nothing written")
            return 0

        session.execute(delete(Embedding).where(
            Embedding.source == "email", Embedding.user_id == user_id))
        session.execute(delete(MailMessage).where(
            MailMessage.user_id == user_id, MailMessage.account_email == account))
        session.commit()
        print("deleted existing email rows")

        msg_rows: list[dict] = []
        emb_rows: list[dict] = []
        n_msg = n_emb = n_skipped_vec = 0

        def flush() -> None:
            nonlocal msg_rows, emb_rows
            if msg_rows:
                # ON CONFLICT so a re-run after a partial failure resumes
                # instead of aborting on uq_mail_user_msg.
                session.execute(
                    insert(MailMessage).on_conflict_do_nothing(
                        index_elements=["user_id", "google_message_id"]),
                    msg_rows,
                )
            if emb_rows:
                session.execute(insert(Embedding), emb_rows)
            session.commit()
            msg_rows, emb_rows = [], []

        with gzip.open(args.dump / "messages.jsonl.gz", "rt", encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                gid = r["google_message_id"]
                body = scrub(r.get("body_text") or "")

                msg_rows.append({
                    "google_message_id": gid,
                    "rfc_message_id": scrub(r.get("rfc_message_id")),
                    "thread_id": r["thread_id"],
                    "account_email": account,
                    "subject": scrub(r.get("subject")),
                    "sender": scrub(r.get("sender")),
                    "to": scrub(r.get("to")),
                    "date": parse_ts(r.get("date")),
                    "snippet": scrub(r.get("snippet")),
                    "body_text": body or None,
                    "labels": r.get("labels"),
                    "is_read": bool(r.get("is_read")),
                    "is_starred": bool(r.get("is_starred")),
                    "has_attachments": bool(r.get("has_attachments")),
                    "is_personal": r.get("is_personal"),
                    "user_id": user_id,
                })
                n_msg += 1

                vi = vec_of.get(gid)
                if vi is not None:
                    if not body:
                        n_skipped_vec += 1
                    else:
                        chunk = body[:CHUNK_CHARS]
                        emb_rows.append({
                            "source": "email",
                            "source_id": gid,
                            "user_id": user_id,
                            "chunk_index": 0,
                            "chunk_text": chunk,
                            "embedding": vectors[vi].tolist(),
                            "content_hash": hashlib.md5(chunk.encode()).hexdigest(),
                            "metadata_json": json.dumps({
                                "account": account,
                                "subject": r.get("subject"),
                                "sender": r.get("sender"),
                                "date": (r.get("date") or "")[:16].replace("T", " "),
                                "thread_id": r["thread_id"],
                                "personal": r.get("is_personal"),
                                "origin": "timemachine",
                            }),
                            "model_name": MODEL,
                        })
                        n_emb += 1

                if len(msg_rows) >= BATCH:
                    flush()
                    print(f"  {n_msg:,}/{manifest['messages']:,} messages, "
                          f"{n_emb:,} vectors", flush=True)
        flush()

        print(f"\ninserted {n_msg:,} messages, {n_emb:,} embeddings")
        if n_skipped_vec:
            print(f"skipped {n_skipped_vec} vectors whose message had an empty body")
        missing = len(vector_ids) - n_emb - n_skipped_vec
        if missing:
            print(f"WARNING: {missing} vectors had no matching message row")
        print(f"\nnext: backfill the gap since the archive ends —")
        print(f"  POST /api/integrations/google_mail/backfill"
              f"?after_date={manifest['date_to'][:10].replace('-', '/')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
