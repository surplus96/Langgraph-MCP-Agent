"""Single source of truth for selectable chat models.

Every model fact lives here: which provider serves it, how many output tokens it
will emit, and whether it still accepts a sampling temperature. Before this
module the same list was spelled out in five places in ``app.py`` and they were
free to disagree; adding a model to one of them and not the others produced
either a ``KeyError`` or a silent misroute to the wrong provider.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from langchain_core.language_models import BaseChatModel

Provider = Literal["anthropic"]


@dataclass(frozen=True)
class ModelSpec:
    """Everything the app needs to know about one selectable model."""

    model_id: str
    provider: Provider
    max_tokens: int
    env_key: str
    #: Current Claude models reject ``temperature`` with a 400; only the older
    #: Haiku line still accepts it. We never send it, but the flag documents why.
    supports_temperature: bool


#: Ordered best-first; the first entry is the default selection.
MODEL_REGISTRY: dict[str, ModelSpec] = {
    "claude-opus-5": ModelSpec(
        model_id="claude-opus-5",
        provider="anthropic",
        max_tokens=128_000,
        env_key="ANTHROPIC_API_KEY",
        supports_temperature=False,
    ),
    "claude-sonnet-5": ModelSpec(
        model_id="claude-sonnet-5",
        provider="anthropic",
        max_tokens=128_000,
        env_key="ANTHROPIC_API_KEY",
        supports_temperature=False,
    ),
    "claude-haiku-4-5-20251001": ModelSpec(
        model_id="claude-haiku-4-5-20251001",
        provider="anthropic",
        max_tokens=64_000,
        env_key="ANTHROPIC_API_KEY",
        supports_temperature=True,
    ),
}

DEFAULT_MODEL = "claude-opus-5"


def available_models() -> list[str]:
    """Model ids whose provider credentials are actually present.

    An empty-string environment variable counts as absent — the previous
    ``is not None`` check let a blank key advertise a model that then failed at
    call time.
    """
    return [model_id for model_id, spec in MODEL_REGISTRY.items() if os.environ.get(spec.env_key)]


def build_model(model_id: str) -> BaseChatModel:
    """Instantiate the chat model for ``model_id``.

    Raises:
        KeyError: if ``model_id`` is not in the registry.
    """
    spec = MODEL_REGISTRY[model_id]

    if spec.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        # No ``temperature``: current Claude models removed sampling parameters
        # and reject them with a 400. max_retries gives us the SDK's built-in
        # exponential backoff on 408/409/429/5xx instead of failing a whole turn.
        # `model` and `max_tokens` are the documented kwargs and work at
        # runtime (populate_by_name), but their pydantic aliases are what mypy
        # sees in the generated __init__, so it reports them as unknown.
        return ChatAnthropic(  # type: ignore[call-arg]
            model=spec.model_id,
            max_tokens=spec.max_tokens,
            max_retries=3,
        )

    raise ValueError(f"Unsupported provider {spec.provider!r} for model {model_id!r}")
