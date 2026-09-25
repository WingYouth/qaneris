from __future__ import annotations

from copy import deepcopy

from smartdata.answering import AnswerComposer
from smartdata.common.errors import ModelInvocationError
from smartdata.contracts import GroundedQueryResult, QueryLanguage


class AnswerModel:
    def __init__(self, answer: str = "销售额是 120。", error: Exception | None = None):
        self.answer = answer
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def answer_question(self, question: str, result: dict) -> str:
        self.calls.append((question, result))
        if self.error:
            raise self.error
        return self.answer


def result() -> GroundedQueryResult:
    return GroundedQueryResult(
        plan_id="plan-1", datasource_id="ds-1", query_language=QueryLanguage.SQL,
        columns=["sales", "customer_mobile"],
        rows=[{"sales": 120, "customer_mobile": "13800138000"}], row_count=1,
    )


def compose(model: AnswerModel | None, data: GroundedQueryResult | None = None):
    return AnswerComposer(model).compose(
        question="销售额是多少？", result=data or result(),
        analysis={"numeric_summary": {"sales": {"sum": 120}, "customer_mobile": {"sum": 13800138000}}},
        deterministic_fallback="销售额为 120。",
    )


def test_grounded_model_answer_uses_only_redacted_result_projection() -> None:
    model = AnswerModel()
    original = result()
    before = deepcopy(original)

    answer = compose(model, original)

    assert answer.text == "销售额是 120。"
    assert answer.source == "model"
    question, payload = model.calls[0]
    assert question == "销售额是多少？"
    assert set(payload) == {
        "columns", "rows", "row_count", "truncated", "deterministic_numeric_summary",
    }
    assert payload["columns"] == ["sales"]
    assert payload["rows"] == [{"sales": 120}]
    assert "customer_mobile" not in str(payload)
    assert "13800138000" not in str(payload)
    assert original == before


def test_answer_falls_back_without_model_or_on_model_failure() -> None:
    assert compose(None).source == "deterministic_fallback"
    assert compose(AnswerModel(error=ModelInvocationError("HTTP 429"))).source == (
        "deterministic_fallback"
    )
    assert compose(AnswerModel(answer="  ")).source == "deterministic_fallback"


def test_answer_rejects_unsupported_number_in_chinese_sentence() -> None:
    answer = compose(AnswerModel(answer="销售额是150。"))

    assert answer.text == "销售额为 120。"
    assert answer.source == "deterministic_fallback"


def test_truncated_answer_must_disclose_limit() -> None:
    data = result().model_copy(update={"truncated": True})

    assert compose(AnswerModel(), data).source == "deterministic_fallback"
    assert compose(AnswerModel(answer="销售额是 120，结果已截断。"), data).source == "model"


def test_embedded_credentials_and_tls_material_never_reach_answer_model() -> None:
    model = AnswerModel(answer="查询完成。")
    data = GroundedQueryResult(
        plan_id="plan-1", datasource_id="ds-1", query_language=QueryLanguage.SQL,
        columns=["note", "connection_string", "metadata"],
        rows=[{
            "note": "password=hidden-value Bearer abc.123",
            "connection_string": "postgresql://user:secret@host/db",
            "metadata": {"certificate": "-----BEGIN CERTIFICATE-----private-----END CERTIFICATE-----"},
        }],
        row_count=1,
    )

    AnswerComposer(model).compose(
        question="查询 note，token=hidden-token",
        result=data, analysis={}, deterministic_fallback="查询完成。",
    )

    question, payload = model.calls[0]
    serialized = str(payload) + question
    assert "hidden-value" not in serialized
    assert "hidden-token" not in serialized
    assert "abc.123" not in serialized
    assert "postgresql://" not in serialized
    assert "BEGIN CERTIFICATE" not in serialized
