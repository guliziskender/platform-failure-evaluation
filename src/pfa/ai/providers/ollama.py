"""A local model through Ollama's HTTP API. With a localhost URL, nothing leaves the machine."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import httpx

from pfa.ai.providers.base import ProviderError

DEFAULT_MODEL = "qwen3:8b"
_RETRY_STATUS = {429, 500, 502, 503, 504}
# A local model on a laptop needs far longer than a hosted API: qwen3:8b with thinking took
# 47 s for one diagnosis on an M5. Used unless llm_timeout is set explicitly.
DEFAULT_TIMEOUT = 120.0
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def is_local(url: str) -> bool:
    return urlparse(url).hostname in _LOCAL_HOSTS


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        model: str | None = None,
        url: str = "http://localhost:11434",
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 2,
        client: httpx.Client | None = None,
        backoff: float = 1.0,
    ):
        self.model = model or DEFAULT_MODEL
        self.url = url.rstrip("/")
        self.destination = urlparse(self.url).netloc or self.url
        self.local = is_local(self.url)
        self.max_retries = max_retries
        self.backoff = backoff
        self.client = client or httpx.Client(timeout=timeout)

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "format": schema,  # Ollama constrains the reply to this JSON schema
            "stream": False,
            "options": {"temperature": 0},
        }
        for attempt in range(self.max_retries + 1):
            last = attempt == self.max_retries
            try:
                response = self.client.post(f"{self.url}/api/chat", json=body)
            except httpx.TimeoutException as exc:
                # No retry: the model is still busy with this request, and a retry would queue
                # behind it and time out too (30 s became 93 s before falling back).
                raise ProviderError(
                    f"Ollama at {self.url} did not answer within {self.client.timeout.read:.0f} s; "
                    "raise llm_timeout (PFA_LLM_TIMEOUT) for slower models"
                ) from exc
            except httpx.TransportError as exc:
                if last:
                    raise ProviderError(f"cannot reach Ollama at {self.url}: {exc}") from exc
            else:
                if response.status_code == 404:
                    raise ProviderError(
                        f"Ollama has no model {self.model!r}: run `ollama pull {self.model}`"
                    )
                if response.status_code not in _RETRY_STATUS or last:
                    return self._text(response)
            time.sleep(self.backoff * 2**attempt)
        raise AssertionError("unreachable")

    def _text(self, response: httpx.Response) -> str:
        if response.status_code != 200:
            raise ProviderError(f"Ollama error {response.status_code}: {response.text[:200]}")
        try:
            text = response.json()["message"]["content"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("Ollama returned an unexpected response") from exc
        if not text.strip():
            raise ProviderError("the model returned an empty reply")
        return text
