"""Standalone embedding subprocess.

Reads a JSON array of strings from stdin, writes a JSON array of
float-vector arrays to stdout, then exits. The OS reclaims the
fastembed/ONNX arenas on process termination — that's the point.

Invoked by EmbeddingService.process_queue() via subprocess.run().
Not imported by anything in the app runtime.
"""

import json
import sys

# How many texts ONNX holds in flight at once. See the comment at the call site
# for the measurements behind this number — it is a memory control, not a
# throughput one.
EMBED_BATCH_SIZE = 16


def main() -> int:
    payload = sys.stdin.read()
    if not payload.strip():
        sys.stdout.write("[]")
        return 0

    texts = json.loads(payload)
    if not isinstance(texts, list) or not texts:
        sys.stdout.write("[]")
        return 0

    # Import inside main() so the model is only loaded when we actually have work.
    from fastembed import TextEmbedding

    model = TextEmbedding("BAAI/bge-small-en-v1.5")
    # batch_size and parallel are set explicitly, NOT left to fastembed's
    # defaults (batch_size=256, parallel=None). Measured on a fresh process per
    # setting, 100 texts of 6,000 chars:
    #
    #     batch_size=256  peak 2,444 MB   8.0s   <- the default
    #     batch_size=32   peak 1,368 MB   7.9s
    #     batch_size=8    peak   619 MB   7.9s
    #
    # Identical wall time, 4x the memory. At 200 items of 6,000 chars the
    # default reached ~4.2 GB on the server and, alongside a second concurrent
    # run, took a 7.8 GB box with Postgres on it to 256 MB available — twice.
    # 16 measures at 806 MB for a full 200-item batch.
    #
    # `parallel=None` keeps this single-process: data-parallel encoding forks a
    # worker, which on 2 cores buys nothing and doubles peak memory. The two
    # consecutive PIDs each holding gigabytes were exactly that.
    vectors = [
        v.tolist() for v in model.embed(texts, batch_size=EMBED_BATCH_SIZE, parallel=None)
    ]
    sys.stdout.write(json.dumps(vectors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
