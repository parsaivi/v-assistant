"""LLM provider interface.

Phase 1 spends nothing, so the default provider is `NullLLM`, which never opens
a socket. Everything in this codebase must work with it — if a feature breaks
when the LLM is null, that feature was built wrong.

The interface exists now so the paid tier drops in later without a rewrite, and
so the privacy tier has exactly one place to enforce egress.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .config import Settings, settings as default_settings


class LLMUnavailable(RuntimeError):
    """Raised when a model is genuinely required and none is configured."""


class PrivacyViolation(RuntimeError):
    """Raised when content from a private folder would leave the machine."""


@dataclass
class Completion:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    provider: str = "null"

    @property
    def free(self) -> bool:
        return self.cost_usd == 0.0


@dataclass
class Provider(ABC):
    name: str = "abstract"

    @abstractmethod
    def complete(self, system: str, user: str, *, max_tokens: int = 800,
                 model: str | None = None) -> Completion:
        ...

    @property
    def available(self) -> bool:
        return False


class NullLLM(Provider):
    """The free provider. Refuses politely instead of silently costing money."""

    name = "null"

    def __init__(self):
        self.calls_refused = 0

    def complete(self, system: str, user: str, *, max_tokens: int = 800,
                 model: str | None = None) -> Completion:
        self.calls_refused += 1
        raise LLMUnavailable(
            "no LLM provider configured — this build is the zero-cost tier. "
            "Every feature has a deterministic path; the caller should use it."
        )

    @property
    def available(self) -> bool:
        return False


class AnthropicLLM(Provider):
    """Wired but inert: constructing it without a key raises before any call.

    Kept here so the paid tier is a config change, not a refactor.
    """

    name = "anthropic"

    PRICES = {  # USD per million tokens (input, output)
        "claude-opus-5": (5.00, 25.00),
        "claude-sonnet-5": (3.00, 15.00),
        "claude-haiku-4-5-20251001": (1.00, 5.00),
    }

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 settings: Settings | None = None):
        import os

        self.s = settings or default_settings
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model or self.s.llm_model
        if not self.api_key:
            raise LLMUnavailable("ANTHROPIC_API_KEY is not set")

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def complete(self, system: str, user: str, *, max_tokens: int = 800,
                 model: str | None = None) -> Completion:
        import json
        import urllib.request

        model = model or self.model
        body = json.dumps({
            "model": model, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"content-type": "application/json",
                     "x-api-key": self.api_key,
                     "anthropic-version": "2023-06-01"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        text = "".join(b.get("text", "") for b in data.get("content", []))
        usage = data.get("usage", {})
        cin, cout = self.PRICES.get(model, (0.0, 0.0))
        it = usage.get("input_tokens", 0)
        ot = usage.get("output_tokens", 0)
        return Completion(text, model, it, ot,
                          it / 1e6 * cin + ot / 1e6 * cout, self.name)


@dataclass
class Guard:
    """Enforces the privacy tier at the single point of egress."""

    settings: Settings = field(default_factory=default_settings.__class__)

    def check(self, rels: list[str]) -> None:
        blocked = [r for r in rels if self.settings.is_private(r)]
        if blocked:
            raise PrivacyViolation(
                "these notes are in private folders and must not be sent to an "
                "API: " + ", ".join(blocked[:5])
            )


def build(settings: Settings | None = None) -> Provider:
    s = settings or default_settings
    if s.llm_provider == "anthropic":
        try:
            return AnthropicLLM(settings=s)
        except LLMUnavailable:
            return NullLLM()
    return NullLLM()
