"""Tests for ``providers.openai.OpenAIEmbeddingProvider``.

HTTP is mocked. Per AGENTS.md, we mock the provider class's HTTP
client (its ``requests.post`` call), not ``requests`` globally.
"""
import math
from unittest.mock import patch

import requests

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.orc_client_semantic_search.providers.base import (
    EmbeddingProviderError,
)
from odoo.addons.orc_client_semantic_search.providers.openai import (
    OpenAIEmbeddingProvider,
)


def _make_provider(**overrides):
    defaults = dict(
        url="https://api.openai.test/v1/embeddings",
        api_key="sk-test",
        model="text-embedding-3-small",
        dim=4,
    )
    defaults.update(overrides)
    return OpenAIEmbeddingProvider(**defaults)


class _FakeResponse:
    """Minimal stand-in for requests.Response — just what the
    provider needs to parse a successful or failed call."""

    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload

    @property
    def ok(self):
        return 200 <= self.status_code < 300


@tagged("orc_client_semantic_search", "post_install", "-at_install")
class OpenAIProviderHappyPathTests(TransactionCase):
    def test_embed_returns_one_vector_per_input(self):
        # OpenAI's response shape: data[].embedding, in input order.
        # The provider should preserve that order on the way out.
        p = _make_provider()
        payload = {
            "data": [
                {"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]},
                {"index": 1, "embedding": [0.0, 1.0, 0.0, 0.0]},
            ],
            "model": "text-embedding-3-small",
            "usage": {"prompt_tokens": 10, "total_tokens": 10},
        }
        with patch(
            "odoo.addons.orc_client_semantic_search.providers.openai.requests.post",
            return_value=_FakeResponse(200, payload),
        ) as mock_post:
            out = p.embed(["alpha", "beta"])
        self.assertEqual(len(out), 2)
        self.assertEqual(len(out[0]), 4)
        self.assertEqual(len(out[1]), 4)
        # Header carries the API key as Bearer.
        _, kwargs = mock_post.call_args
        self.assertIn("Authorization", kwargs.get("headers", {}))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-test")

    def test_embed_returns_l2_normalised_vectors(self):
        # We rely on this downstream so cosine == dot product. If
        # the provider class ever ships unnormalised vectors, the
        # search method's "score in [0, 1]" promise breaks.
        p = _make_provider()
        payload = {"data": [{"index": 0, "embedding": [3.0, 4.0, 0.0, 0.0]}]}
        with patch(
            "odoo.addons.orc_client_semantic_search.providers.openai.requests.post",
            return_value=_FakeResponse(200, payload),
        ):
            out = p.embed(["x"])
        magnitude = math.sqrt(sum(v * v for v in out[0]))
        self.assertAlmostEqual(magnitude, 1.0, places=5)

    def test_provider_tag_includes_model(self):
        p = _make_provider(model="text-embedding-3-large")
        self.assertEqual(p.provider_tag(), "openai:text-embedding-3-large")


@tagged("orc_client_semantic_search", "post_install", "-at_install")
class OpenAIProviderErrorPathTests(TransactionCase):
    def test_401_raises_with_status(self):
        # The cron worker uses ``status`` to decide retry policy:
        # 401 means stop trying (fix the key); 5xx means retry.
        p = _make_provider()
        with patch(
            "odoo.addons.orc_client_semantic_search.providers.openai.requests.post",
            return_value=_FakeResponse(401, {"error": {"message": "bad key"}}),
        ):
            with self.assertRaises(EmbeddingProviderError) as ctx:
                p.embed(["x"])
        self.assertEqual(ctx.exception.status, 401)

    def test_5xx_raises_with_status(self):
        p = _make_provider()
        with patch(
            "odoo.addons.orc_client_semantic_search.providers.openai.requests.post",
            return_value=_FakeResponse(503, {"error": {"message": "overload"}}),
        ):
            with self.assertRaises(EmbeddingProviderError) as ctx:
                p.embed(["x"])
        self.assertEqual(ctx.exception.status, 503)

    def test_dimension_mismatch_raises(self):
        # Provider class is configured with dim=4 but the response
        # has dim=8 — that's a config error (wrong model) and we
        # want to surface it loudly rather than silently store
        # mis-shaped vectors.
        p = _make_provider(dim=4)
        payload = {"data": [{"index": 0, "embedding": [0.0] * 8}]}
        with patch(
            "odoo.addons.orc_client_semantic_search.providers.openai.requests.post",
            return_value=_FakeResponse(200, payload),
        ):
            with self.assertRaises(EmbeddingProviderError):
                p.embed(["x"])

    def test_network_error_wraps_in_provider_error(self):
        # A raw ConnectionError from `requests` becomes our typed
        # EmbeddingProviderError so the cron worker only has one
        # exception to catch.
        p = _make_provider()
        with patch(
            "odoo.addons.orc_client_semantic_search.providers.openai.requests.post",
            side_effect=requests.ConnectionError("dns fail"),
        ):
            with self.assertRaises(EmbeddingProviderError):
                p.embed(["x"])

    # ------------------------------------------- reported usage

    def test_reports_usage_on_success(self):
        p = _make_provider(dim=4)
        payload = {
            "data": [{"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]}],
            "usage": {"prompt_tokens": 9, "total_tokens": 9},
        }
        with patch(
            "odoo.addons.orc_client_semantic_search.providers.openai.requests.post",
            return_value=_FakeResponse(200, payload),
        ):
            p.embed(["x"])
        self.assertEqual(p.last_usage_tokens, 9)

    def test_reports_none_when_the_endpoint_omits_usage(self):
        # Optional across the OpenAI-compatible ecosystem. Absence is
        # normal, and the cron estimates instead of refusing.
        p = _make_provider(dim=4)
        payload = {"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]}]}
        with patch(
            "odoo.addons.orc_client_semantic_search.providers.openai.requests.post",
            return_value=_FakeResponse(200, payload),
        ):
            p.embed(["x"])
        self.assertIsNone(p.last_usage_tokens)

    def test_reports_usage_even_when_the_payload_is_malformed(self):
        # A 200 carrying usage but no `data` was billed all the same.
        # Capturing usage only after validating `data` would make this
        # class of failure look free, and the cron charges failures
        # from exactly this attribute — so an endpoint failing this way
        # could be retried past the daily cap indefinitely.
        p = _make_provider(dim=4)
        with patch(
            "odoo.addons.orc_client_semantic_search.providers.openai.requests.post",
            return_value=_FakeResponse(200, {"usage": {"total_tokens": 77}}),
        ):
            with self.assertRaises(EmbeddingProviderError):
                p.embed(["x"])
        self.assertEqual(p.last_usage_tokens, 77)

    def test_reports_usage_even_on_a_dimension_mismatch(self):
        p = _make_provider(dim=4)
        payload = {
            "data": [{"index": 0, "embedding": [0.0] * 8}],
            "usage": {"total_tokens": 55},
        }
        with patch(
            "odoo.addons.orc_client_semantic_search.providers.openai.requests.post",
            return_value=_FakeResponse(200, payload),
        ):
            with self.assertRaises(EmbeddingProviderError):
                p.embed(["x"])
        self.assertEqual(p.last_usage_tokens, 55)

    def test_a_stale_usage_value_is_cleared_before_each_call(self):
        # Otherwise a network failure — which costs nothing — would be
        # charged whatever the previous successful call reported.
        p = _make_provider()
        p.last_usage_tokens = 999
        with patch(
            "odoo.addons.orc_client_semantic_search.providers.openai.requests.post",
            side_effect=requests.ConnectionError("dns fail"),
        ):
            with self.assertRaises(EmbeddingProviderError):
                p.embed(["x"])
        self.assertIsNone(p.last_usage_tokens)
