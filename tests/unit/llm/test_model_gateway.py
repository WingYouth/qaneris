"""Model gateway tests.

The gateway owns SmartData's prompts, schema and failure semantics; aiyallm is only the
invocation infrastructure underneath. These tests therefore mock the **aiyallm boundary**
(``client.chat``) rather than any transport detail, which is also why nothing here needs a
network or a real credential.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiyallm import NoAvailableProviderError, RateLimitError

from smartdata.common.errors import ModelInvocationError, QueryPlanningError
from smartdata.llm import gateway as gateway_module
from smartdata.llm.gateway import AiyallmSchemaModel, build_chat_client

NAMESPACED_MODEL = "deepseek-ai/DeepSeek-V4-Pro-0813"


class FakeChatClient:
    """Stands in for the aiyallm client and records what the gateway asked for."""

    def __init__(self, reply: str = "{}", error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.calls: list[dict] = []

    def chat(self, messages=None, *, model=None, temperature=None):
        self.calls.append({"messages": messages, "model": model, "temperature": temperature})
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=self.reply, model=model, provider="smartdata")


def model_with(
    reply: str = "{}", error: Exception | None = None
) -> tuple[AiyallmSchemaModel, FakeChatClient]:
    client = FakeChatClient(reply=reply, error=error)
    return AiyallmSchemaModel("https://model.example/v1", "key", "model", client=client), client


def test_gateway_calls_the_model_through_aiyallm() -> None:
    model, client = model_with("摘要")

    assert model.summarize_schema({"datasources": []}) == "摘要"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["model"] == "auto"
    assert call["temperature"] == 0
    assert [item["role"] for item in call["messages"]] == ["system", "user"]
    assert "数据目录分析器" in call["messages"][0]["content"]


def test_model_planner_returns_single_authorized_native_query() -> None:
    content = json.dumps(
        {
            "datasource_id": "ds_mongo",
            "dataset": "orders",
            "query": {"collection": "orders", "filter": {"status": "completed"}, "limit": 10},
            "query_language": "mongodb",
            "purpose": "查询已完成订单",
        }
    )
    model, _ = model_with(content)

    plan = model.plan_query("已完成订单", {}, ["ds_mongo"], 10)

    assert plan.datasource_id == "ds_mongo"
    assert json.loads(plan.query)["collection"] == "orders"


def test_model_planner_rejects_unapproved_datasource() -> None:
    content = json.dumps(
        {
            "datasource_id": "ds_forbidden",
            "dataset": "orders",
            "query": "SELECT * FROM orders",
            "query_language": "sql",
        }
    )
    model, _ = model_with(content)

    with pytest.raises(QueryPlanningError, match="未授权"):
        model.plan_query("订单", {}, ["ds_allowed"], 10)


def test_model_planner_rejects_non_json_plan() -> None:
    model, _ = model_with("not-json")

    with pytest.raises(QueryPlanningError, match="JSON 查询计划"):
        model.plan_query("订单", {}, ["ds_allowed"], 10)


def test_unconfigured_model_stays_unconfigured(monkeypatch) -> None:
    for name in (
        "SMARTDATA_MODEL_BASE_URL",
        "SMARTDATA_MODEL_API_KEY",
        "SMARTDATA_MODEL_NAME",
    ):
        monkeypatch.delenv(name, raising=False)

    assert AiyallmSchemaModel.from_environment() is None


@pytest.mark.parametrize(
    "missing",
    ["SMARTDATA_MODEL_BASE_URL", "SMARTDATA_MODEL_API_KEY", "SMARTDATA_MODEL_NAME"],
)
def test_partial_configuration_is_still_unconfigured(monkeypatch, missing: str) -> None:
    monkeypatch.setenv("SMARTDATA_MODEL_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("SMARTDATA_MODEL_API_KEY", "key")
    monkeypatch.setenv("SMARTDATA_MODEL_NAME", "model")
    monkeypatch.delenv(missing)

    assert AiyallmSchemaModel.from_environment() is None


def test_configured_environment_builds_the_gateway(monkeypatch) -> None:
    monkeypatch.setenv("SMARTDATA_MODEL_BASE_URL", "https://model.example/v1/")
    monkeypatch.setenv("SMARTDATA_MODEL_API_KEY", "key")
    monkeypatch.setenv("SMARTDATA_MODEL_NAME", NAMESPACED_MODEL)

    model = AiyallmSchemaModel.from_environment()

    assert model is not None
    assert model.base_url == "https://model.example/v1"
    assert model.model == NAMESPACED_MODEL


def test_namespaced_model_id_is_not_split_into_provider_and_model() -> None:
    """Regression guard for aiyallm's ``provider/model`` syntax.

    aiyallm reads a slash in a model reference as ``provider/model`` and hands the endpoint only
    the part after it, so a namespaced id would arrive truncated. The configured id must reach
    the provider verbatim.
    """
    client = build_chat_client(
        base_url="https://model.example/v1", api_key="key", model=NAMESPACED_MODEL
    )

    targets = client.registry.resolve_candidates("auto", default_model=client.default_model)

    assert [target.model for target in targets] == [NAMESPACED_MODEL]


def test_provider_failures_become_smartdata_errors() -> None:
    failure = RateLimitError("smartdata rate limit reached: 429", provider="smartdata")
    model, _ = model_with(error=failure)

    with pytest.raises(ModelInvocationError, match="模型调用失败") as raised:
        model.summarize_schema({})

    # The provider SDK exception type must not reach the caller.
    assert not isinstance(raised.value, type(failure))
    assert isinstance(raised.value.__cause__, RateLimitError)


def test_provider_error_detail_never_carries_the_api_key() -> None:
    secret = "sk-secret-value"
    failure = RateLimitError(
        f"smartdata request failed: 401 invalid api key {secret}", provider="smartdata"
    )
    model = AiyallmSchemaModel(
        "https://model.example/v1", secret, "model", client=FakeChatClient(error=failure)
    )

    with pytest.raises(ModelInvocationError) as raised:
        model.summarize_schema({})

    assert secret not in str(raised.value)
    assert "<redacted>" in str(raised.value)


def test_nested_provider_reason_survives_the_boundary() -> None:
    """aiyallm hides per-provider reasons in ``NoAvailableProviderError.errors``.

    The operator needs the reason — "insufficient balance", "invalid model" — not just
    "no provider succeeded".
    """
    reason = RateLimitError("429 insufficient balance", provider="smartdata")
    failure = NoAvailableProviderError("No provider succeeded for 'auto'", errors=[reason])
    model, _ = model_with(error=failure)

    with pytest.raises(ModelInvocationError) as raised:
        model.summarize_schema({})

    assert "insufficient balance" in str(raised.value)


def test_gateway_does_not_submit_chat_completions_itself() -> None:
    """The gateway must reach a model through aiyallm, not by posting to an endpoint."""
    source = Path(gateway_module.__file__).read_text(encoding="utf-8")

    assert "httpx" not in source
    assert "/chat/completions" not in source
    assert "from aiyallm import" in source or "import aiyallm" in source
