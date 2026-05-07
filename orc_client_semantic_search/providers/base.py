"""Embedding provider abstraction.

The concrete provider class is selected by the ``provider_kind``
field on the global ``orc.embedding.config`` row. Adding a new
provider is either a config swap (for OpenAI-compatible endpoints)
or a new class subclassing ``EmbeddingProvider`` (~30 lines).

Don't import vendor SDKs here. The provider HTTP shape is simple
enough that a ``requests.post`` is clearer than a transitive-dep-
heavy SDK; on Odoo.sh that simplicity matters.
"""
from __future__ import annotations

from typing import Sequence


class EmbeddingProviderError(Exception):
    """Raised by providers when the upstream call fails. Carries the
    upstream HTTP status (when available) so the cron worker can
    distinguish auth failures (don't retry forever) from transient
    5xx (do retry)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class EmbeddingProvider:
    """Stateless provider — instances are cheap. Construct per-call
    with the values from the global config row.

    ``embed(texts)`` returns one float32 vector per input text, in
    order. Vectors are L2-normalised by the provider class so the
    semantic-search method can use a plain dot product instead of
    re-normalising on every query.
    """

    name: str = "base"

    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        model: str,
        dim: int,
        timeout_connect: float = 30.0,
        timeout_read: float = 60.0,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.timeout_connect = timeout_connect
        self.timeout_read = timeout_read

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one L2-normalised float32 vector per input text."""
        raise NotImplementedError

    def provider_tag(self) -> str:
        """Stable string identifying this provider+model combo for
        the ``orc.embedding.provider`` audit field."""
        return f"{self.name}:{self.model}"
