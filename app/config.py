"""
Application configuration module.

Loads settings from environment variables with sensible defaults.
Secrets (API keys) are never logged or returned in responses.
"""

import os
from dotenv import load_dotenv

# Load .env file if present (local development)
load_dotenv()


class Settings:
    """Centralised application settings loaded from environment variables."""

    PORT: int = int(os.getenv("PORT", "8000"))
    HOST: str = os.getenv("HOST", "0.0.0.0")

    # LLM provider configuration
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    MODEL_NAME: str = os.getenv("MODEL_NAME", "gemini-2.5-flash")

    # Timeout budget (seconds) for the LLM call within each request
    LLM_TIMEOUT_SECONDS: int = int(os.getenv("LLM_TIMEOUT_SECONDS", "15"))

    @property
    def llm_available(self) -> bool:
        """Return True when a valid API key is configured."""
        return bool(self.GEMINI_API_KEY and self.GEMINI_API_KEY != "your_gemini_api_key_here")


settings = Settings()
