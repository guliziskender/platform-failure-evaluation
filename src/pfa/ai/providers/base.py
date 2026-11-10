"""The one interface every LLM backend implements."""

from __future__ import annotations

from typing import Any, Protocol


class ProviderError(Exception):
    """The provider could not answer: timeout, rate limit after retries, auth, network."""


class ProviderConfigError(Exception):
    """The provider is not usable as configured. The message tells the user how to fix it."""


class LLMProvider(Protocol):
    name: str  # "anthropic", "ollama"
    model: str
    # Where the data goes, for messages like "sending evidence to api.anthropic.com".
    destination: str
    local: bool  # True when the destination is this machine: nothing leaves it

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        """Return the model's reply: a JSON document meant to match `schema`.

        Validation is the caller's job, so all providers are held to the same checks.
        Raises ProviderError when no reply could be obtained.
        """
        ...
