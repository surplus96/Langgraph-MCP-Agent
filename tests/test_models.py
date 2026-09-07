"""The model registry — the thing five duplicated lists used to disagree about."""

from __future__ import annotations

import pytest

from mcp_agent.models import (
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    EFFORT_LEVELS,
    MODEL_REGISTRY,
    RESTRICTED_MODELS,
    ModelSpec,
    available_models,
    build_model,
)


def test_default_model_is_registered():
    assert DEFAULT_MODEL in MODEL_REGISTRY


@pytest.mark.parametrize("model_id", list(MODEL_REGISTRY))
def test_every_entry_is_self_consistent(model_id: str):
    spec = MODEL_REGISTRY[model_id]
    assert isinstance(spec, ModelSpec)
    assert spec.model_id == model_id
    assert spec.max_tokens > 0
    assert spec.env_key


def test_no_model_offered_without_its_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert available_models() == []


def test_blank_key_does_not_advertise_a_model(monkeypatch):
    """`is not None` used to let an empty key advertise a model that then failed."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert available_models() == []


def test_key_present_offers_every_anthropic_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert available_models() == list(MODEL_REGISTRY)


@pytest.mark.parametrize("model_id", list(MODEL_REGISTRY))
def test_build_model_never_sends_temperature(monkeypatch, model_id: str):
    """Current Claude models reject sampling parameters with a 400."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    model = build_model(model_id)
    assert model.temperature is None
    assert model.max_tokens == MODEL_REGISTRY[model_id].max_tokens


@pytest.mark.parametrize("model_id", list(MODEL_REGISTRY))
def test_context_window_is_at_least_the_output_budget(model_id: str):
    spec = MODEL_REGISTRY[model_id]
    assert spec.context_window >= spec.max_tokens


def test_registry_matches_values_verified_against_the_models_api():
    """Verified 2026-09-04 via GET /v1/models. Re-check with scripts/check_models.py."""
    verified = {
        "claude-opus-5": (128_000, 1_000_000, True),
        "claude-sonnet-5": (128_000, 1_000_000, True),
        "claude-haiku-4-5-20251001": (64_000, 200_000, False),
    }
    assert set(MODEL_REGISTRY) == set(verified)
    for model_id, (max_tokens, context, effort) in verified.items():
        spec = MODEL_REGISTRY[model_id]
        assert spec.max_tokens == max_tokens
        assert spec.context_window == context
        assert spec.supports_effort is effort


def test_restricted_models_are_not_offered():
    """A restricted model must not reach the selector or the builder."""
    for model_id in RESTRICTED_MODELS:
        assert model_id not in MODEL_REGISTRY
        with pytest.raises(KeyError):
            build_model(model_id)


def test_every_restriction_states_a_reason():
    for model_id, reason in RESTRICTED_MODELS.items():
        assert reason.strip(), f"{model_id} is restricted with no stated reason"


def test_unknown_model_raises():
    with pytest.raises(KeyError):
        build_model("gpt-4o")


# --- Effort and thinking ------------------------------------------------------


def test_effort_reaches_models_that_accept_it(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    model = build_model("claude-opus-5", "xhigh")
    assert model.output_config == {"effort": "xhigh"}


def test_effort_is_withheld_from_models_that_reject_it(monkeypatch):
    """Haiku 4.5 returns a 400 for output_config.effort; it must not be sent."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    model = build_model("claude-haiku-4-5-20251001", "max")
    assert model.output_config is None


def test_adaptive_thinking_asks_for_a_visible_summary(monkeypatch):
    """The API default is "omitted", which streams as a long dead pause."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    model = build_model("claude-opus-5")
    assert model.thinking == {"type": "adaptive", "display": "summarized"}


def test_adaptive_thinking_is_withheld_where_unsupported(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert build_model("claude-haiku-4-5-20251001").thinking is None


@pytest.mark.parametrize("effort", EFFORT_LEVELS)
def test_every_advertised_effort_level_is_accepted(monkeypatch, effort):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert build_model("claude-opus-5", effort).output_config == {"effort": effort}


def test_default_effort_is_a_level_the_ui_offers():
    assert DEFAULT_EFFORT in EFFORT_LEVELS


@pytest.mark.parametrize("model_id", list(MODEL_REGISTRY))
def test_effort_and_thinking_support_agree_with_the_models_api(model_id: str):
    """Verified 2026-09-04: only Haiku 4.5 lacks both."""
    spec = MODEL_REGISTRY[model_id]
    is_haiku = model_id.startswith("claude-haiku")
    assert spec.supports_effort is not is_haiku
    assert spec.supports_adaptive_thinking is not is_haiku
