# pylint: skip-file
# pylint's astroid crashes on this file in CI (F0002 astroid-error).
"""OpenAI embeddings client.

Also covers OpenAI-compatible endpoints (Voyage's compat layer,
Together's, local llama.cpp servers exposing the same shape) — same
HTTP contract, just point ``url`` at the alternative endpoint and
swap the API key.
"""
from __future__ import annotations

import math
from typing import Sequence

import requests

from .base import EmbeddingProvider, EmbeddingProviderError


class OpenAIEmbeddingProvider(EmbeddingProvider):
    name = "openai"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """POST ``/v1/embeddings`` with the configured model.

        Returns one L2-normalised float32 vector per input text in
        the same order. Wraps every failure mode in
        ``EmbeddingProviderError`` so the cron worker only has one
        exception to catch.
        """
        if not texts:
            return []

        # Clear first. A stale value from the previous call would be
        # charged against a call that never reached the upstream —
        # billing the tenant for a DNS failure.
        self.last_usage_tokens = None

        body = {"model": self.model, "input": list(texts)}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                self.url,
                json=body,
                headers=headers,
                timeout=(self.timeout_connect, self.timeout_read),
            )
        except requests.RequestException as exc:
            # ConnectionError, Timeout, etc — collapse to a typed
            # error so the cron worker has one branch to handle.
            raise EmbeddingProviderError(
                f"network error contacting {self.url}: {exc}",
            ) from exc

        if not response.ok:
            # Surface the upstream message + status. Status drives
            # retry policy in the cron worker (401 → don't retry,
            # 5xx → retry with backoff).
            try:
                payload = response.json()
                msg = (
                    payload.get("error", {}).get("message")
                    if isinstance(payload, dict) else None
                )
            except ValueError:
                msg = response.text[:200]
            raise EmbeddingProviderError(
                f"HTTP {response.status_code}: {msg or 'no body'}",
                status=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise EmbeddingProviderError(
                f"unexpected response shape: {exc}",
            ) from exc

        # What the upstream says it billed, for the cron's daily cap.
        # Optional in the OpenAI-compatible ecosystem, so absence is
        # normal, not an error — the caller estimates instead.
        #
        # Read BEFORE validating `data`. A 200 that reports usage but
        # omits or malforms the payload was still billed, and the cron
        # charges failures only from what we captured here; capturing
        # after the validation would make that class of failure look
        # free and let it retry past the daily cap.
        usage = payload.get("usage") if isinstance(payload, dict) else None
        reported = usage.get("total_tokens") if isinstance(usage, dict) else None
        self.last_usage_tokens = reported if isinstance(reported, int) else None

        try:
            data = payload["data"]
        except (KeyError, TypeError) as exc:
            raise EmbeddingProviderError(
                f"unexpected response shape: {exc}",
            ) from exc

        # OpenAI's data[] is in input order; the index field is
        # informational. Trust the array order for forward-compat
        # with providers that omit ``index``.
        out = []
        for item in data:
            try:
                vec = list(item["embedding"])
            except (KeyError, TypeError) as exc:
                raise EmbeddingProviderError(
                    f"missing embedding in response item: {exc}",
                ) from exc

            if len(vec) != self.dim:
                # Wrong-dimension response means the configured
                # ``vector_dim`` doesn't match the chosen model. We
                # surface this loudly rather than silently storing
                # mis-shaped vectors that would corrupt the index.
                raise EmbeddingProviderError(
                    f"dimension mismatch: expected {self.dim}, got {len(vec)}",
                )

            magnitude = math.sqrt(sum(v * v for v in vec))
            if magnitude == 0.0:
                # All-zero vector is degenerate — keep it to avoid
                # crashing but the search will treat it as
                # un-normalised noise. Provider issue, not ours.
                out.append(vec)
            else:
                out.append([v / magnitude for v in vec])

        return out
