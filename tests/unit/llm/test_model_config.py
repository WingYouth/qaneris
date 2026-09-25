"""Selecting which model endpoint set a process talks to.

These tests pass an explicit mapping instead of patching ``os.environ``, so they describe the
configuration contract directly.
"""

from __future__ import annotations

import pytest

from smartdata.common.errors import ModelInvocationError
from smartdata.llm.config import profile_names, resolve_model_profile
from smartdata.llm.gateway import AiyallmSchemaModel

DEFAULT_SET = {
    "SMARTDATA_MODEL_BASE_URL": "https://default.example/v1",
    "SMARTDATA_MODEL_API_KEY": "default-key",
    "SMARTDATA_MODEL_NAME": "vendor/default-model",
}

MODELSCOPE_SET = {
    "SMARTDATA_MODEL_MODELSCOPE_BASE_URL": "https://api-inference.modelscope.cn/v1",
    "SMARTDATA_MODEL_MODELSCOPE_API_KEY": "modelscope-key",
    "SMARTDATA_MODEL_MODELSCOPE_NAME": "deepseek-ai/DeepSeek-V4-Pro-0813",
}

OPENROUTER_SET = {
    "SMARTDATA_MODEL_OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
    "SMARTDATA_MODEL_OPENROUTER_API_KEY": "openrouter-key",
    "SMARTDATA_MODEL_OPENROUTER_NAME": "nvidia/nemotron-3-ultra-550b-a55b:free",
}


def test_no_configuration_means_no_model() -> None:
    assert resolve_model_profile({}) is None


def test_the_default_set_needs_no_selector() -> None:
    profile = resolve_model_profile(DEFAULT_SET)

    assert profile is not None
    assert profile.name is None
    assert profile.base_url == "https://default.example/v1"
    assert profile.model == "vendor/default-model"


def test_a_partial_default_set_stays_unconfigured() -> None:
    """The pre-existing behaviour: an incomplete default set is simply "no model"."""
    partial = dict(DEFAULT_SET)
    partial.pop("SMARTDATA_MODEL_API_KEY")

    assert resolve_model_profile(partial) is None


def test_the_selector_picks_one_of_several_declared_sets() -> None:
    env = {**DEFAULT_SET, **MODELSCOPE_SET, **OPENROUTER_SET, "SMARTDATA_MODEL_PROFILE": "openrouter"}

    profile = resolve_model_profile(env)

    assert profile is not None
    assert profile.name == "openrouter"
    assert profile.base_url == "https://openrouter.ai/api/v1"
    assert profile.model == "nvidia/nemotron-3-ultra-550b-a55b:free"


def test_the_selector_is_case_insensitive() -> None:
    env = {**MODELSCOPE_SET, "SMARTDATA_MODEL_PROFILE": "  ModelScope  "}

    profile = resolve_model_profile(env)

    assert profile is not None
    assert profile.base_url == "https://api-inference.modelscope.cn/v1"


def test_without_a_selector_the_default_set_wins_even_when_others_exist() -> None:
    """Backward compatibility: existing deployments keep working untouched."""
    env = {**DEFAULT_SET, **MODELSCOPE_SET}

    profile = resolve_model_profile(env)

    assert profile is not None
    assert profile.name is None
    assert profile.base_url == "https://default.example/v1"


def test_selecting_an_undeclared_set_fails_loudly() -> None:
    env = {**DEFAULT_SET, **MODELSCOPE_SET, "SMARTDATA_MODEL_PROFILE": "typo"}

    with pytest.raises(ModelInvocationError, match="未找到模型 profile“typo”") as raised:
        resolve_model_profile(env)

    # The message has to say what *is* available, otherwise the typo is hard to spot.
    assert "MODELSCOPE" in str(raised.value)


def test_selecting_a_set_with_nothing_declared_explains_the_naming_rule() -> None:
    with pytest.raises(ModelInvocationError, match="未找到模型 profile") as raised:
        resolve_model_profile({"SMARTDATA_MODEL_PROFILE": "modelscope"})

    assert "SMARTDATA_MODEL_<名称>_BASE_URL" in str(raised.value)


def test_a_half_written_set_names_the_missing_variable() -> None:
    env = {k: v for k, v in MODELSCOPE_SET.items() if not k.endswith("_API_KEY")}
    env["SMARTDATA_MODEL_PROFILE"] = "modelscope"

    with pytest.raises(ModelInvocationError, match="不完整") as raised:
        resolve_model_profile(env)

    assert "SMARTDATA_MODEL_MODELSCOPE_API_KEY" in str(raised.value)


def test_declared_names_exclude_the_selector_and_the_default_set() -> None:
    env = {**DEFAULT_SET, **MODELSCOPE_SET, **OPENROUTER_SET, "SMARTDATA_MODEL_PROFILE": "modelscope"}

    assert profile_names(env) == ["MODELSCOPE", "OPENROUTER"]


def test_the_selector_slot_cannot_also_be_a_set_name() -> None:
    """``SMARTDATA_MODEL_PROFILE`` is the selector, so it is never listed as a set."""
    env = {"SMARTDATA_MODEL_PROFILE_BASE_URL": "https://x.example/v1"}

    assert profile_names(env) == []


def test_gateway_builds_from_the_selected_set(monkeypatch) -> None:
    for key in list(DEFAULT_SET) + list(MODELSCOPE_SET) + list(OPENROUTER_SET):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**MODELSCOPE_SET, **OPENROUTER_SET}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SMARTDATA_MODEL_PROFILE", "openrouter")

    model = AiyallmSchemaModel.from_environment()

    assert model is not None
    assert model.base_url == "https://openrouter.ai/api/v1"
    assert model.model == "nvidia/nemotron-3-ultra-550b-a55b:free"
