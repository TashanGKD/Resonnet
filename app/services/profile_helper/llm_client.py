"""LLM client for profile helper: uses OpenAI-compatible API with AI_GENERATION config."""
from openai import OpenAI

from app.core.config import (
    get_ai_generation_api_key,
    get_ai_generation_base_url,
    get_ai_generation_model,
)


def create_client(base_url: str | None = None, api_key: str | None = None) -> OpenAI | None:
    """Create OpenAI-compatible client for profile helper."""
    key = api_key or get_ai_generation_api_key()
    url = base_url or get_ai_generation_base_url()
    if not key or not url:
        return None
    return OpenAI(api_key=key, base_url=url)


def get_default_model() -> str:
    """Get default model from config."""
    return get_ai_generation_model()
