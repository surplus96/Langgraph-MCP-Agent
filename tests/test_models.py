"""The model registry — the thing five duplicated lists used to disagree about."""

from __future__ import annotations

import pytest

from mcp_agent.models import (
    DEFAULT_MODEL,
    MODEL_REGISTRY,
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


def test_unknown_model_raises():
    with pytest.raises(KeyError):
        build_model("gpt-4o")
