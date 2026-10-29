"""Optional AI provider interface.

The core application works with no AI provider configured. Providers must be
OpenAI-compatible (``/chat/completions``), which covers NVIDIA NIM and most
self-hosted gateways. API keys are read from environment variables only and are
never written to the database, logs, reports, or the UI.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

import requests

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "meta/llama-3.1-70b-instruct"


class AIUnavailable(RuntimeError):
    """Raised when no provider is configured or the provider fails."""


class AIProvider(Protocol):
    name: str

    def available(self) -> bool:  # pragma: no cover - protocol
        ...

    def complete(self, system: str, user: str, temperature: float = 0.2, max_tokens: int = 1200) -> str:  # pragma: no cover
        ...


@dataclass
class NullProvider:
    reason: str = "No AI provider configured."

    name: str = "none"

    def available(self) -> bool:
        return False

    def complete(self, system: str, user: str, temperature: float = 0.2, max_tokens: int = 1200) -> str:
        raise AIUnavailable(self.reason)


@dataclass
class OpenAICompatibleProvider:
    base_url: str
    model: str
    api_key: str
    timeout: int = 60

    name: str = "openai-compatible"

    def available(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def complete(self, system: str, user: str, temperature: float = 0.2, max_tokens: int = 1200) -> str:
        if not self.available():
            raise AIUnavailable("Provider is not fully configured.")
        url = self.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise AIUnavailable(f"Network error contacting the AI provider: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            # Never leak the key or the full request into the error surfaced to the UI.
            raise AIUnavailable(f"AI provider returned HTTP {response.status_code}: {response.text[:300]}")
        try:
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise AIUnavailable("AI provider returned an unexpected response shape.") from exc


def redact(key: Optional[str]) -> str:
    if not key:
        return "(not set)"
    return f"{key[:4]}...{key[-4:]}" if len(key) > 12 else "(set)"


def provider_config() -> Dict[str, Any]:
    """Describe the configured provider without exposing the secret."""
    api_key = os.getenv("AI_API_KEY") or os.getenv("NVIDIA_API_KEY") or ""
    base_url = os.getenv("AI_BASE_URL", DEFAULT_BASE_URL)
    model = os.getenv("AI_MODEL", DEFAULT_MODEL)
    return {
        "configured": bool(api_key),
        "base_url": base_url,
        "model": model,
        "key": redact(api_key),
        "enabled": os.getenv("AI_ENABLED", "0").lower() in {"1", "true", "yes"},
    }


def get_provider() -> AIProvider:
    config = provider_config()
    if not config["configured"] or not config["enabled"]:
        reason = "AI is disabled or no API key is set (AI_API_KEY / NVIDIA_API_KEY)." if not config["configured"] else "AI_ENABLED is not set to 1."
        return NullProvider(reason=reason)
    return OpenAICompatibleProvider(
        base_url=config["base_url"],
        model=config["model"],
        api_key=os.getenv("AI_API_KEY") or os.getenv("NVIDIA_API_KEY") or "",
    )


def test_connection() -> tuple[bool, str]:
    """One short round trip to confirm credentials work. Returns (ok, message)."""
    provider = get_provider()
    if not provider.available():
        return False, getattr(provider, "reason", "Provider unavailable.")
    try:
        text = provider.complete(
            system="Reply with the single word: ready",
            user="ping",
            temperature=0.0,
            max_tokens=8,
        )
        return True, f"Provider responded: {text.strip()[:60]}"
    except AIUnavailable as exc:
        return False, str(exc)
