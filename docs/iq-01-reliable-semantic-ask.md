# IQ-01 — Reliable Semantic Ask(可靠语义问数)

**Status(状态)：READY FOR CODEX IMPLEMENTATION(可交给 Codex 代码智能体直接实施)**  
**Implementation Baseline(实现基线)：`main(主分支)@220d0cfce4afacf36722139afc41620564b8b622`**  
**Parent Plan(父级计划)：`docs/conversational-analytics-runtime-development-plan.md`**  
**Scope(范围)：只实现 IQ-01(可靠语义问数)，不得提前进入 IQ-02/IQ-03/IQ-04/IQ-05/IQ-06**

---

## 1. 目标

这张卡只解决当前单次 Ask(问数) 主链的四个产品问题：

1. **低模型置信度不再提前阻断真实数据发现。**
2. **真正需要澄清时，必须基于真实 Grounding(语义落地) 候选给出具体问题和具体选项。**
3. **真实结构中没有所需数据时，必须具体说明缺失的数据能力，不再要求用户笼统“确认指标、维度和时间范围”或要求用户知道物理表字段。**
4. **数据库真实查询成功后，Answer Composer(答案编排器) 可以基于已验证 Result(结果) 生成面向业务用户的回答；模型不可用、输出为空或答案引入无证据数字时，确定性降级回答仍必须成功。**

这张卡完成后的核心行为：

```text
用户问题
  ↓
Rule Facts(规则事实)
  ↓
LLM Intent(大语言模型意图)
  ↓
Semantic Retrieval(语义检索)
  ↓
Grounding(语义落地)
  ├─ 唯一真实绑定 → 继续执行
  ├─ 多个真实绑定 → 给具体候选让用户选择
  └─ 真实结构不支持 → 具体说明缺什么
  ↓
现有 Plan / Validation / Revision Fence / Read-only Execution
现有计划 / 校验 / 扫描版本围栏 / 只读执行
  ↓
Typed Result + Evidence(类型化结果 + 证据)
  ↓
Answer Composer(答案编排器)
  ├─ Grounded Model Answer(有数据依据的模型回答)
  └─ Deterministic Fallback(确定性降级回答)
```

这张卡**不改变数据库事实来源**。最终业务数字仍以现有 Typed Result(类型化结果) 和 Evidence(证据) 为准。

---

## 2. 当前真实代码问题

Codex(代码智能体) 开始修改前必须先阅读以下文件，不要只根据本卡猜代码：

```text
smartdata/semantic/clarification.py
smartdata/semantic/intent.py
smartdata/semantic/grounding.py
smartdata/application/service.py
smartdata/llm/ports.py
smartdata/llm/gateway.py
smartdata/contracts/api.py
smartdata/contracts/query.py

tests/unit/semantic/test_intent_understanding.py
tests/unit/semantic/test_grounding.py
tests/unit/application/test_unified_ask.py
tests/unit/application/test_ask_stream.py
tests/unit/interfaces/test_api_ask_stream.py
tests/unit/cli/test_main.py
```

### 2.1 Low Confidence Gate(低置信度门) 是当前截图问题的直接根因

当前 `ClarificationBuilder(澄清构造器)`：

```python
if query.confidence < self.confidence_threshold and not requests:
    ...
    question="当前业务意图置信度较低，请确认指标、维度和时间范围。"
```

导致：

```text
Intent(意图)
→ confidence < 0.6
→ clarification_required(需要澄清)
→ return
```

Semantic Retrieval(语义检索) 和 Grounding(语义落地) 根本没有机会查看真实企业数据图。

这与当前主架构文档中“confidence(置信度) 只作为证据，不作为固定产品正确性阈值”的原则冲突。

### 2.2 Grounding(语义落地) 已经具备正确的确定性边界

当前 `SemanticGrounder(语义落地器)` 已经能够：

- 唯一真实物理绑定时自动选择；
- 多个不同物理绑定时拒绝猜测；
- 生成真实 Candidate Label(候选标签)；
- 没有已确认关系时拒绝通过同名字段推断 Join(关联)；
- 把 Grounding Confidence(语义落地置信度) 只作为 Evidence(证据)。

IQ-01(可靠语义问数) 不重写这套机制。

问题主要在于：

- 无候选时只有 `unresolved_ambiguities(未解决歧义)` 文本；
- `SmartDataService(智能数据服务)` 会把它统一包装成“请确认：...”；
- 完全没有 Binding(绑定) 时还会返回“请指定表、指标或字段”，把物理 Schema(模式) 认知负担推给用户。

### 2.3 正式 Ask(问数) 查询成功后仍只使用机械摘要

当前成功链最终：

```python
answer=self._summarize(...)
```

典型输出只是：

```text
已完成“xxx”，返回 N 行数据。
```

`AiyallmSchemaModel(模型网关)` 已有 `answer_question()`，但正式 Ask(问数) 主链没有使用。

IQ-01(可靠语义问数) 要把“答案表达”做成独立能力，但不能让模型重新解释数据库结构、重新计划查询或修改 Result(结果)。

---

## 3. 冻结边界

Codex(代码智能体) 必须把下面内容当作硬边界。

### 3.1 本卡允许修改

允许修改：

```text
smartdata/semantic/clarification.py
smartdata/semantic/grounding.py
smartdata/application/service.py
smartdata/llm/ports.py
smartdata/llm/gateway.py
smartdata/answering/*              # 可以新增，推荐
相关 tests(测试)
必要的 docs(文档)
```

如果实现需要对其它文件做很小的 Contract(契约) 兼容修改，可以做，但必须在最终报告说明原因。

### 3.2 本卡禁止提前实现

不得实现：

- IQ-02(五数据库可信执行) 的 PostgreSQL(关系数据库)/MySQL(关系数据库)/MongoDB(文档数据库)/Redis(内存键值数据库) 新编译器；
- Conversation(会话)；
- Semantic Memory(语义记忆)；
- Run Runtime(运行实例运行时)；
- Diagnostic Agent Loop(诊断智能体循环)；
- FederatedPlan(联合计划)；
- 跨数据源 Join(关联)；
- JoinMapping(关联映射)；
- 新 Login(登录) / JWT(JSON 网络令牌) / OAuth(开放授权) / RBAC(基于角色的访问控制)；
- Fake Demo Mode(伪演示模式)；
- 缓存假结果；
- 修改现有 TLS(传输层安全)、Excel(电子表格)、MCP(模型上下文协议)、Skill(技能) 架构。

### 3.3 不允许破坏的查询不变量

必须继续保持：

```text
BusinessQuery(业务查询)
→ Semantic Retrieval(语义检索)
→ Grounding(语义落地)
→ GroundedQueryContext(落地查询上下文)
→ GroundedQueryPlan(落地查询计划)
→ Plan Validation(计划校验)
→ Native Query(原生查询)
→ Native Validation(原生查询校验)
→ Revision Fence(扫描版本围栏)
→ Read-only Execution(只读执行)
→ Result + Evidence(结果 + 证据)
```

禁止让 LLM(大语言模型) 直接生成最终可执行 SQL(结构化查询语言)。

禁止为了“回答更智能”绕过 Grounding(语义落地) 或 Validation(校验)。

---

## 4. 实现设计

不要大重构。按下面四个改造点完成。

### 4.1 Clarification Policy(澄清策略)：Confidence(置信度) 不再是阻断条件

修改 `smartdata/semantic/clarification.py`。

目标：

`ClarificationBuilder(澄清构造器)` 只处理**确定性的 Intent-layer Conflict(意图层冲突)**，例如当前 `rule_ambiguities(规则歧义)`。

必须删除：

```text
confidence_threshold
low_confidence_1
“当前业务意图置信度较低，请确认指标、维度和时间范围。”
```

模型输出：

```text
confidence = 0.2
```

但没有确定性 Rule Conflict(规则冲突) 时：

```text
IntentUnderstandingResult.needs_clarification == False
```

然后必须继续进入 Semantic Retrieval(语义检索)。

模型 `BusinessQuery.ambiguities(业务查询歧义说明)` 继续只作为 Advisory Evidence(建议性证据)，不得单独阻断。

必须保留：

> 如果 Rule Layer(规则层) 发现用户写了无法安全解释的约束，继续查询会静默丢条件，则仍然可以在 Intent(意图) 层停止。

### 4.2 Grounding Clarification(语义落地澄清)：从“泛化确认”改为“真实数据解释”

修改 `smartdata/semantic/grounding.py` 和 `smartdata/application/service.py`。

原则：

> Grounding(语义落地) 已经知道系统到底找到了什么或缺了什么，因此用户提示必须来自 Grounding(语义落地)，不能再由 Service(服务) 层统一加“请确认”。

#### A. 没有语义候选

当前：

```text
未找到与指标“发货金额”匹配的语义候选
↓
Service
↓
请确认：未找到...
```

改为 Grounding(语义落地) 自己提供 User-facing Clarification(面向用户的澄清)，例如：

```text
当前已发布数据中没有找到可确认的指标“发货金额”。
如果该数据存在，请先补充对应数据源或发布相应业务指标。
```

如果缺的是 Dimension(维度)、Entity(实体)、Filter Field(过滤字段)，文本应准确指出类别。

不要告诉普通用户：

> 请指定物理表名或字段名。

#### B. 多个真实定义

继续使用现有 `_ambiguity_clarification()` 的真实候选机制。

要求：

- Options(选项) 必须来自真实 Grounding Candidate(语义落地候选)；
- 普通用户首先看到 Business Label(业务标签)；
- 不得把内部 Graph ID(图标识) 当选项文本；
- Physical Field(物理字段) 可以放在 Description(说明) / Evidence(证据) 中，但不是用户必须输入的内容。

例如：

```text
“销售额”有多个企业定义，请确认你指的是：
- 实收销售额
- 含税销售额
```

#### C. 缺少业务时间轴

当前已有明确 unresolved message(未解决消息)，但没有专门 ClarificationRequest(澄清请求)。

改成具体用户提示：

```text
问题包含“最近一个月”，但当前数据源没有可确认的业务时间轴。
已扫描结构不能安全判断应该使用下单日期、支付日期还是其它日期字段。
```

如果已经存在多个真实 Time Axis(时间轴) Candidate(候选)，给真实候选选项。

不要根据“字段看起来像日期”猜。

#### D. 缺少已确认关系

保留 Fail Closed(失败关闭)。

用户提示必须明确：

```text
当前问题需要同时使用 A 和 B，但企业数据图中没有它们之间已确认的关系，
因此 SmartData 当前不会按同名字段或 *_id 自动连接。
```

不要转换成泛化的“请补充问题范围”。

#### E. Service Fallback(服务层兜底)

`SmartDataService(智能数据服务)` 中这段：

```python
question=f"请确认：{message}"
```

不得继续作为正常 Grounding Failure(语义落地失败) 的主要产品文案机制。

Service(服务) 层只负责投影 Grounding(语义落地) 已给出的结构化 Clarification(澄清)。

如果出现理论上不应发生的“无 Binding(绑定)、无 Clarification(澄清)、无具体 unresolved(未解决项)”兜底情况，允许返回：

```text
当前已发布数据中没有找到足够的受治理结构来回答这个问题。
请检查相应数据源是否已经扫描并发布所需业务字段。
```

不能返回：

```text
请指定表、指标或字段。
```

### 4.3 Answer Composer(答案编排器)：模型只能消费已验证结果

新增一个高内聚模块，推荐：

```text
smartdata/answering/__init__.py
smartdata/answering/composer.py
```

不要继续把 Answer(回答) 逻辑堆进 `SmartDataService(智能数据服务)`。

建议最小接口：

```python
class AnswerComposer:
    def compose(
        self,
        *,
        question: str,
        result: GroundedQueryResult,
        analysis: dict[str, Any],
        deterministic_fallback: str,
    ) -> AnswerComposition:
        ...
```

`AnswerComposition(答案编排结果)` 至少包含：

```text
text
source = model | deterministic_fallback
```

可以使用 Dataclass(数据类)；不要为了这张卡建立复杂 Domain Framework(领域框架)。

#### Model Input(模型输入) 边界

模型只能收到经过 Query Engine(查询引擎) 验证后的结果投影：

```text
question
columns
rows
row_count
truncated
deterministic_numeric_summary
```

禁止传入：

- Credential(凭据)；
- Connection String(连接串)；
- Secret(密钥)；
- TLS Material(TLS 材料)；
- 私有 Chain-of-Thought(思维链)；
- 未执行的 Candidate SQL(候选 SQL)；
- 未确认的关系；
- 任意 Graph Internal ID(图内部标识)。

对 `is_sensitive_field()` 命中的敏感列，进入模型前必须移除或替换为 `<redacted>`。不要因为 Answer Composer(答案编排器) 新增外部模型调用而扩大已有数据泄露面。

#### Model Output(模型输出) 边界

模型只负责把真实结果组织成人类可读语言。

模型不得：

- 修改 Result(结果)；
- 修改 Evidence(证据)；
- 重新计划；
- 重新查询；
- 声称看到了未提供的数据；
- 增加数据库结果中没有依据的新业务数字。

增加一个**轻量 Fail-closed Number Guard(失败关闭数字保护)**：

- 从 `question(问题)`、`result(结果)` 和 `analysis(分析)` 收集允许出现的数字 Token(数字标记)；
- 如果模型回答引入新的数字 Token(数字标记)，并且该数字无法在允许集合中找到，则不要把该模型回答作为正式 Answer(回答)；
- 直接使用 Deterministic Fallback(确定性降级回答)。

这张卡不要求实现通用数学证明器。原则是宁可降级，也不要把无数据依据的新数字显示成正式答案。

#### Fallback(降级)

下列任何情况必须保留查询成功状态，并使用当前确定性摘要：

- Answer Model(答案模型) 未配置；
- Answer Model(答案模型) 调用抛 `ModelInvocationError(模型调用错误)`；
- 模型返回空文本；
- Number Guard(数字保护) 失败。

此时：

```text
AskStatus = completed
analysis.answer_source = deterministic_fallback
```

模型回答成功：

```text
analysis.answer_source = model
```

查询已经成功后，Answer Model(答案模型) HTTP 429(请求过多) **不得把整个 Ask(问数) 改成 FAILED(失败)**。

### 4.4 Ask Orchestration(问数编排)：保持事件契约稳定

不要新增或删除 `AskEventType(问数事件类型)`。

成功事件顺序继续是：

```text
accepted
intent_ready
retrieval_ready
grounding_ready
plan_ready
query_ready
execution_started
result_ready
done
```

变化仅在于：

> 一个 `confidence=0.4` 但真实 Grounding(语义落地) 唯一可执行的问题，现在必须走完整成功链，而不是在 `intent_ready` 后直接 `clarification_required`。

Rule Ambiguity(规则歧义) 仍可保持：

```text
accepted
intent_ready
clarification_required
done
```

Grounding Ambiguity(语义落地歧义) 保持：

```text
accepted
intent_ready
retrieval_ready
grounding_ready
clarification_required
done
```

`ask()` 与 `ask_stream()` 必须继续共享同一个内部编排，不允许出现两套行为。

---

## 5. 具体代码落点

Codex(代码智能体) 应优先按以下方式实施；如真实代码要求轻微调整，可以调整，但不要改变职责边界。

| 文件 | IQ-01(可靠语义问数) 职责 |
| --- | --- |
| `smartdata/semantic/clarification.py` | 删除 Low Confidence Gate(低置信度门)，只保留确定性 Intent Conflict(意图冲突) |
| `smartdata/semantic/grounding.py` | 产生具体 Missing / Ambiguous / Relationship / Time-axis Clarification(缺失/歧义/关系/时间轴澄清) |
| `smartdata/answering/composer.py` | 新增 Answer Composer(答案编排器)、敏感字段过滤、Number Guard(数字保护)、模型失败降级 |
| `smartdata/answering/__init__.py` | 导出最小公共能力 |
| `smartdata/application/service.py` | 编排 Composer(编排器)，删除泛化 Grounding Fallback(语义落地兜底)，记录 `analysis.answer_source` |
| `smartdata/llm/ports.py` | 如需要，抽出最小 Answer Model Port(答案模型端口)；不要扩大基础设施依赖 |
| `smartdata/llm/gateway.py` | 保持回答 Prompt(提示词) 只允许基于真实结果，不得补数字 |
| tests(测试) | 更新旧低置信度断言并增加新验收 |

不要把 `Grounding(语义落地)`、`Answer Composer(答案编排器)`、`Application Service(应用服务)` 三个职责混成一个新大类。

---

## 6. 必须新增/修改的测试

测试名称可以根据代码风格调整，但行为必须全部覆盖。

### 6.1 Intent Understanding(意图理解)

更新：

`tests/unit/semantic/test_intent_understanding.py`

必须证明：

1. `confidence=0.4` 且无 Rule Conflict(规则冲突)：
   - `needs_clarification == False`
   - `clarifications == []`
   - `BusinessQuery.confidence` 仍保留为 Evidence(证据)。

2. 模型 `ambiguities=["未指定时间范围"]` 但真实规则无冲突：
   - 不阻断。

3. `orders.status=completed` 这类无法安全落为业务过滤条件的 Rule Ambiguity(规则歧义)：
   - 仍然阻断；
   - 不能静默丢过滤条件。

删除或改写所有依赖 `low_confidence_1` 的旧测试。

### 6.2 Grounding(语义落地)

更新：

`tests/unit/semantic/test_grounding.py`

至少覆盖：

- 唯一真实候选 -> executable(可执行)；
- 多个不同物理绑定 -> 具体候选 Options(选项)；
- 无指标候选 -> 具体说明缺指标；
- 无维度候选 -> 具体说明缺维度；
- 时间表达但无受治理时间轴 -> 具体说明缺业务时间轴；
- 两对象无已确认关系 -> 具体说明关系缺失且不会猜 Join(关联)。

所有用户提示不得退化成：

```text
当前业务意图置信度较低...
请确认指标、维度和时间范围...
请指定表、指标或字段...
```

### 6.3 Ask / Streaming(问数 / 流式)

更新：

```text
tests/unit/application/test_unified_ask.py
tests/unit/application/test_ask_stream.py
tests/unit/interfaces/test_api_ask_stream.py
```

必须证明：

#### Low Confidence Execution(低置信度执行)

```text
confidence = 0.4
+
唯一真实 Grounding(语义落地)
=
继续执行
```

Streaming(流式) 必须出现：

```text
accepted
intent_ready
retrieval_ready
grounding_ready
plan_ready
query_ready
execution_started
result_ready
done
```

#### Real Ambiguity(真实歧义)

真实 Grounding(语义落地) 有两个不同 Metric Binding(指标绑定)：

```text
clarification_required
options = ["实收销售额", "含税销售额"]
```

#### Missing Data(缺失数据)

例如问题“汇总发货数据”，如果当前受治理结构没有任何可确认发货指标：

- 不返回 Low Confidence(低置信度) 泛化文案；
- 返回具体缺失内容；
- 不进入 Plan(计划) / Execute(执行)。

### 6.4 Answer Composer(答案编排器)

新增推荐：

```text
tests/unit/answering/test_composer.py
```

必须覆盖：

1. 模型根据真实 Result(结果) 返回正常回答 -> `source=model`。
2. 模型未配置 -> Deterministic Fallback(确定性降级)。
3. `ModelInvocationError(模型调用错误)` -> Deterministic Fallback(确定性降级)，Ask(问数) 仍 completed(完成)。
4. 模型返回空字符串 -> Fallback(降级)。
5. 模型回答引入 Result(结果)/Question(问题)/Analysis(分析) 中不存在的新数字 -> Fallback(降级)。
6. 敏感列值不进入模型 Payload(负载)。
7. Composer(编排器) 不修改原始 Result(结果)。

### 6.5 Existing Interface Regression(现有接口回归)

现有以下行为必须继续通过：

- `ask()` 和 `ask_stream()` Terminal Response(终态响应) 一致；
- API(应用程序接口) SSE(服务器发送事件) Contract(契约) 不变；
- CLI(命令行界面) 仍只调用 Application Service(应用服务)；
- MCP(模型上下文协议) Progress(进度) 不需要新增事件；
- Schema Inventory(结构清单) 现有模型失败降级行为不受影响；
- Explicit SQL(显式 SQL) 兼容路径不改。

---

## 7. Codex(代码智能体) 执行顺序

不要同时大面积修改。按以下顺序完成，每一步先跑对应测试。

### Step 1 — Remove Confidence Gate(移除置信度门)

修改：

```text
smartdata/semantic/clarification.py
tests/unit/semantic/test_intent_understanding.py
```

先跑：

```bash
pytest -q tests/unit/semantic/test_intent_understanding.py
```

### Step 2 — Make Grounding User-facing(让语义落地直接产生可用澄清)

修改：

```text
smartdata/semantic/grounding.py
smartdata/application/service.py
tests/unit/semantic/test_grounding.py
tests/unit/application/test_unified_ask.py
tests/unit/application/test_ask_stream.py
```

先跑相关 Semantic / Application(语义 / 应用) 测试。

### Step 3 — Add Answer Composer(增加答案编排器)

新增 `smartdata/answering/`，然后在 `SmartDataService(智能数据服务)` 查询成功后调用。

保持：

```text
execution.result
execution.evidence
```

完全不可被模型修改。

### Step 4 — Update API / CLI Test Doubles(API / CLI 测试替身)

当前多个 Fake Intent Model(伪意图模型) 的 `answer_question()` 会主动抛：

```text
Ask does not use an answer model
```

IQ-01(可靠语义问数) 后这条历史假设已经失效。

更新这些 Test Double(测试替身) 时：

- 成功查询场景返回一个基于传入 Result(结果) 的固定测试回答；
- 需要验证 Fallback(降级) 的场景显式抛 `ModelInvocationError(模型调用错误)`；
- 不要把所有测试都 Mock(模拟) 成不经过 Composer(编排器)。

### Step 5 — Full Regression(完整回归)

必须执行：

```bash
pytest -q
ruff check .
```

如果 Repository(仓库) 当前 Node(节点运行时) / Frontend(前端) 环境可用，再执行现有前端测试；本卡不修改前端，因此前端环境不可用不应伪造 PASS(通过)。

---

## 8. Acceptance Scenarios(验收场景)

Codex(代码智能体) 最终报告必须明确逐项给出 PASS(通过) / FAIL(失败) / BLOCKED(阻塞)。

### A. 截图问题回归

输入：

```text
汇总一下发货数据
```

场景 A1：真实 Grounding(语义落地) 能唯一绑定一个可执行发货汇总指标。

期望：

```text
不因 confidence(置信度) 低提前停止
→ Retrieval(检索)
→ Grounding(语义落地)
→ Query(查询)
→ Result(结果)
→ Answer(回答)
```

场景 A2：真实数据里存在多个发货口径。

期望返回具体业务选项，例如：

```text
发货单数量
发货商品数量
发货金额
```

不能返回：

```text
请确认指标、维度和时间范围
```

场景 A3：真实结构没有发货相关资产。

期望：

```text
明确告诉用户当前已发布数据中没有找到可确认的发货指标/对象，
并提示补充对应数据源或治理相应业务资产。
```

不能要求用户先知道物理表名。

### B. 销售额真实歧义

输入：

```text
今年销售额是多少？
```

真实 Grounding(语义落地) 有两个正式 Metric(指标) 定义。

期望：

```text
请选择：
- 实收销售额
- 含税销售额
```

澄清来自真实 Candidate(候选)，而不是来自模型自报低置信度。

### C. 查询成功、Answer Model(答案模型) HTTP 429(请求过多)

数据库查询已经成功。

期望：

```text
AskStatus = completed
Result 保留
Evidence 保留
answer = deterministic fallback
analysis.answer_source = deterministic_fallback
```

不得：

```text
AskStatus = failed
```

### D. Model Answer Hallucinated Number(模型回答引入无证据数字)

真实结果：

```text
sales = 120
```

模型回答：

```text
销售额是 150。
```

期望：

```text
拒绝模型回答
→ deterministic fallback
```

### E. Rule Conflict(规则冲突)

输入含不能安全解释的显式物理过滤：

```text
orders.status=completed 的订单数量
```

期望仍然澄清，不允许为了“少问用户”而静默删除过滤条件。

---

## 9. Definition of Done(完成定义)

只有下面全部满足，IQ-01(可靠语义问数) 才能标记 DONE(完成)：

1. 删除固定 Low Confidence Gate(低置信度门)。
2. `BusinessQuery.confidence(业务查询置信度)` 继续保留在 Trace(轨迹) / Evidence(证据) 中，但不单独决定是否执行。
3. Rule Ambiguity(规则歧义) 仍然 Fail Closed(失败关闭)。
4. Grounding Ambiguity(语义落地歧义) 使用真实业务候选选项。
5. Missing Data(缺失数据) 返回具体业务缺口，不要求用户知道表名/字段名。
6. 缺业务时间轴和缺已确认关系有明确用户提示。
7. 低置信度 + 唯一真实绑定能够走完整正式查询链。
8. 新 Answer Composer(答案编排器) 只消费已验证结果。
9. 敏感字段不进入 Answer Model(答案模型) Payload(负载)。
10. Answer Model(答案模型) 失败不破坏已成功数据库查询。
11. 模型引入无证据数字时 Fail Closed(失败关闭) 到确定性摘要。
12. `AskEvent(问数事件)` 类型和成功事件顺序保持兼容。
13. `ask()` 与 `ask_stream()` 继续共享同一编排。
14. Explicit SQL(显式 SQL)、Schema Inventory(结构清单)、CLI(命令行界面)、MCP(模型上下文协议) 不发生架构回归。
15. `pytest -q` 通过。
16. `ruff check .` 通过。
17. 没有 Fake Result(假结果)，没有为了测试加入生产 Fake Mode(伪模式)。

---

## 10. Codex(代码智能体) 最终交付格式

完成代码后，不要只回复“完成”。

最终必须报告：

```text
IQ-01 status: PASS / FAIL / BLOCKED

Changed architecture:
- ...

Changed files:
- ...

Behavior changes:
- low confidence:
- real ambiguity:
- missing data:
- answer composition:
- model failure fallback:

Tests:
- targeted:
- full pytest:
- ruff:

Acceptance:
- A1:
- A2:
- A3:
- B:
- C:
- D:
- E:

Known limitations:
- ...

Next card:
- IQ-02 is NOT started
```

如果任何测试因为外部模型、Neo4j(图数据库) 或其它外部基础设施不可用而无法执行，必须标记 BLOCKED(阻塞) 并给出真实原因；不得伪造 PASS(通过)。
