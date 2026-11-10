"""Pick and configure the LLM provider. The `send_data` switch is enforced here."""

from __future__ import annotations

from pfa.ai.providers.base import LLMProvider, ProviderConfigError, ProviderError
from pfa.config import PROVIDERS, Settings

__all__ = [
    "LLMProvider",
    "ProviderConfigError",
    "ProviderError",
    "data_flow",
    "make_provider",
]


def make_provider(settings: Settings) -> LLMProvider | None:
    """The configured provider, or None when no provider is set (rules only).

    Raises ProviderConfigError when a provider is set but must not or cannot be used.
    """
    if settings.provider is None:
        return None

    if settings.provider == "anthropic":
        _require_send_data(settings, "api.anthropic.com")
        if not settings.anthropic_api_key:
            raise ProviderConfigError(
                "the anthropic provider needs an API key: set ANTHROPIC_API_KEY "
                "(or anthropic_api_key in the config file)."
            )
        from pfa.ai.providers.anthropic import AnthropicProvider

        return AnthropicProvider(
            model=settings.model,
            api_key=settings.anthropic_api_key,
            timeout=settings.llm_timeout,
        )

    if settings.provider == "ollama":
        from pfa.ai.providers.ollama import OllamaProvider, is_local

        if not is_local(settings.ollama_url):
            _require_send_data(settings, settings.ollama_url)
        return OllamaProvider(
            model=settings.model, url=settings.ollama_url, timeout=settings.llm_timeout
        )

    raise ProviderConfigError(
        f"unknown provider {settings.provider!r}; use one of: {', '.join(PROVIDERS)}."
    )


def data_flow(settings: Settings) -> tuple[LLMProvider | None, str]:
    """The provider to use (or None), and one plain sentence on what leaves the machine."""
    try:
        provider = make_provider(settings)
    except ProviderConfigError as exc:
        return None, (
            f"AI provider {settings.provider} is set but not used: {exc} "
            "Rules only; nothing leaves this machine."
        )
    if provider is None:
        return None, "No AI provider: rules only. Nothing leaves this machine."
    if provider.local:
        return provider, (
            f"AI: {provider.name} {provider.model} at {provider.destination}. "
            "Nothing leaves this machine."
        )
    return provider, (
        f"AI: {provider.name} {provider.model}. Redacted evidence is sent to "
        f"{provider.destination} (send_data is on)."
    )


def _require_send_data(settings: Settings, destination: str) -> None:
    if not settings.send_data:
        raise ProviderConfigError(
            f"not sending evidence to {destination} because send_data is off. Allow it with "
            "--send-data, PFA_SEND_DATA=1 or send_data: true in the config file "
            "(evidence is redacted first; see it with --show-prompt)."
        )
