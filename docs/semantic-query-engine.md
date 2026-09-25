# Qaneris Semantic Query Engine(语义查询引擎设计)

## 1. Formal Pipeline(正式主链)

```text
Question(问题)
→ RuleFacts(规则事实)
→ LLM Intent Parsing(大语言模型意图解析)
→ BusinessQuery(业务查询)
→ Semantic Retrieval(语义检索)
→ Grounding(语义落地)
→ GroundedQuery(已落地查询)
→ QueryContext(查询上下文)
→ QueryPlan(查询计划)
→ Plan Validation(计划校验)
→ Query Generation(查询生成)
→ Native Validation(原生查询校验)
→ Permission Gate(权限门)
→ Execution(执行)
→ Typed Result(类型化结果)
→ Evidence / QueryTrace(证据 / 查询轨迹)
```

> `QueryTrace`(查询轨迹)只表示可公开的结构化执行事实，例如 Intent 摘要、Grounding 绑定、QueryPlan、Native Query、Execution、Evidence 和 Error；它不是模型私有 Chain-of-Thought(思维链)，产品界面不得要求或展示隐藏推理文本。

## 2. Responsibility Boundary(职责边界)

### RuleFactExtractor(规则事实提取器)

只提取日期、数字、显式运算符、范围、Top-N(前N名)等确定性事实，不做真实字段绑定。

### LLMIntentParser(大语言模型意图解析器)

输入：question(问题) + rule_facts(规则事实) + BusinessQuery Schema(业务查询结构约束)。  
输出：结构化 BusinessQuery(业务查询)。

禁止猜测 datasource_id(数据源标识)、真实 DataObject(数据对象) / Field(字段)、Relationship(关系)或最终 SQL / CQL / Cypher / Flux(原生查询语言)。

### SemanticRetriever(语义检索器)

从 Neo4j Enterprise Data Graph(Neo4j企业数据图)与 Semantic Asset Registry(语义资产注册表)召回 SemanticCandidate(语义候选)。只召回，不最终选择。

### Grounder(语义落地器)

状态：COMPLETE(完成)。

- 把业务表达绑定到真实 Graph Identity(图物理身份)。
- 多物理目标歧义进入 Clarification(澄清)。
- Allowlist(允许列表)只由成功绑定产生。
- Relationship(关系)只来自真实 `RELATES_TO`。
- 跨数据源绑定不进入可执行查询。

### QueryContextBuilder(查询上下文构建器)

状态：COMPLETE(完成，P1-04C1 + P1-04C1.1 + P1-04C1.2)。

只消费 `GroundingResult`(语义落地结果)，产出 `GroundedQueryContext`(已落地查询上下文)。

- 不重新检索、不读取 `CompanyDataProfile`(企业数据画像)物理结构、不重新扫描数据库、不调用模型。
- 语义落地未解决歧义或仍需 Clarification(澄清)时，以 `QueryContextBuildError` 失败关闭，不产出可执行 QueryContext。
- Allowlist(允许列表)只由成功绑定推导，并与 `GroundedQuery`(已落地查询)的 Allowlist 完全一致。
- 过滤值、时间表达、排名方向与数量原样携带，不做二次解析。
- 时间维度强制经过 `TimeSpec.dimension` → 已落地绑定 → 日期/时间类型校验；无法落地的维度、时间字段非日期类型或同时存在 `time_range` 但无 `TimeSpec` 全部以结构化错误失败关闭；不再接受调用方以字段路径直接命名时间轴。
- `TimeSpec` 已被收敛为只携带 `dimension`(已落地业务维度)：自然语言时间表达由 `BusinessQuery.time_expression` 承担，规范化日期边界由 `TimeRange` 承担，三处单一来源互不冲突。
- **时间轴的选取来自受治理声明，而不是数据类型推断**：只有声明了 `time_axis` 的 DIMENSION 资产才构成业务时间轴。
  该绑定仍须通过完整治理链（图物理身份、可信来源、非名称迁移解析、物理目标唯一），
  `TimeSpec.dimension` 取该维度自身的业务名（如「下单日期」），不是用户说的时间表达。见 §6.2。
- 受治理物理定位由 `GroundedDataObjectRef` 携带 `namespace` / `qualified_name` / `object_kind` / `name`，
  `public.orders` 与 `archive.orders` 因此在上下文中保持独立的物理身份；`name` 来自
  `GraphDataObject.name`（如 `orders`），不是业务词 `销售额`，也不是 `qualified_name`。
- 检索阶段把 FIELD candidate 的 owning `GraphDataObject` 定位一并下沉，filter slot 仅绑定裸
  FIELD 时，`QueryContext.data_objects` 仍可拿到表级 native locator；不需要回到图。
- 受治理聚合语义由 `QueryContextBinding.default_aggregation` 携带；Metric 资产在缺少
  `default_aggregation` 且没有结构化 `DerivationSpec` 覆盖时，Context 构建以
  `没有受治理聚合语义，也没有结构化衍生计算覆盖` 失败关闭，规划器与上下文一致按
  `derivation > default_aggregation > fail-closed` 选择聚合函数，不回退到 `SUM` 兜底。
- `_candidate_object_locator` 接受同一数据对象的多种候选：empty locator 不会与 populated
  locator 形成冲突；多种 populated locator 互相矛盾时失败关闭；候选顺序反转不影响结果。

### QueryPlanner(查询规划器)

状态：COMPLETE(完成，P1-04C1 + P1-04C1.1 + P1-04C1.2，确定性规则版)。

只消费 GroundedQuery(已落地查询)与 QueryContext(查询上下文)。输出结构化 QueryPlan(查询计划)。

实现为 `GroundedQueryPlanner`(确定性规则规划器)：

- 聚合函数按以下顺序选择：`DerivationSpec.operation` 覆盖 > `QueryContextBinding.default_aggregation` > 失败关闭；Metric 资产没有受治理聚合语义时规划器拒绝构造计划并以 `缺少受治理聚合语义` 报错，非可加性指标不再被默认 `SUM` 兜底；无任何指标时回退到 `COUNT(*)`。
- 排名方向、排名数量、过滤、时间范围、连接全部取自上下文，不重新解析问题文本。
- 计划中的每一处物理引用都是图物理身份(datasource / data object / field path / field id)，不保存业务词作为身份；`data_objects` 字典中 `GroundedDataObjectRef.name` 是来自 `GraphDataObject.name` 的 native name。
- 计划携带 `data_objects` 字典(每个 `data_object_id` 对应一份 `GroundedDataObjectRef`，含 `namespace` / `qualified_name` / `object_kind` / `name`)与 `scan_version`，Generator 可直接消费而无需回查图；执行期校验可据此拒绝基于旧版图的计划。
- 未引入 LLM Planner(大语言模型规划器)，留待后续增强阶段。

不得：

- 引入 Allowlist(允许列表)之外的对象、字段或关系。
- 重新解释业务词。
- 自行创造 Join Path(连接路径)。
- 直接执行数据库查询。
- 对 Metric 资产进行 `SUM` 兜底。

### PlanValidator(查询计划校验器)

状态：COMPLETE(完成，P1-04C1 + P1-04C1.1 + P1-04C1.2)。

`GroundedPlanValidator` 只校验、不修复：计划校验失败即失败。

校验项：数据源与工作区范围、Allowlist(允许列表)之外的对象/字段、图字段标识一致性、Join 必须来自
已确认关系并使用其自身连接字段、禁止跨数据源 Join、未解决歧义时禁止规划、计划不得携带原生查询文本或
查询语言、重复引用与别名、时间范围与时间字段成对出现、`data_objects` 字典必须与 `data_object_ids` 一一对应
且每个 locator 的 `(datasource_id, data_object_id, name, namespace, qualified_name, object_kind)` 全字段
与 Context 完全一致（篡改任意字段即拒绝）、聚合函数必须与 Metric 受治理语义一致、
`scan_version` 形成硬栅栏：当 `context.scan_version` 已设置时 `plan.scan_version` 必须严格相等，
`plan.scan_version=None` 也被拒绝（仅 `context.scan_version=None` 时可允许 plan 端空值，作为 legacy 兼容）。

`NativeQueryValidator`(原生查询校验)与只读安全校验仍是独立边界，属 P1-04C2。

### QueryGenerator(查询生成器)

把经过 Plan Validation(计划校验)的 QueryPlan(查询计划)编译为目标数据源的原生只读查询。

它不重新解释用户语义，不增加新物理资产。

状态：COMPLETE(完成，P1-04C2 + P1-04C2.1，SQLite / SQL 基准实现)。

- `GroundedSQLCompiler` 只消费已验证的 `GroundedQueryPlan`，不读取图、画像、注册表或模型。
- 标识符只来自计划中的物理 locator 并安全引用；缺少 native object `name` 时失败关闭。
- 业务值与 `LIMIT` 保持为 `command + parameters` 直到 SQLite driver parameter binding；
  `display_command` 不包含参数值。
- `IN` 过滤器只接受 `list / tuple`；拒绝 `set`，保证 `command` 与 `parameters` 在多次编译下完全一致。
- Native Validation 独立检查只读、单语句与 placeholder 数量；读只检查在 strip 引号 / 注释 / 字符串字面量
  之后进行，所以合法的 quoted identifier(`"delete"`)不会被错判成 DELETE 语句。
- 执行前 `ExecutionRevisionValidator` 要求 `native.scan_version` 严格等于当前发布图 `scan_version`。
- 执行返回 `GroundedQueryResult` 与不含敏感参数的 `ExecutionEvidence`。
- Provenance 校验：`GroundedNativeQueryValidator.validate(native, plan, expected=expected)` 比较
  `datasource_id` / `plan_id` / `scan_version` / `query_language` / `command` / `parameters` 六字段是否
  与编译产物一致；同 identifier 但 `command` / `parameters` 被替换的伪造 native 一律拒绝。
- **正式执行入口 `QanerisService.execute_grounded_plan(context, plan, workspace_id, max_rows)`**
  七步串行执行：Plan Validation → Compiler → Native Validation（含 provenance）→ 读图拿当前
  `scan_version` → Revision Validation → Adapter 执行 → `GroundedExecution`。
  原 `execute_grounded_query(native, ...)` 仅作为内部 primitive，不再是 Application Service 的公开授权入口。
- 内部 SQLite adapter 还附加 `mode=ro` 与 `PRAGMA query_only = ON` 两道 driver 级防线，是 validator 之上的
  兜底。

## 3. LLM Role Model(大语言模型角色设计)

模型实现可以共享同一个底层 Provider(提供商)，但逻辑职责分开：

1. Intent Model(意图模型)：自然语言 → BusinessQuery。
2. Embedding Model(嵌入模型)：Phase 2 检索向量化。
3. Reranker Model(重排模型)：Phase 2 候选重排。
4. Planner Model(规划模型)：在 GroundedQuery + Allowlist 约束下辅助生成结构化 QueryPlan。
5. Answer Model(回答模型)：Typed Result + Evidence + QueryTrace → 最终自然语言回答。

这些是 Role(角色)，不要求部署五个不同模型。业务模块应依赖 `qaneris/llm/` 抽象，不直接绑定具体 Provider(提供商)。

## 4. Grounding Decision(语义落地决策)

第一版优先 Deterministic Logic(确定性逻辑)。

未来如使用 LLM(大语言模型)辅助 Grounding，只允许在系统提供的有限候选集合中判断，结果仍须通过图物理身份校验。

## 5. Clarification(澄清)

典型触发：

- “收入”同时匹配净收入、含税收入、回款金额。
- 同一术语对应多个业务实体。
- 多个 DataSource(数据源)都有合理候选且用户未指定范围。
- 无法形成唯一合法关系路径。

Clarification 输出只展示必要的人类可读业务选项，不泄漏敏感样本值。

### 5.1 Ambiguity Policy(歧义策略)

执行与否由**物理层**裁决，不由模型的措辞裁决：

| 来源 | 是否阻断执行 | 理由 |
| --- | --- | --- |
| 语义落地出现多个不同物理目标 | **阻断** | 两个受治理定义指向不同物理字段，除追问别无正解；澄清携带候选 label |
| 规则层派生歧义（`BusinessQueryMergePolicy.rule_ambiguities`） | **阻断** | 确定性输入无法表达为业务过滤（例如用户写了物理字段路径），若继续执行会静默丢弃该约束并返回错误答案 |
| 意图置信度低于阈值（默认 0.6） | **阻断** | 意图本身不可靠 |
| 意图模型自报的 `ambiguities` | **不阻断** | 业务定义由受治理资产裁定，模型不二次否决治理；作为可追溯证据保留在 `AskResponse.analysis.intent_ambiguities` |

理由：意图模型被要求上报它注意到的一切细节，因此它会对普通问题也给出
「未指定时间范围」「统计口径是否扣除退款」这类注记。若每条注记都阻断执行，管线行为就取决于
所配置模型的啰嗦程度，而不是已发布的企业数据图。真实环境验收（
[Phase 1 Real LLM E2E Report](../tmp/phase1_e2e_acceptance_report.md) §5.2）实测该现象在同一问题多次运行间
不稳定，会让本应可回答的问题随机失败。

## 6. Phase Breakdown(阶段拆分)

- P1-04A Semantic Retrieval(语义检索)：COMPLETE(完成)。
- P1-04B Grounding(语义落地)：COMPLETE(完成)。
- P1-04C1 QueryContext / QueryPlan Contract(查询上下文 / 查询计划契约)：COMPLETE(完成)。
- P1-04C1.1 Execution Readiness Hardening(执行可读性强化)：COMPLETE(完成)。受治理物理定位 + 受治理聚合语义 + 结构化时间维度 + 扫描版本均已纳入契约。
- P1-04C1.2 Final Execution Boundary Corrections(最终执行边界修正)：COMPLETE(完成)。`GroundedDataObjectRef.name` 收敛到 `GraphDataObject.name`；FIELD candidate 由检索阶段下沉 owning object locator；locator 校验覆盖 `name` / `namespace` / `qualified_name` / `object_kind` 全字段且要求 `plan.data_objects == data_object_ids`；`scan_version` 形成硬栅栏（`plan=None` 也被拒绝）；`TimeSpec` 收敛为只携带 `dimension`，自然语言表达回归 `BusinessQuery.time_expression`；Context 与 Planner 在 `derivation > default_aggregation > fail-closed` 聚合规则上对齐。
- P1-04C2 Query Generator / Execution(查询生成 / 执行)：COMPLETE(完成)。
- P1-04C2.1 Execution Trust Boundary & Acceptance Closeout(执行信任边界与验收收尾)：COMPLETE(完成)。正式 `execute_grounded_plan` 七步入口；`GroundedNativeQueryValidator` 增加 provenance（六字段）；`read_only` validator 改用引号 / 注释剥离以正确识别 quoted identifier / 注释 / 字符串字面量中的关键字；`IN` 拒绝 `set` 保证确定性。
- P1-04D Unified Ask Pipeline Core Implementation(统一问数流水线核心实现)：COMPLETE(完成，待真实外部 E2E 验收)。

所有 API / CLI / MCP / Web 入口最终统一调用 Application Service(应用服务)主链，不复制问数逻辑。

## 6.1 Unified Ask Orchestration(统一问数编排)

`QanerisService.ask()` 是唯一正式自然语言 Ask 主链：

```text
RuleExtractor + LLMBusinessParser
→ BusinessQuery
→ retrieve_semantics(requested_datasource_id)
→ ground_semantics
→ Clarification OR build_query_context
→ plan_grounded_query
→ execute_grounded_plan
→ GroundedQueryResult + ExecutionEvidence
```

- LLM 只通过 `parse_business_query()` 产生 `BusinessQuery`；自然语言路径不调用模型 SQL planner，也不回退
  到 `CompanyDataProfile` / `simple_planner`。
- `AskRequest.datasource_id` 作为 `requested_datasource_id` 进入 Intent facts 并原样传给 Retrieval / Context；
  未指定数据源时由 workspace 图检索与 Grounding 决定，禁止从 ready datasource 中随意选择第一个。
- Intent 或 Grounding 需要澄清时返回 `status=clarification_required`，且 `result=None`；公开选项仅包含
  用户可理解的 label，不暴露 graph / field identifier。
- 时间表达先由 `RuleExtractor` 提取并确定性归一化为 `TimeRange`；时间轴的选取见 §6.2，
  并通过 `TimeSpec.dimension` 进入 Context。无法归一化时间范围或时间轴不唯一时返回澄清，不默认选择日期字段。
- 成功结果携带 `BusinessQuery`、`GroundedQueryPlan`、`GroundedQueryResult` 与 `ExecutionEvidence`；
  失败继续抛出既有结构化领域错误，Clarification 不作为 Error。
- `request.sql` 仅保留为明确隔离的 legacy explicit-native-query compatibility path；它不是自然语言失败时的 fallback。

## 6.2 Governed Time Axis(受治理业务时间轴)

问题：意图模型只表达业务含义，因此「近30天按下单地区看实收销售额前10名」这类问题会给出
`dimensions=["下单地区"]` 与 `time_expression="近30天"`，**不会**把「下单日期」列进 dimensions。
若时间轴要求从「已落地的 Dimension」里挑选，则时间轴永远为空，此类问题只能澄清。
真实环境验收复现了该现象（[Phase 1 Real LLM E2E Report](../tmp/phase1_e2e_acceptance_report.md) §5.2）。

设计（显式声明，不做推断）：

```text
SemanticAssetSeed / SemanticAsset / SemanticCandidate   time_axis: bool
        ↓ 治理链（可信来源 + 图确认物理身份 + 非名称迁移解析 + 物理目标唯一）
GroundingBinding.time_axis   →   ask() 取 TimeSpec.dimension
```

规则：

- 只有**声明了 `time_axis` 的 DIMENSION 资产**才是业务时间轴。管理员显式声明；
  下游不得按字段名、数据类型或「第一个 date 字段」推断。
- 检索阶段：当 `BusinessQuery.time_expression` 存在时，按**结构召回**（与已确认关系同一机制）
  提供受治理时间轴候选，不依赖词面匹配——「近30天」与「下单日期」本就没有词面重叠。
- 落地阶段：时间轴走 `GroundingSlot.TIME_AXIS`，复用与其他槽位相同的治理与唯一性判定。
  - 0 个受治理时间轴 → 失败关闭并澄清；
  - ≥2 个指向不同物理列的时间轴 → 澄清并给出候选 label；
  - 恰好 1 个 → 确定性绑定。
- `TimeSpec.dimension` 取该维度自身的业务名（如「下单日期」），不是用户的时间表达；
  Context Builder 仍校验其为 date/datetime/timestamp/timestamptz，作为最后一道守卫。
- 没有时间表达时**不**绑定时间轴，避免给无时间语义的查询加上时间过滤。

## 7. Grounded Query Contracts(落地后查询契约)

物理确定性区域自 Grounding(语义落地)之后开始：此后不再重新解释业务语义。

```text
GroundedQuery(已落地查询)
→ GroundedQueryContext(查询上下文)
→ GroundedQueryPlan(查询计划)
→ Plan Validation(计划校验)
```

### GroundedQueryContext(查询上下文)

正式契约定义在 `qaneris/contracts/query.py`，由 `qaneris/querying/context.py` 构建。

- 携带：workspace / requested datasource / objective / 已落地绑定 / 指标 / 维度 / 实体 / 过滤 / 排名 /
  衍生计算 / 时间表达与规范化时间范围 / 时间字段 / 期望输出 / 已确认关系 / 四个 Allowlist + 字段标识 Allowlist /
  置信度与证据。
- 只保存成功绑定的物理身份：`datasource_id`、`data_object_id`、`field_id`、`field_path`、`relationship_id`。
- 业务词只作为展示与追溯信息(`business_term`)，不能作为物理身份被再次解析。
- 未解决歧义无法进入该契约：模型层校验器直接拒绝，构建阶段则以结构化错误失败关闭。

### GroundedQueryPlan(查询计划)

正式契约定义在 `qaneris/contracts/query.py`，由 `qaneris/querying/planning/grounded_planner.py` 生成。

- 表达“如何查询”，不表达 SQL / CQL / Cypher / Flux(原生查询语言)，契约中没有命令字段与查询语言字段。
- 支持：字段选择、聚合(count / sum / average / minimum / maximum)、过滤
  (equals / not_equals / greater_than / greater_or_equal / less_than / less_or_equal / between / in / contains)、
  分组、排名(top / bottom + limit)、时间范围、单个已确认关系的直接连接。
- 第一阶段不支持：多跳连接、跨数据源连接、子查询、递归查询、窗口函数、任意表达式、LLM 自由 SQL。
- `plan_id` 由计划内容确定性派生：同一上下文重复规划得到完全相同的计划与标识。

### Legacy Contract(旧契约)

`qaneris/contracts/query.py` 中的 `QueryContext` / `QueryPlan` 是基于 `CompanyDataProfile`(企业数据画像)
的旧契约，保留为兼容路径，供 `QueryPreparationPipeline`(查询准备流水线)与旧 `ask()` 使用。
新能力不得依赖它们；统一 `ask()` 主链属 P1-04D。

## 8. Failure Semantics(失败语义)

至少区分 IntentParsingError(意图解析错误)、GraphUnavailableError(图不可用错误)、NoSemanticCandidate(没有语义候选)、AmbiguousGrounding(语义落地歧义)、GroundingFailed(语义落地失败)、QueryContextBuildFailed(查询上下文构建失败)、QueryPlanningFailed(查询规划失败)、UnsafeQuery(不安全查询)、PermissionDenied(权限拒绝)、ExecutionFailed(执行失败)。

禁止把这些统一包装成“模型回答失败”。
