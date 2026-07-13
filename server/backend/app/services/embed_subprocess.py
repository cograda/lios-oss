"""Standalone embedding subprocess.

Reads a JSON array of strings from stdin, writes a JSON array of
float-vector arrays to stdout, then exits. The OS reclaims the
fastembed/ONNX arenas on process termination — that's the point.

Invoked by EmbeddingService.process_queue() via subprocess.run().
Not imported by anything in the app runtime.
"""

import json
import sys


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
    vectors = [v.tolist() for v in model.embed(texts)]
    sys.stdout.write(json.dumps(vectors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
