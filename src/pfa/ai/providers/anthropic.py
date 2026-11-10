"""Claude via the Anthropic API, with structured outputs: the reply must match the schema."""

from __future__ import annotations

from typing import Any

import anthropic

from pfa.ai.providers.base import ProviderError

DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOKENS = 2048


class AnthropicProvider:
    name = "anthropic"
    destination = "api.anthropic.com"
    local = False

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        client: Any = None,
    ):
        self.model = model or DEFAULT_MODEL
        # The SDK retries rate limits (429), overload and 5xx with exponential backoff.
        self.client = client or anthropic.Anthropic(
            api_key=api_key, timeout=timeout, max_retries=max_retries
        )

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.RateLimitError as exc:
            raise ProviderError("Anthropic API rate limit reached (after retries)") from exc
        except anthropic.AuthenticationError as exc:
            raise ProviderError("Anthropic API rejected the API key (ANTHROPIC_API_KEY)") from exc
        except anthropic.APITimeoutError as exc:
            raise ProviderError("Anthropic API timed out") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"cannot reach the Anthropic API: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc

        if response.stop_reason == "refusal":
            raise ProviderError("the model declined to answer")
        if response.stop_reason == "max_tokens":
            raise ProviderError(f"the reply was cut off at {MAX_TOKENS} tokens")
        text = "".join(b.text for b in response.content if b.type == "text")
        if not text.strip():
            raise ProviderError("the model returned an empty reply")
        return text
