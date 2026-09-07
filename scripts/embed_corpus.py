"""Build the dense index. Run once: `python scripts/embed_corpus.py`.

Stored as a plain .npy matrix rather than a vector database: 1075 chunks is a
1075 x 384 float array, roughly 1.6 MB. A brute-force dot product over that is
sub-millisecond, and it keeps the prototype to one dependency instead of a
service to run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import CHUNKS_PATH, EMBEDDINGS_PATH, EMBED_MODEL  # noqa: E402


def main() -> None:
    from sentence_transformers import SentenceTransformer

    with CHUNKS_PATH.open(encoding="utf-8") as fh:
        chunks = [json.loads(line) for line in fh if line.strip()]

    texts = [f"{c['title']}\n{c['text']}" for c in chunks]
    print(f"embedding {len(texts)} chunks with {EMBED_MODEL} ...")
    model = SentenceTransformer(EMBED_MODEL)
    # Small batch + capped sequence length: this runs on CPU on a memory-tight
    # machine, and chunks are already capped at ~1400 chars by the corpus builder.
    model.max_seq_length = 256
    vecs = model.encode(texts, batch_size=8, normalize_embeddings=True,
                        show_progress_bar=True)
    np.save(EMBEDDINGS_PATH, np.asarray(vecs, dtype=np.float32))
    print(f"wrote {vecs.shape} -> {EMBEDDINGS_PATH}")


if __name__ == "__main__":
    main()
