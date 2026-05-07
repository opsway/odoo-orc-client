"""Tests for ``utils.cosine``."""
import numpy as np
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.orc_client_semantic_search.utils.cosine import top_k


def _norm(v):
    """Normalise to unit length so dot product equals cosine similarity."""
    arr = np.array(v, dtype=np.float32)
    return arr / np.linalg.norm(arr)


@tagged("orc_client_semantic_search", "post_install", "-at_install")
class CosineTopKTests(TransactionCase):
    def test_ranks_candidates_by_cosine_descending(self):
        # Pre-normalised vectors so dot product == cosine similarity.
        # The query points along (1, 0); the candidate aligned with
        # it scores 1, the orthogonal one scores 0, the inverse -1.
        q = _norm([1.0, 0.0])
        cands = [
            ("knowledge.article", 1, _norm([1.0, 0.0])),
            ("knowledge.article", 2, _norm([0.0, 1.0])),
            ("knowledge.article", 3, _norm([-1.0, 0.0])),
        ]
        out = top_k(q, cands, limit=10)
        self.assertEqual(len(out), 3)
        self.assertEqual([r[1] for r in out], [1, 2, 3])
        self.assertAlmostEqual(out[0][2], 1.0, places=5)
        self.assertAlmostEqual(out[1][2], 0.0, places=5)
        self.assertAlmostEqual(out[2][2], -1.0, places=5)

    def test_respects_limit(self):
        q = _norm([1.0, 0.0])
        cands = [
            ("m", i, _norm([1.0, 0.0])) for i in range(5)
        ]
        out = top_k(q, cands, limit=3)
        self.assertEqual(len(out), 3)

    def test_empty_candidates_returns_empty(self):
        # The semantic_search method calls top_k after fetching
        # rows; an empty corpus must not crash.
        q = _norm([1.0, 0.0])
        self.assertEqual(top_k(q, [], limit=10), [])

    def test_returns_model_id_score_tuples(self):
        # The return shape is what semantic_search forwards (after
        # mapping into dicts). Pin the tuple positions so the wire
        # contract doesn't drift.
        q = _norm([1.0, 0.0])
        cands = [("knowledge.article", 42, _norm([1.0, 0.0]))]
        out = top_k(q, cands, limit=10)
        self.assertEqual(len(out), 1)
        model, res_id, score = out[0]
        self.assertEqual(model, "knowledge.article")
        self.assertEqual(res_id, 42)
        self.assertIsInstance(score, float)
