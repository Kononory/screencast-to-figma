from processor.ai.base import AIProvider
from processor.ai.gemini_provider import GeminiProvider
from processor.ai.no_ai_provider import NoAIProvider


def get_ai_provider(provider_name: str | None) -> AIProvider:
    normalized = (provider_name or "gemini").lower().strip()

    if normalized in {"gemini", "google", "google-gemini"}:
        return GeminiProvider()

    if normalized in {"none", "no_ai", "no-ai", "off"}:
        return NoAIProvider()

    # Unknown provider — current code path runs Gemini regardless of value,
    # so fall back here to preserve that behavior.
    return GeminiProvider()
