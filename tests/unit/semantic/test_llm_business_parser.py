"""Intent parsing through the model port.

The BusinessQuery contract, its JSON schema, parsing and validation all belong to SmartData, so
these tests exercise them through the gateway while mocking only the aiyallm boundary.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from smartdata.common.errors import IntentParsingError
from smartdata.llm.gateway import AiyallmSchemaModel


class FakeChatClient:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[dict] = []

    def chat(self, messages=None, *, model=None, temperature=None):
        self.calls.append({"messages": messages, "model": model, "temperature": temperature})
        return SimpleNamespace(content=self.reply)


def model(reply: str = "{}") -> tuple[AiyallmSchemaModel, FakeChatClient]:
    client = FakeChatClient(reply)
    return AiyallmSchemaModel("https://model.example/v1", "key", "model", client=client), client


def test_gateway_parses_schema_constrained_business_query() -> None:
    content = json.dumps(
        {
            "question": "今年销售额",
            "objective": "lookup",
            "metrics": ["销售额"],
            "time_expression": "今年",
            "requested_output": ["scalar"],
            "confidence": 0.9,
        }
    )
    gateway, client = model(content)

    query = gateway.parse_business_query("今年销售额", {"time_expression": "今年"})

    assert query.metrics == ["销售额"]
    assert query.time_expression == "今年"
    # The prompt carries SmartData's own schema, built from the SmartData contract; aiyallm only
    # transports it.
    payload = json.loads(client.calls[0]["messages"][1]["content"])
    assert "business_query_schema" in payload
    assert "metrics" in payload["business_query_schema"]["properties"]


@pytest.mark.parametrize(
    "content",
    [
        "not-json",
        json.dumps(
            {
                "question": "销售额",
                "objective": "lookup",
                "metrics": ["销售额"],
                "datasource_id": "ds_forbidden",
            }
        ),
        json.dumps(
            {
                "question": "销售额",
                "objective": "lookup",
                "metrics": ["销售额"],
                "sql": "SELECT SUM(amount) FROM orders",
            }
        ),
        json.dumps(
            {
                "question": "已完成订单",
                "objective": "lookup",
                "filters": [
                    {
                        "subject": "状态",
                        "operator": "=",
                        "value": "已完成",
                        "field_path": "orders.status",
                    }
                ],
            }
        ),
    ],
)
def test_gateway_rejects_invalid_or_out_of_contract_model_output(content: str) -> None:
    gateway, _ = model(content)

    with pytest.raises(IntentParsingError, match="有效的 BusinessQuery"):
        gateway.parse_business_query("销售额", {})


def test_summarize_schema_returns_the_model_text() -> None:
    gateway, client = model("工作区包含 2 个数据源。")

    summary = gateway.summarize_schema({"datasources": [], "datasets": []})

    assert summary == "工作区包含 2 个数据源。"
    assert len(client.calls) == 1
