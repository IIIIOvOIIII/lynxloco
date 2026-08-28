"""Explicit Omni protocol selection and legacy-profile compatibility."""

import pytest
from miloco.config.settings import OmniModelSettings
from miloco.perception.engine.omni import provider
from miloco.perception.engine.omni.provider import (
    GeminiAdapter,
    MiMoAdapter,
    QwenOmniAdapter,
)
from pydantic import ValidationError


@pytest.mark.parametrize(
    ("configured", "model", "expected"),
    [
        ("openai_chat_completions", "gemini-3-pro", "openai_chat_completions"),
        ("openai_responses", "gemini-3-pro", "openai_responses"),
        ("gemini_native", "xiaomi/mimo-v2.5", "gemini_native"),
    ],
)
def test_explicit_protocol_overrides_any_model_name(configured, model, expected):
    assert provider.resolve_api_protocol(configured, model) == expected


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("xiaomi/mimo-v2.5", "openai_chat_completions"),
        ("qwen3.5-omni-flash", "openai_chat_completions"),
        ("Gemini-3-Pro", "gemini_native"),
    ],
)
def test_legacy_profile_infers_protocol_from_model_only(model, expected):
    assert provider.resolve_api_protocol(None, model) == expected


def test_base_url_cannot_influence_protocol_resolution():
    legacy = OmniModelSettings.model_validate(
        {
            "model": "local-vision-model",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
        }
    )
    assert legacy.api_protocol is None
    assert provider.resolve_api_protocol(legacy.api_protocol, legacy.model) == (
        "openai_chat_completions"
    )


@pytest.mark.parametrize(
    "value",
    ["responses", "chat", "openai", "gemini", ""],
)
def test_settings_reject_unknown_explicit_protocol(value):
    with pytest.raises(ValidationError):
        OmniModelSettings(api_protocol=value)


def test_legacy_profile_keeps_missing_protocol_unresolved():
    legacy = OmniModelSettings.model_validate(
        {
            "label": "old-local-profile",
            "model": "gemini-2.5-flash",
            "base_url": "http://127.0.0.1:8000/v1beta",
            "api_key": "",
        }
    )
    assert legacy.api_protocol is None


def test_chat_protocol_preserves_model_specialization():
    assert isinstance(
        provider.get_adapter("openai_chat_completions", "xiaomi/mimo-v2.5"),
        MiMoAdapter,
    )
    assert isinstance(
        provider.get_adapter("openai_chat_completions", "qwen3.5-omni-flash"),
        QwenOmniAdapter,
    )
    assert isinstance(
        provider.get_adapter("openai_chat_completions", "gemini-named-local-model"),
        MiMoAdapter,
    )


def test_explicit_gemini_selects_gemini_for_non_gemini_model():
    assert isinstance(provider.get_adapter("gemini_native", "local-vlm"), GeminiAdapter)


def test_explicit_responses_selects_responses_for_any_model():
    assert isinstance(
        provider.get_adapter("openai_responses", "gemini-3-pro"),
        provider.OpenAIResponsesAdapter,
    )


def test_legacy_adapter_selection_uses_model_inference():
    assert isinstance(provider.get_adapter(None, "gemini-3-pro"), GeminiAdapter)
    assert isinstance(provider.get_adapter(None, "qwen3.5-omni-plus"), QwenOmniAdapter)
