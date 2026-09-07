"""Single source of truth for selectable chat models.

Every model fact lives here: which provider serves it, how many output tokens it
will emit, and whether it still accepts a sampling temperature. Before this
module the same list was spelled out in five places in ``app.py`` and they were
free to disagree; adding a model to one of them and not the others produced
either a ``KeyError`` or a silent misroute to the wrong provider.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)

Provider = Literal["anthropic"]

#: How hard the model works before answering. Trades tokens for thoroughness;
#: the API default is "high". Lower levels mean fewer, more consolidated tool
#: calls and less preamble, which suits routine lookups.
Effort = Literal["low", "medium", "high", "xhigh", "max"]

EFFORT_LEVELS: tuple[Effort, ...] = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT: Effort = "high"


@dataclass(frozen=True)
class ModelSpec:
    """Everything the app needs to know about one selectable model."""

    model_id: str
    provider: Provider
    #: Maximum output tokens. Verified against the Models API, not guessed.
    max_tokens: int
    #: Maximum input tokens (context window). Also from the Models API.
    context_window: int
    env_key: str
    #: Shortest prefix Anthropic will cache for this model. Below it,
    #: ``cache_control`` is ignored silently — no error, no warning, just no
    #: caching. This is why a small tool set can make caching a no-op.
    min_cacheable_tokens: int
    #: Whether the model accepts ``output_config.effort``. Haiku 4.5 does not —
    #: it still uses the older ``thinking={"type": "enabled", ...}`` form. This
    #: matters when effort control is added; do not send effort where it is
    #: unsupported.
    supports_effort: bool
    #: Whether the model takes ``thinking={"type": "adaptive"}``. Haiku 4.5 does
    #: not — it still uses the older ``{"type": "enabled", "budget_tokens": N}``
    #: form, which this app does not set. Verified against the Models API.
    supports_adaptive_thinking: bool


#: Ordered best-first; the first entry is the default selection.
#: Every value below was verified against the live Models API on 2026-09-04.
#: Re-check with `uv run python scripts/check_models.py` after any change.
MODEL_REGISTRY: dict[str, ModelSpec] = {
    "claude-opus-5": ModelSpec(
        model_id="claude-opus-5",
        provider="anthropic",
        supports_adaptive_thinking=True,
        min_cacheable_tokens=512,
        max_tokens=128_000,
        context_window=1_000_000,
        env_key="ANTHROPIC_API_KEY",
        supports_effort=True,
    ),
    "claude-sonnet-5": ModelSpec(
        model_id="claude-sonnet-5",
        provider="anthropic",
        supports_adaptive_thinking=True,
        min_cacheable_tokens=1_024,
        max_tokens=128_000,
        context_window=1_000_000,
        env_key="ANTHROPIC_API_KEY",
        supports_effort=True,
    ),
    "claude-haiku-4-5-20251001": ModelSpec(
        model_id="claude-haiku-4-5-20251001",
        provider="anthropic",
        supports_adaptive_thinking=False,
        min_cacheable_tokens=4_096,
        max_tokens=64_000,
        context_window=200_000,
        env_key="ANTHROPIC_API_KEY",
        supports_effort=False,
    ),
}

DEFAULT_MODEL = "claude-opus-5"

#: Models this account can reach but the app deliberately does not offer.
#: Recorded so the exclusion reads as a decision rather than an oversight, and
#: enforced by a test — adding one of these to MODEL_REGISTRY fails the suite
#: until the entry here is removed along with it.
RESTRICTED_MODELS: dict[str, str] = {
    "claude-fable-5-1": (
        "Highest-capability tier, priced well above Opus 5. Restricted by "
        "project decision; enable only with an explicit cost sign-off."
    ),
}


def available_models() -> list[str]:
    """Model ids whose provider credentials are actually present.

    An empty-string environment variable counts as absent — the previous
    ``is not None`` check let a blank key advertise a model that then failed at
    call time.
    """
    return [model_id for model_id, spec in MODEL_REGISTRY.items() if os.environ.get(spec.env_key)]


def build_model(model_id: str, effort: Effort = DEFAULT_EFFORT) -> BaseChatModel:
    """Instantiate the chat model for ``model_id``.

    Args:
        model_id: A key of :data:`MODEL_REGISTRY`.
        effort: How hard the model should work. Ignored, with a log line, on
            models that do not accept it — sending it there is a 400.

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
        extra: dict[str, Any] = {}

        if spec.supports_effort:
            extra["output_config"] = {"effort": effort}
        elif effort != DEFAULT_EFFORT:
            logger.info(
                "%s does not accept output_config.effort; ignoring %r",
                model_id,
                effort,
            )

        if spec.supports_adaptive_thinking:
            # display defaults to "omitted", which in a streaming chat UI reads
            # as a long dead pause before any text appears. A summary is not
            # the raw chain of thought; it is what the API is willing to show.
            extra["thinking"] = {"type": "adaptive", "display": "summarized"}

        return ChatAnthropic(  # type: ignore[call-arg]
            model=spec.model_id,
            max_tokens=spec.max_tokens,
            max_retries=3,
            **extra,
        )

    raise ValueError(f"Unsupported provider {spec.provider!r} for model {model_id!r}")
