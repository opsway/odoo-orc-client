"""Brute-force cosine top-K over stored vector blobs.

Vectors are stored L2-normalised at indexing time (provider class
normalises before returning). Search-time cosine then collapses to
a plain dot product, which lets us write the whole top-K query as
five lines of numpy.

The corpus we expect (knowledge.article + a few helpdesk/attachment
models per tenant) sits comfortably under 100K vectors, where
brute force is ~50ms per query. ANN backends (FAISS, hnswlib) are
explicitly out of scope for v1 — see README "Limits".
"""
from __future__ import annotations

from typing import Iterable

import numpy as np


def top_k(query_vec, candidates: Iterable[tuple], limit: int = 10) -> list[tuple]:
    """Rank ``candidates`` by cosine similarity to ``query_vec``.

    :param query_vec: ``numpy.ndarray`` shape (D,), float32, L2-normalised.
    :param candidates: iterable of ``(model: str, res_id: int, vector: numpy.ndarray)``
        with each ``vector`` shape (D,), float32, L2-normalised.
    :param limit: top-K. The returned list has at most this many items.

    :returns: list of ``(model, res_id, score)`` sorted by descending
        score.
    """
    cands = list(candidates)
    if not cands or limit <= 0:
        return []

    # Stack the vectors once; numpy's matmul beats a Python loop
    # for any non-trivial corpus.
    matrix = np.vstack([c[2] for c in cands])
    scores = matrix @ query_vec  # both already L2-normed → dot == cosine

    # argsort returns ascending; flip + slice to top-K desc.
    take = min(limit, len(cands))
    top_idx = np.argpartition(-scores, take - 1)[:take] if take < len(cands) else np.arange(len(cands))
    top_idx = top_idx[np.argsort(-scores[top_idx])]

    return [
        (cands[i][0], cands[i][1], float(scores[i]))
        for i in top_idx
    ]
