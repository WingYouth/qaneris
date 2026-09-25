"""Language-model gateway implementations.

``aiyallm`` performs model invocation and nothing else. Everything that defines SmartData's
business contract — the prompts, ``BusinessQuery.model_json_schema()``, JSON parsing, Pydantic
validation and the failure semantics — stays on this side of the boundary, and no aiyallm type
(a response object, a routing result, a provider config or an exception) crosses it.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from aiyallm import Aiyallm
from aiyallm.exceptions import AiyallmError
from pydantic import ValidationError

from smartdata.common.errors import (
    IntentParsingError,
    ModelInvocationError,
    QueryPlanningError,
)
from smartdata.contracts import BusinessQuery, QueryPlanStep
from smartdata.llm.config import resolve_model_profile

DEFAULT_TIMEOUT_SECONDS = 60.0

#: Provider name aiyallm registers the single configured endpoint under.
_PROVIDER_NAME = "smartdata"

#: Passed as the model reference on every call. See :func:`build_chat_client` for why the
#: configured model id itself is not passed here.
_SELECTED_MODEL = "auto"

#: Longest provider detail kept in an error message.
_MAX_DETAIL = 240


class ChatClient(Protocol):
    """The slice of the aiyallm client this gateway depends on."""

    def chat(
        self,
        messages: Any = None,
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> Any: ...


def build_chat_client(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Aiyallm:
    """Build the aiyallm client for the one OpenAI-compatible endpoint SmartData is configured with.

    aiyallm reads a slash inside a model reference as ``provider/model`` and hands the provider
    only the part after it. A namespaced id such as ``deepseek-ai/DeepSeek-V4-Pro-0813`` would
    therefore reach the endpoint as ``DeepSeek-V4-Pro-0813``. The configured id is declared on the
    provider verbatim and selected through aiyallm's "every declared model" route, which keeps it
    intact. ``default_model`` is deliberately left unset for the same reason: it is re-read as a
    model reference, which reintroduces the split.
    """
    return Aiyallm(
        providers=[
            {
                "type": "openai_compatible",
                "name": _PROVIDER_NAME,
                "api_key": api_key,
                "base_url": base_url,
                "models": [model],
                "timeout": timeout,
            }
        ],
        default_model=None,
    )


class AiyallmSchemaModel:
    """The SmartData model port, carried by aiyallm.

    The configured endpoint is described by SmartData's own ``SMARTDATA_MODEL_*`` variables; how
    they map onto aiyallm is this class's business, not the semantic or querying layers'.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        client: ChatClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        # Retained only so a provider message echoed back in an error can be scrubbed of the
        # credential before anyone sees it. It is deliberately not a public attribute.
        self._secrets = (api_key,)
        self._client = client if client is not None else self._build_client(timeout)

    def _build_client(self, timeout: float) -> Aiyallm:
        try:
            return build_chat_client(
                base_url=self.base_url,
                api_key=self._secrets[0],
                model=self.model,
                timeout=timeout,
            )
        except AiyallmError as error:
            raise ModelInvocationError(f"模型网关配置无效：{self._detail(error)}") from error

    @classmethod
    def from_environment(cls) -> AiyallmSchemaModel | None:
        """Return the gateway for the selected endpoint set, or ``None`` when none is configured.

        Which set is selected is decided in :mod:`smartdata.llm.config`; an unconfigured model
        stays a recognisable state rather than a silent switch to some unknown external model,
        while an explicitly selected set that cannot be honoured raises.
        """
        profile = resolve_model_profile()
        if profile is None:
            return None
        return cls(profile.base_url, profile.api_key, profile.model)

    def _detail(self, error: BaseException) -> str:
        """Provider detail with any configured credential removed.

        aiyallm reports several failure modes as one generic ``NoAvailableProviderError`` and keeps
        the per-provider reasons in a nested ``errors`` list, so the nested messages are collected
        too: a bare "no provider succeeded" does not tell an operator whether the key was
        rejected, the account is out of credit or the endpoint is unreachable.
        """
        collected: list[str] = []
        self._collect_detail(error, collected, 0)
        detail = "; ".join(dict.fromkeys(item for item in collected if item))
        for secret in self._secrets:
            if secret:
                detail = detail.replace(secret, "<redacted>")
        return detail[:_MAX_DETAIL]

    @classmethod
    def _collect_detail(cls, error: BaseException, collected: list[str], depth: int) -> None:
        if depth > 3:
            return
        text = str(error).strip()
        if text:
            collected.append(text)
        nested = getattr(error, "errors", None)
        for item in nested or ():
            if isinstance(item, BaseException):
                cls._collect_detail(item, collected, depth + 1)

    def _chat(self, system: str, user: str, json_response: bool = False) -> str:
        """Send one system + user turn and return the assistant text.

        ``json_response`` records the port's need for structured output. aiyallm 0.1.3 cannot
        express it: no provider adapter sends ``response_format`` and the public API has no
        structured-output parameter. The requirement therefore stays where it already was —
        SmartData's own prompt asks for a bare JSON object, and the parse + validate step in the
        callers fails closed with ``IntentParsingError`` / ``QueryPlanningError`` when the model
        does not comply.
        """
        try:
            response = self._client.chat(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                model=_SELECTED_MODEL,
                temperature=0,
            )
        except AiyallmError as error:
            # aiyallm already folds provider and transport failures (auth, rate limit, timeout,
            # network, unusable response) into its own hierarchy. Translate once, here, so no
            # provider SDK exception type reaches the caller.
            raise ModelInvocationError(f"模型调用失败：{self._detail(error)}") from error
        return str(getattr(response, "content", "") or "")

    def summarize_schema(self, context: dict[str, Any]) -> str:
        return self._chat(
            "你是数据目录分析器。根据结构、关系和脱敏样本总结每个数据源的业务实体、"
            "关键字段、确定关系和候选跨库映射。不得编造不存在的关系。",
            json.dumps(context, ensure_ascii=False),
        )

    def summarize_schema_inventory(self, question: str, context: dict[str, Any]) -> str:
        """Describe one published datasource using structural evidence only."""
        return self._chat(
            "你是数据目录助手。根据给定数据源已扫描的表名、字段名和结构说明，"
            "用中文简洁回答用户想了解这个数据库里有什么。可以概括从名称能看出的主题，"
            "但必须明确这只是结构推断；不得声称看过表内记录，不得编造表、字段、"
            "关系、业务指标或记录数量。不要重复罗列完整字段清单，清单会单独展示。",
            json.dumps({"question": question, "scanned_structure": context}, ensure_ascii=False),
        )

    def parse_business_query(self, question: str, rule_facts: dict[str, Any]) -> BusinessQuery:
        content = self._chat(
            "你是业务查询意图解析器。只表达业务目标、实体、指标、维度、业务过滤、自然时间、"
            "比较、衍生、排名、期望输出、歧义和置信度。不得输出数据源 ID、表名、集合名、"
            "物理字段、SQL 或其他原生查询。明确数字、日期和运算符必须遵守 rule_facts。"
            "只返回符合 business_query_schema 的 JSON 对象。",
            json.dumps(
                {
                    "question": question,
                    "rule_facts": rule_facts,
                    "business_query_schema": BusinessQuery.model_json_schema(),
                },
                ensure_ascii=False,
            ),
            json_response=True,
        )
        try:
            parsed = json.loads(content)
            return BusinessQuery.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError) as error:
            raise IntentParsingError("模型没有返回有效的 BusinessQuery") from error

    def resolve_followup(self, question: str, context: dict[str, Any]) -> str:
        """Expand a follow-up without selecting physical data or answering it."""
        content = self._chat(
            "只把当前问题补全成独立业务问题。只允许使用 confirmed_semantic_memory 中已经确认的语义。"
            "当前用户明确表达优先于历史；不得增加不存在的业务事实。"
            "不得输出数据库 ID、表名、集合名、字段名、SQL、Mongo/Redis 原生命令。"
            '不要执行查询，不要回答问题。只返回 JSON: {"resolved_question": "..."}。',
            json.dumps({"question": question, "context": context}, ensure_ascii=False),
            json_response=True,
        )
        try:
            resolved = json.loads(content)["resolved_question"]
        except (ValueError, KeyError, TypeError) as error:
            raise IntentParsingError("模型没有返回有效的会话问题") from error
        if not isinstance(resolved, str):
            raise IntentParsingError("模型没有返回有效的会话问题")
        return resolved

    def plan_query(
        self,
        question: str,
        context: dict[str, Any],
        allowed_datasource_ids: list[str],
        max_rows: int,
    ) -> QueryPlanStep:
        contracts = {
            "sql": "单条 SELECT 或 WITH...SELECT 字符串",
            "redis": '{"command":"白名单读取命令","args":[]}',
            "mongodb": '{"collection":"...","filter":{},"projection":{},"limit":N}',
            "couchdb": '{"selector":{},"fields":[],"limit":N}',
            "cassandra": "单条 SELECT CQL",
            "hbase": '{"table":"...","row_prefix":"可选","limit":N}',
            "neo4j": "不含写入关键字的 Cypher",
            "influxdb": "不含 to() 的 Flux",
            "elasticsearch": '{"index":"...","query":{},"size":N}',
            "opensearch": '{"index":"...","query":{},"size":N}',
            "milvus": '{"collection":"...","filter":"...","limit":N}',
            "qdrant": '{"collection":"...","filter":{},"limit":N}',
            "weaviate": "只读 GraphQL query",
        }
        content = self._chat(
            "你是只读数据查询规划器。只能选择 allowed_datasource_ids 中的一个数据源，"
            "不得执行跨源查询，不得生成任何写入、管理或结构修改。根据 datasource.driver "
            "使用对应查询契约。只返回 JSON：datasource_id、dataset、query、"
            "query_language、purpose。query 是对象时必须保留为 JSON 对象。",
            json.dumps(
                {
                    "question": question,
                    "max_rows": max_rows,
                    "allowed_datasource_ids": allowed_datasource_ids,
                    "query_contracts": contracts,
                    "schema_context": context,
                },
                ensure_ascii=False,
            ),
            json_response=True,
        )
        try:
            planned = json.loads(content)
        except json.JSONDecodeError as error:
            raise QueryPlanningError("模型没有返回有效的 JSON 查询计划") from error
        datasource_id = planned.get("datasource_id")
        if datasource_id not in allowed_datasource_ids:
            raise QueryPlanningError("模型选择了未授权或未就绪的数据源")
        query = planned.get("query")
        if isinstance(query, dict):
            query = json.dumps(query, ensure_ascii=False)
        if not isinstance(query, str) or not query.strip():
            raise QueryPlanningError("模型没有返回可执行查询")
        return QueryPlanStep(
            id="q1",
            datasource_id=datasource_id,
            dataset=str(planned.get("dataset") or "query"),
            purpose=str(planned.get("purpose") or f"回答：{question}"),
            query=query,
            query_language=str(planned.get("query_language") or "native"),
        )

    def answer_question(self, question: str, result: dict[str, Any]) -> str:
        return self._chat(
            "你是数据问答助手。只根据给定的真实查询结果回答，不得补充、推测或编造数字。"
            "结果截断时必须明确说明。",
            json.dumps({"question": question, "result": result}, ensure_ascii=False),
        )

    def propose_evidence_questions(self, context):
        from smartdata.diagnostics.models import DiagnosticDecision

        content = self._chat(
            "你是受治理的数据诊断规划器。只能提出下一步需数据库验证的自然业务问题。"
            "不得直接回答原因，不得输出 SQL、表名、字段名、数据源 ID、原生查询或思维链。"
            "只依据诊断目标、已确认会话语义和已验证数据库观察，优先选择少量高价值问题。"
            "历史诊断 finding 只帮助选择方向，不是当前数据库事实，必须重新查询验证。"
            "首轮遇到下降问题时先验证下降和比较基准；基准不明确则请求具体业务澄清，不能默认环比。"
            "证据足够时 action=stop；真正缺少业务定义时 action=clarify。"
            "只返回 JSON 对象：action (query|clarify|stop), questions "
            "(question, purpose, priority), clarification。",
            context.model_dump_json(exclude_none=True),
            json_response=True,
        )
        try:
            return DiagnosticDecision.model_validate_json(content)
        except ValidationError as error:
            raise ModelInvocationError("诊断规划输出不符合契约") from error

    def synthesize_diagnosis(self, context):
        return self._chat(
            "仅根据 verified observations 回答诊断问题。unavailable 只能作为数据缺口。"
            "不得创造数字，不得把相关性表述为确定因果，必须指出数据缺口，结果截断时必须说明。"
            "不得暴露 SQL 参数或内部推理过程。",
            context.model_dump_json(exclude_none=True),
        )


#: Compatibility alias for the pre-migration name. The implementation behind it is aiyallm-backed,
#: so the alias carries no architectural meaning; new code should use ``AiyallmSchemaModel``.
OpenAICompatibleSchemaModel = AiyallmSchemaModel


def summarize_without_model(context: dict[str, Any]) -> str:
    datasource_count = len(context["datasources"])
    dataset_count = len(context["datasets"])
    relation_count = len(context["relations"])
    mapping_count = len(context["mapping_candidates"])
    return (
        f"当前工作区包含 {datasource_count} 个数据源、{dataset_count} 个数据集、"
        f"{relation_count} 条库内关系和 {mapping_count} 组跨库字段候选。"
    )
