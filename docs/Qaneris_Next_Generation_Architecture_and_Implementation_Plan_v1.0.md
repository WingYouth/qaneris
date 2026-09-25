# Qaneris Next-Generation Architecture & Implementation Plan(智能数据平台下一代架构与实施计划)

**Version(版本)：1.0**  
**Revision(修订)：2026-09-20，Phase 1 可信查询核心已完成；短期交付优先级切换为 4-Day Roadshow Release(四天路演版本) 产品化收口**  
**Status(状态)：Target Architecture + Phase 1 Complete + Roadshow Productization Current(目标架构 + 阶段一完成 + 路演产品化当前主线)**

> **Current Status(当前状态)**：Phase 1 Trusted Query Engine 已 COMPLETE。
> 固定真实 LLM + Neo4j + SQLite 验收集曾完成六类问题 × 3 次，18/18 符合预期；该结果不代表总体准确率 100%。
> 当前四天优先级是 Roadshow Release：16 个服务型测试库服务器部署与 Qaneris 验收、14 个 SSL/TLS、证书生命周期、Excel、Web / CLI / Skill / MCP、Streaming、Trace / SQL、Visualization 与演示视频。
> 当前实现与下一步以 [Development Roadmap](development-roadmap.md) 和 `main` 源码为准。

# 0. 文档定位

本文件是 Qaneris(智能数据平台) 当前开发的权威主架构文档。后续模块设计不得与本文件冲突。

详细开发信息按职责收敛到以下持续维护文档；历史设计与旧阶段专项说明由 Git 保存：

- [Development Roadmap(开发路线图)](development-roadmap.md)：当前阶段、Roadshow Scope 与下一步。
- [Semantic Query Engine(语义查询引擎设计)](semantic-query-engine.md)：Intent、Retrieval、Grounding、Planning、Validation 与 Execution 边界。
- [Product Interfaces(产品接口设计)](product-interfaces.md)：Web / API / CLI / Skill / MCP、Streaming、Trace、Visualization 与 Excel。
- [Connection Model(连接与SSL/TLS设计)](connection-model.md)：数据源连接、Secret、证书与 CredentialStore。
- [Roadshow Overview(路演总览)](roadshow-overview.md)：对外口径与 Demo 范围。

本次修订把数据库初始化链路进一步简化：

```text
Enterprise Databases(企业数据库)
        ↓
Scan(扫描)
        ↓
Graph Builder(图构建器)
        ↓
Neo4j Enterprise Data Graph(Neo4j 企业数据图)
```

核心原则只有两条：

1. **Scan(扫描)必须尽可能全面地读取企业数据库真实可访问的结构信息。**
2. **Relationship(关系)只保存能够确认的真实关系；有关系就写，没有关系就不写。**

不再把 CompanyDataProfile(企业数据画像)作为产品架构中的独立输出层。现有 `ScanSnapshot`(扫描快照)、`DataSourceProfile`(数据源画像)、`CompanyDataProfile`(企业数据画像)等可以在迁移期作为代码内部兼容结构，但不再作为未来主架构的核心数据层。

`database_profile.md` 等 Markdown(标记文档)不再是 Scan(扫描)主输出，只作为可选的人类可读派生结果。

---

# 1. Qaneris(智能数据平台)目标架构

```text
┌──────────────────────────────────────────────────────────┐
│ Interaction Layer(交互层)                               │
│ Web / API / CLI / Skill / MCP                         │
└─────────────────────────┬────────────────────────────────┘
                          ▼
┌──────────────────────────────────────────────────────────┐
│ Agent Runtime(智能体运行时)                              │
│ Goal / Planner / Task DAG / Runtime / Trace              │
│ 目标 / 规划 / 任务图 / 运行时 / 轨迹                    │
└─────────────────────────┬────────────────────────────────┘
                          ▼
┌──────────────────────────────────────────────────────────┐
│ Semantic Query Engine(语义查询引擎)                      │
│ Intent / Retrieval / Grounding / Planning / Validation   │
│ 意图 / 检索 / 语义落地 / 规划 / 校验                    │
└─────────────────────────┬────────────────────────────────┘
                          ▼
┌──────────────────────────────────────────────────────────┐
│ Semantic & Data Runtime(语义与数据运行时)                 │
│ Neo4j Enterprise Data Graph(Neo4j 企业数据图)            │
│ Semantic Assets / Permission / Query Compiler            │
│ 语义资产 / 权限 / 查询编译器                             │
│ Adapter / Execution / Cache                              │
│ 适配器 / 执行 / 缓存                                     │
└─────────────────────────┬────────────────────────────────┘
                          ▼
┌──────────────────────────────────────────────────────────┐
│ Scan Layer(扫描层)                                        │
│ Metadata / Object / Field / Index / Constraint / Relation│
│ 元数据 / 对象 / 字段 / 索引 / 约束 / 关系                │
└─────────────────────────┬────────────────────────────────┘
                          ▼
                 Data Sources(数据源)
SQL / MongoDB / Redis / Cassandra / Neo4j / InfluxDB / ...
```

架构边界：

- Adapter(适配器)负责读取不同数据库的真实结构和执行原生查询。
- Scan(扫描)负责把不同数据库结构标准化。
- Graph Builder(图构建器)负责把扫描结果转换为统一图结构。
- Neo4j(图数据库)负责保存企业数据库结构图。
- Semantic Retriever(语义检索器)读取 Neo4j(图数据库)结构和 Semantic Asset Registry(语义资产注册表)。
- Query Engine(查询引擎)根据真实结构和真实关系生成受约束查询。
- Agent Runtime(智能体运行时)负责复杂任务规划、执行、恢复和追踪。

---

# 2. Enterprise Database Scan(企业数据库扫描)

## 2.1 Scan(扫描)目标

Scan(扫描)负责回答：

> 企业数据库中真实存在什么结构？这些结构之间存在哪些能够确认的关系？

主流程：

```text
Configured Database(已配置数据库)
        ↓
Connection Verification(连接验证)
        ↓
Scan Metadata(扫描元数据)
        ↓
Scan Objects / Fields / Indexes / Constraints / Relationships
扫描对象 / 字段 / 索引 / 约束 / 关系
        ↓
Normalize(标准化)
        ↓
Graph Builder(图构建器)
        ↓
Neo4j Writer(Neo4j 写入器)
        ↓
Neo4j Enterprise Data Graph(Neo4j 企业数据图)
```

Scan(扫描)完成的判断标准不再是“生成了 Markdown(标记文档)”，而是“Neo4j(图数据库)中的当前企业数据图与源数据库真实结构一致”。

## 2.2 Scan Completeness(扫描完整性)

不同数据库能力不同，但只要源数据库能够稳定提供，就应尽量扫描以下结构：

| 类别 | 扫描内容 |
| --- | --- |
| Database(数据库) | 名称、类型、Driver(驱动)、数据库名、命名空间等非敏感信息 |
| Namespace(命名空间) | Schema(模式)、Keyspace(键空间)、数据库原生命名空间 |
| DataObject(数据对象) | Table(表)、View(视图)、Collection(集合)、Measurement(测量)、Node Label(节点标签)、Edge Type(边类型)、Key Group(键分组)、Vector Collection(向量集合)等 |
| Field(字段) | 名称、路径、标准类型、原生类型、可空、主键、唯一、索引状态、默认值、Comment(注释)等 |
| Index(索引) | 名称、字段、顺序、唯一性、索引类型、数据库原生参数 |
| Constraint(约束) | Primary Key(主键)、Unique Constraint(唯一约束)、Foreign Key(外键)、Check Constraint(检查约束)等 |
| Relationship(关系) | 数据库原生明确关系或管理员明确配置且校验通过的关系 |
| Statistics(统计) | 结构扫描所需的受限统计信息 |
| Sample(样例) | 仅在结构识别确有需要时进行有限、只读、脱敏采样，不长期复制完整业务数据 |

安全要求：密码、Token(令牌)、密钥、证书原文和完整敏感连接串不得写入 Neo4j(图数据库)、模型上下文或普通日志。

---

# 3. Neo4j Enterprise Data Graph(Neo4j 企业数据图)

## 3.1 核心节点

第一版保持简单，只定义以下核心节点：

```text
Database(数据库)
Namespace(命名空间)
DataObject(数据对象)
Field(字段)
Index(索引)
Constraint(约束)
```

`DataObject`(数据对象)统一表示不同数据库中的结构对象：

| 数据库家族 | DataObject Type(数据对象类型) |
| --- | --- |
| Relational(关系型) | table(表)、view(视图) |
| Document(文档型) | collection(集合) |
| Key Value(键值型) | key_group(键分组) |
| Wide Column(宽列型) | wide_column_table(宽列表) |
| Graph(图型) | node_label(节点标签)、edge_type(边类型) |
| Search(搜索型) | index(索引对象) |
| Time Series(时序型) | measurement(测量) |
| OLAP(分析型) | table(表)、view(视图)、materialized_view(物化视图) |
| Vector(向量型) | vector_collection(向量集合) |

## 3.2 核心关系

```text
(Database)-[:HAS_NAMESPACE]->(Namespace)
(Database)-[:CONTAINS]->(DataObject)
(Namespace)-[:CONTAINS]->(DataObject)
(DataObject)-[:HAS_FIELD]->(Field)
(DataObject)-[:HAS_INDEX]->(Index)
(DataObject)-[:HAS_CONSTRAINT]->(Constraint)
(DataObject)-[:RELATES_TO]->(DataObject)
```

没有独立 Namespace(命名空间)概念的数据库，可以由 Database(数据库)直接 `CONTAINS`(包含) DataObject(数据对象)。

## 3.3 节点建议属性

### Database(数据库)

```text
id
workspace_id
name
kind
driver
database_name
scan_version
scanned_at
```

### DataObject(数据对象)

```text
id
datasource_id
namespace
name
object_kind
comment
description
estimated_record_count
```

### Field(字段)

```text
id
object_id
name
path
data_type
native_type
nullable
primary_key
unique
indexed
default_value
comment
description
```

### Index(索引)

```text
id
object_id
name
index_type
unique
fields
native_options
```

### Constraint(约束)

```text
id
object_id
name
constraint_type
fields
expression
```

---

# 4. Relationship Correctness(关系正确性)

关系规则固定为：

> **有明确关系就写；没有明确关系就不写。**

允许创建正式 `RELATES_TO`(关联到) 的来源只有：

1. 源数据库自身明确声明的关系，例如 Foreign Key(外键)、图数据库原生 Edge(边)或数据库能够明确读取的引用关系。
2. 管理员或企业配置明确声明的关系，并且引用的数据源、对象和字段都已经存在且通过结构校验。

以下情况不能单独生成正式关系：

- 两个字段名称相同。
- 两个字段数据类型相同。
- 样本值看起来重合。
- 两个对象名称看起来相关。
- LLM(大语言模型)认为两张表“可能有关”。
- Structure Inference(结构推断)认为存在业务联系。

第一版不保存 Candidate Relationship(候选关系)。无法确认就不写。

关系边至少保存：

```text
relationship_type
source
from_field_path
to_field_path
directed
confirmed
```

数据库原生关系：

```text
source = database
confirmed = true
```

管理员明确配置且校验通过的关系：

```text
source = configuration
confirmed = true
```

---

# 5. Rescan(重新扫描)

重新扫描同一 Data Source(数据源)时，当前有效 Neo4j(图数据库)子图必须反映最近一次成功扫描的真实结构：

- 新增对象、字段、索引、约束和真实关系要新增。
- 已删除对象、字段、索引、约束和关系要从当前有效图移除或失效。
- 已修改结构要正确更新。
- 不能因为本次扫描失败破坏上一版成功图。
- 当前有效图不能存在悬空边和重复唯一标识。
- 保留 `scan_version`(扫描版本) 和 `scanned_at`(扫描时间)。

历史版本是否长期保存可以后续决定，但当前图必须与最近一次成功 Scan(扫描)一致。

---

# 6. Semantic Query Architecture(语义查询架构)

## 6.1 主流程

```text
NL Question(自然语言问题)
  ↓
Rule Fact Extraction(规则事实提取)
  ├─ Date / Number / Explicit Operator / Top-N
  │  日期 / 数字 / 显式运算符 / 前 N 名
  ↓
RuleFacts(规则事实)
  ↓
LLM Intent Understanding(大语言模型意图理解)
  ↓
BusinessQuery(业务查询)
  ↓
Semantic Retrieval(语义检索)
  ├─ Neo4j Enterprise Data Graph(Neo4j 企业数据图)
  └─ Semantic Asset Registry(语义资产注册表)
  ↓
Grounding(语义落地)
  ↓
GroundedQuery(已落地查询)
  ↓
QueryContext(查询上下文)
  ↓
QueryPlan(查询计划)
  ↓
Plan Validation(计划校验)
  ↓
QueryGenerator(查询生成器)
  ↓
Native Validation(原生查询校验)
  ↓
Permission Gate(权限门)
  ↓
Execution(执行)
  ↓
Typed Result(类型化结果)
  ↓
Result Validation(结果校验) + QueryTrace(查询轨迹)
```

## 6.2 BusinessQuery(业务查询)

BusinessQuery(业务查询)表达用户真正要问的业务问题，而不是直接写 `orders.pay_amount` 一类物理字段。

至少包含：objective(目标)、entities(业务实体)、metrics(指标)、dimensions(维度)、filters(过滤)、time_expression(时间表达)、comparison(比较)、derivations(衍生计算)、ranking(排名)、requested_output(期望输出)、ambiguities(歧义)、confidence(置信度)。

## 6.2.1 Intent Understanding & LLM Boundary(意图理解与大语言模型边界)

Intent Understanding(意图理解)采用 **Rule + LLM + Grounding(规则 + 大语言模型 + 语义落地)** 的分层设计，不允许单独依赖 Regex(正则表达式)，也不允许让 LLM(大语言模型)直接自由生成最终数据库查询。

正式主链固定为：

```text
Question(用户问题)
   ↓
RuleFactExtractor(规则事实提取器)
   ↓
RuleFacts(规则事实)
   ↓
LLMIntentParser(大语言模型意图解析器)
   ↓
BusinessQuery(业务查询)
   ↓
SemanticRetriever(语义检索器)
   ↓
Grounder(语义落地器)
   ↓
GroundedQuery(已落地查询)
   ↓
QueryContext(查询上下文)
```

模块职责必须保持清晰：

| 模块 | 负责 | 不负责 |
| --- | --- | --- |
| RuleFactExtractor(规则事实提取器) | 日期、数字、显式比较符、Top-N(前 N 名)、明确范围等确定性事实 | 不判断业务实体和指标的真实物理字段 |
| LLMIntentParser(大语言模型意图解析器) | 理解业务目标、实体、指标、维度、业务过滤、自然时间、比较、衍生、排名、期望输出、歧义和置信度 | 不选择真实表、真实字段、真实关系，不生成最终 SQL(结构化查询语言) |
| SemanticRetriever(语义检索器) | 从 Neo4j(图数据库)和 Semantic Asset Registry(语义资产注册表)召回真实结构与受治理语义资产 | 不发明不存在的数据对象或关系 |
| Grounder(语义落地器) | 把 BusinessQuery(业务查询)绑定到真实 DataSource(数据源)、DataObject(数据对象)、Field(字段)、Relationship(关系) | 不允许绑定图中不存在的资产 |
| QueryPlanner(查询规划器) | 基于 GroundedQuery(已落地查询)和 QueryContext(查询上下文)决定查询逻辑 | 不直接执行数据库查询 |
| QueryGenerator(查询生成器) | 把 QueryPlan(查询计划)编译为 SQL(结构化查询语言)或其他原生查询语言 | 不重新解释用户业务语义 |

### 6.2.2 RuleFacts(规则事实)

RuleFactExtractor(规则事实提取器)只提取能够确定识别的事实，典型包括：

```text
date / datetime(日期 / 日期时间)
number(数字)
explicit operator(显式运算符)
range(明确范围)
Top-N(前 N 名)
explicit datasource hint(显式数据源提示，可选)
```

例如：

```text
问题：
“查 2026-08-01 到 2026-08-31，销售额超过 100 万的前 10 个客户”

RuleFacts(规则事实)：
dates = [2026-08-01, 2026-08-31]
numbers = [1000000, 10]
operator = greater_than
limit = 10
```

Regex(正则表达式)可以参与这一层，但其职责仅限于确定性事实提取。Regex(正则表达式)不作为完整自然语言语义理解方案。

### 6.2.3 LLM Intent Parsing(大语言模型意图解析)

LLMIntentParser(大语言模型意图解析器)的输入至少包括：

```text
question(原始用户问题)
rule_facts(规则事实)
BusinessQuery schema(BusinessQuery 数据结构约束)
```

输出必须是结构化的 BusinessQuery(业务查询)，至少符合第 6.2 节定义的字段。

示例：

```text
Question(问题):
“最近一个月销售额最高的前 10 个客户”

BusinessQuery(业务查询):
objective = ranking
entities = [客户]
metrics = [销售额]
dimensions = [客户]
time_expression = 最近一个月
ranking.direction = top
ranking.limit = 10
requested_output = [table]
```

LLM(大语言模型)在这一层 **禁止**：

1. 输出或猜测 datasource_id(数据源编号)。
2. 输出或猜测真实 Table(表)、Collection(集合)、DataObject(数据对象)名称。
3. 输出或猜测真实 Field(字段)名称。
4. 发明 Relationship(关系)或 Join Path(连接路径)。
5. 直接生成最终 SQL(结构化查询语言)、CQL(Cassandra 查询语言)、Cypher(图查询语言)或其他可执行原生命令。
6. 绕过 Grounding(语义落地)、Plan Validation(计划校验)、Native Validation(原生查询校验)或 Permission Gate(权限门)。

模型的职责是“理解业务问题”，不是“决定数据库事实”。

### 6.2.4 Grounding Boundary(语义落地边界)

BusinessQuery(业务查询)中的业务词必须通过 Semantic Retrieval(语义检索)和 Grounding(语义落地)绑定到真实资产。

例如：

```text
“客户”
   ↓
Semantic Retrieval(语义检索)
   ↓
crm_customer(DataObject 数据对象)

“销售额”
   ↓
Semantic Retrieval(语义检索)
   ↓
sales_order.net_amount(Field 字段)

“客户订单关系”
   ↓
Neo4j Enterprise Data Graph(Neo4j 企业数据图)
   ↓
crm_customer.customer_id
→ sales_order.customer_id
```

最终形成 GroundedQuery(已落地查询)，并维护明确 Allowlist(允许列表)：

```text
allowed_datasource_ids
allowed_data_object_ids
allowed_field_paths
allowed_relationship_ids
```

后续 QueryContext(查询上下文)、QueryPlan(查询计划)和最终数据库查询只能使用该 Allowlist(允许列表)中的资产。

### 6.2.5 Failure / Ambiguity / Fallback(失败 / 歧义 / 降级策略)

查询理解阶段必须显式处理以下情况：

**1. LLM(大语言模型)调用成功且 BusinessQuery(业务查询)有效**

继续执行 Semantic Retrieval(语义检索)和 Grounding(语义落地)。

**2. LLM(大语言模型)返回格式无效**

不得尝试从非结构化模型文本中拼装 SQL(结构化查询语言)。应返回 IntentParsingError(意图解析错误)，或在明确支持的有限问题类型上使用 RuleBased Parser(规则解析器)降级。

**3. LLM(大语言模型)不可用**

允许 RuleBased Parser(规则解析器)作为兼容性降级路径，但只支持明确、有限、可测试的问题类型。降级能力不得被描述为完整语义理解能力。

**4. BusinessQuery(业务查询)存在明确歧义**

例如“收入”可能对应含税收入、净收入、回款金额等多个受治理指标。系统应进入 Clarification(澄清)，不能静默选择一个指标。

**5. Grounding(语义落地)无法找到真实对象或字段**

必须失败或进入 Clarification(澄清)，不得由 LLM(大语言模型)补造物理字段。

**6. 存在多个近似候选且无法可靠区分**

记录 unresolved_ambiguities(未解决歧义)，并进入 Clarification(澄清)或返回不可确定结果。

第一版不强制固定统一 confidence threshold(置信度阈值)；阈值需要通过 Evaluation(评估)数据确定，不能凭经验写死为产品正确性依据。

### 6.2.6 Implementation Contract(实现契约)

推荐代码边界：

```text
qaneris/
├─ semantic/
│  ├─ intent.py          # RuleFacts + LLM -> BusinessQuery
│  ├─ retrieval.py       # BusinessQuery -> SemanticCandidate
│  ├─ grounding.py       # Candidate -> GroundedQuery
│  └─ registry.py        # 受治理语义资产
│
├─ llm/
│  ├─ ports.py           # 模型抽象
│  └─ gateway.py         # OpenAI-compatible(兼容 OpenAI 接口)模型调用
│
└─ querying/
   ├─ context.py         # GroundedQuery -> QueryContext，可逐步落位
   ├─ planning/
   ├─ generation/
   ├─ validation/
   └─ execution/
```

现有 `OpenAICompatibleSchemaModel.parse_business_query()` 应作为 LLM Intent Parsing(大语言模型意图解析)能力迁入正式主链。

现有 `RuleBasedQueryIntentParser`(基于规则的查询意图解析器)保留为 Compatibility / Fallback(兼容 / 降级)能力，不再作为未来完整语义查询主路径。

现有允许 LLM(大语言模型)直接返回最终原生查询的路径属于迁移期 Compatibility Path(兼容路径)。新主链完成后，正式自然语言查询应统一经过：

```text
BusinessQuery(业务查询)
→ Semantic Retrieval(语义检索)
→ Grounding(语义落地)
→ QueryContext(查询上下文)
→ QueryPlan(查询计划)
→ QueryGenerator(查询生成器)
→ Validation(校验)
→ Execution(执行)
```

### 6.2.7 Intent Understanding Acceptance Criteria(意图理解验收标准)

至少使用固定测试问题集验证：

- Aggregate(聚合)：数量、总和、平均、最大、最小。
- Time Expression(时间表达)：最近 7 天、最近一个月、明确起止日期。
- Filter(过滤)：文本、数值、显式比较。
- Ranking(排名)：Top-N(前 N 名)、Bottom-N(后 N 名)。
- Dimension(维度)：按客户、地区、商品、月份等分组。
- Ambiguity(歧义)：一个业务词对应多个受治理定义时能够触发 Clarification(澄清)。
- Hallucination Guard(幻觉防护)：模型输出不存在的表、字段、关系时不能进入执行链。
- Fallback(降级)：模型不可用时，有限规则问题能按定义降级；超出能力明确失败。

评估应至少记录 BusinessQuery Accuracy(业务查询准确率)、Ambiguity Detection Accuracy(歧义识别准确率)、Grounding Accuracy(语义落地准确率)以及 End-to-End Query Success Rate(端到端查询成功率)。

---

## 6.3 Semantic Retrieval(语义检索)

SemanticRetriever(语义检索器)的物理结构来源改为 Neo4j Enterprise Data Graph(Neo4j 企业数据图)。

- Neo4j(图数据库)：Database(数据库)、DataObject(数据对象)、Field(字段)、Index(索引)、Constraint(约束)、Relationship(关系)。
- Semantic Asset Registry(语义资产注册表)：Metric(指标)、Dimension(维度)、Business Term(业务术语)、Alias(别名)。

第一阶段继续采用 Metadata + Alias + Lexical Retrieval(元数据 + 别名 + 文本检索)。第二阶段再增加 Embedding Retrieval(向量检索)、Reranker(重排器)、Historical Successful Query(历史成功查询)和企业知识库。

## 6.4 Grounding(语义落地)

Grounding(语义落地)只能绑定 Neo4j(图数据库)中真实存在的数据源、对象、字段和关系，以及 Semantic Asset Registry(语义资产注册表)中受治理的资产。

模型不能发明物理字段，也不能发明关系。

## 6.5 Trusted Semantic Path / Exploratory Path(可信语义路径 / 探索路径)

- Trusted Semantic Path(可信语义路径)：已确认或已发布的指标、维度、业务术语 + Neo4j(图数据库)真实结构。
- Exploratory Path(探索路径)：Neo4j(图数据库)真实结构 + 受限语义推理。

两条路径最终都必须落到真实图节点和真实关系上。

---

# 7. Agent Runtime Architecture(智能体运行时架构)

统一 Runtime(运行时)核心循环：

```text
GoalSpec(目标规格)
   ↓
Planner(规划器)
   ↓
Structured Task DAG(结构化任务图)
   ↓
Scheduler / Orchestrator(调度器 / 编排器)
   ↓
Execute Node(执行节点)
   ↓
Observation(观察结果)
   ↓
Validator(校验器)
   ↓
Completion Criteria(完成标准)
   ├─ 未满足：Continue / Replan / Ask User(继续 / 重规划 / 询问用户)
   └─ 已满足：Finalize(收尾) → Answer / Artifact(答案 / 产物) → Trace(轨迹)
```

第一版 Task(任务)类型限制为 query(查询)、analysis(分析)、retrieval(检索)、visualization(可视化)、report(报告)、validate(验证)、clarify(澄清)。

Query Agent(查询智能体)保持“薄”，只负责把查询任务提交给 Semantic Query Engine(语义查询引擎)，不直接自由生成最终数据库查询。

---

# 8. Security / Permission / Governance(安全 / 权限 / 治理)

- 数据库连接默认使用只读账号。
- 权限检查在 Engine / Runtime(引擎 / 运行时)执行，不依赖 Prompt(提示词)。
- 密码、Token(令牌)、密钥、证书原文和完整敏感连接串不得写入 Neo4j(图数据库)。
- 样本读取必须有限、只读、可脱敏。
- 模型建议不能直接成为真实 Relationship(关系)。
- 模型建议不能直接成为正式 Metric Definition(指标定义)或 Permission Policy(权限策略)。

---

# 9. Evaluation & Validation(评估与校验)

## 9.1 Scan Evaluation(扫描评估)

| 指标 | 要求 |
| --- | --- |
| Object Recall(对象召回率) | 源数据库允许读取的目标对象不能系统性漏扫 |
| Field Recall(字段召回率) | 字段结构应与源数据库一致 |
| Index Recall(索引召回率) | 数据库可读取索引应完整进入图 |
| Constraint Recall(约束召回率) | 主键、唯一、外键和可读取约束应正确 |
| Relationship Precision(关系精确率) | Neo4j(图数据库)中的正式关系必须有真实证据 |
| Relationship Recall(关系召回率) | 源数据库明确声明的关系不能漏扫 |
| Graph Integrity(图完整性) | 无悬空边、重复唯一标识和无效引用 |
| Rescan Consistency(重扫一致性) | 源结构变化后当前图正确同步 |

真实数据库测试必须使用已知结构或数据库系统目录进行逐项比对，不能只看 Neo4j(图数据库)画出来“像不像”。

## 9.2 Query Evaluation(查询评估)

继续评测 BusinessQuery Accuracy(业务查询准确率)、Retrieval Recall(检索召回率)、Grounding Accuracy(语义落地准确率)、QueryPlan Match(查询计划匹配)、Native Query Validity(原生查询合法率)、Execution Success Rate(执行成功率)和 Expected Result Match(预期结果匹配)。

---

# 10. Phase 1(阶段一)开发顺序

当前开发顺序调整为：

```text
P1-SCAN
Database Scan(数据库扫描)
        ↓
Neo4j Enterprise Data Graph(Neo4j 企业数据图)
        ↓
Semantic Retrieval(语义检索)
        ↓
Grounding & Planning(语义落地与规划)
        ↓
Query Execution(查询执行)
        ↓
Query Agent(查询智能体)
        ↓
Agent Runtime Enhancements(智能体运行时增强)
```

## 10.1 P1-SCAN(扫描工作包) —— 当前最高优先级

### Scope(范围)

1. 定义统一 Scan Graph Contract(扫描图契约)。
2. 实现 Graph Builder(图构建器)。
3. 实现 Neo4j Graph Store(Neo4j 图存储)。
4. 建立 Neo4j(图数据库)节点唯一约束和必要索引。
5. 逐类补齐 Adapter Scan(适配器扫描)能力。
6. 补齐对象、字段、索引、约束和真实关系扫描。
7. 实现 Rescan(重扫)和当前有效图更新。
8. 实现 Scan Validation / Graph Validation(扫描校验 / 图校验)。
9. 使用第一批真实数据库进行 Acceptance Test(验收测试)。

### 建议 Contract(契约)

```text
ScanGraph
ScanGraphNode
ScanGraphEdge
DatabaseNode
NamespaceNode
DataObjectNode
FieldNode
IndexNode
ConstraintNode
```

受控 Edge Type(图边类型)：

```text
HAS_NAMESPACE
CONTAINS
HAS_FIELD
HAS_INDEX
HAS_CONSTRAINT
RELATES_TO
```

### Acceptance Criteria(验收标准)

- 测试数据库对象数量与源数据库检查结果一致。
- 字段名称、类型、主键、可空信息与源数据库一致。
- 可读取索引和约束与源数据库一致。
- 源数据库明确存在的关系全部进入 Neo4j(图数据库)。
- 源数据库没有明确关系的对象之间不产生 `RELATES_TO`(关联到)。
- 重扫后新增、删除、修改的结构能够正确同步。
- Neo4j(图数据库)中不存在悬空关系和重复唯一节点。
- Scan(扫描)失败不能破坏上一版成功的当前有效图。

### Non-goals(非目标)

- 不推测跨数据库关系。
- 不通过 LLM(大语言模型)补关系。
- 不把字段同名作为关系证据。
- 不把完整业务数据复制进 Neo4j(图数据库)。
- 不要求 P1-SCAN(扫描工作包)同时完成 Query Agent(查询智能体)或 Agent Runtime(智能体运行时)。

---

# 11. 现有代码迁移原则

当前已经完成的代码尽量复用，不做无必要重写：

1. 保留 Adapter(适配器)连接和查询执行能力，重点扩展 Scan(扫描)接口。
2. 现有 `DatasetInfo`、`FieldInfo`、`RelationInfo` 可以逐步映射为新的 Scan Graph Contract(扫描图契约)。
3. 现有 `ScanSnapshot`(扫描快照)、`DataSourceProfile`(数据源画像)、`CompanyDataProfile`(企业数据画像)暂时保留用于兼容和迁移，但不新增依赖它们的新核心能力。
4. SemanticRetriever(语义检索器)的物理结构来源逐步从 CompanyDataProfile(企业数据画像)切换到 Neo4j Enterprise Data Graph(Neo4j 企业数据图)。
5. `database_profile.md` 从 Scan(扫描)主输出改为 Neo4j(图数据库)的可选派生报告。
6. P1-SCAN(扫描工作包)验收通过后，再继续 P1-04 Grounding & Planning(语义落地与规划)主链开发。

---

# 12. 推荐代码模块边界

```text
qaneris/
├─ scan/
│  ├─ contracts.py       # ScanGraph / Node / Edge
│  ├─ builder.py         # 扫描结果 -> 图
│  ├─ validation.py      # 扫描与图校验
│  └─ service.py         # 扫描编排
│
├─ graph/
│  ├─ ports.py           # GraphStore(图存储接口)
│  ├─ neo4j.py           # Neo4jGraphStore(Neo4j 图存储)
│  └─ schema.py          # Neo4j 约束与索引
│
├─ adapters/
│  └─ ...                # 各数据库扫描与查询执行
│
├─ semantic/
│  ├─ intent.py
│  ├─ retrieval.py       # Neo4j + 语义资产检索
│  ├─ grounding.py
│  └─ registry.py
│
├─ querying/
│  ├─ planning/
│  ├─ generation/
│  ├─ validation/
│  └─ execution/
│
├─ agent/
├─ memory/
├─ skills/
└─ evaluation/
```

边界必须保持：Adapter(适配器)负责读源数据库；Scan(扫描)负责标准化；Graph Builder(图构建器)负责构图；Neo4j Graph Store(Neo4j 图存储)负责持久化；Semantic Retrieval(语义检索)消费图，不重新扫描数据库。

---

# 13. Roadshow Release(路演版本)验收标准

Roadshow Release 是当前短期产品交付目标，但不能改变 Phase 1 的可信查询边界。

- 16 个服务型测试数据库从 `NLQuery-Test-Dataset` 部署到路演服务器，并由 Qaneris 主仓库完成真实 connection + scan + Neo4j 回读验收。
- 其中 14 个完成真实 SSL/TLS 连接；另外 2 个仅在真实 driver/protocol 验证后记录豁免原因，不提前指定。
- 用户密码和证书具备 Upload → Validation → Managed CredentialStore → SecretReference → Runtime Materialization → Rotation/Delete 生命周期。
- Excel `.xlsx` 完成 Upload → deterministic materialization → immutable SQLite → existing Scan → Neo4j → Grounding → Query → Result。
- Web、CLI、Skill、MCP 共享同一个 Application Contract；不得复制核心查询逻辑。
- Roadshow Web 展示 Clarification、Typed Result、结构化 Execution Trace、QueryPlan、SQL/Native Query Evidence 和 Visualization。
- Web / CLI / MCP / Skill 共享一个结构化 Streaming / Progress Event Contract；不暴露模型私有 Chain-of-Thought。
- 固定 Demo Dataset / Questions / Acceptance Script，并录制可重复演示视频。
- 查询仍保持只读；任何产品入口不得绕过 Grounding、Plan/Native Validation、Revision Fence 和权限/范围门禁。

Roadshow 进度、当前缺口与完成状态只在 [Development Roadmap](development-roadmap.md) 维护。

---
# 14. Current Implementation Status(当前实施状态，2026-09-20)

## 14.1 Verified Complete(已核实完成)

- Phase 1 Trusted Query Engine：`QanerisService.ask()` 主链已完成。
- Scan → Neo4j Enterprise Data Graph、多数据源 ScanRun、ScanSnapshot 与图回读校验已实现。
- API `POST /api/ask` 与 MCP `ask_data` 已存在。
- CLI 已实现 `doctor`、Phase 1 acceptance、`source test/scan`、`source import-excel` 与非交互式 `qaneris ask`（human/JSON）。
- Vite + React Web Foundation 已存在。
- 当前 Adapter Registry 注册 21 个 driver。

## 14.2 Roadshow Current Priority(当前路演优先级)

```text
16 service DB server deployment + real Qaneris scan acceptance
+ 14 SSL/TLS
+ managed credential/certificate lifecycle
+ Excel ingestion
+ product CLI ask
+ Skill / MCP productization
+ Web Ask Workspace
+ unified Streaming / Execution Trace / SQL display
+ Result Visualization
+ Demo video
```

## 14.3 Explicit Gaps(明确未完成项)

- `NLQuery-Test-Dataset` 的测试库已准备，但尚不能据此声称路演服务器部署完成。
- `TLSConfig` 和 `SecretReference` 已实现，但 Managed CredentialStore、用户证书上传、轮换/删除和完整证书验证尚未实现。
- CLI `qaneris ask` 已实现非交互式 human/JSON 输出（RS-CLI-01A）；交互式 REPL 不在路演范围。统一 Streaming Event Contract 尚未建立。
- React Web 已有 Foundation 与 EXCEL-01C 的 Excel Upload / Datasource Scope / 最小 Ask 切片（Clarification、Result Table、Plan 与 `evidence.display_command` 展示）；完整 Ask Workspace（Execution Trace 时间线、Visualization）尚未完成。
- Excel ingestion 已实现并完成真实 Neo4j / 真实模型 Roadshow E2E（EXCEL-01A/01B/01C，RS-EXCEL-01 DONE）。
- 当前 API / CLI / MCP / Skill 没有统一 Streaming Event Contract。
- 当前没有正式 Result Visualization 实现。
- 后端已有 `plan` 与 `ExecutionEvidence.display_command`，但 Web Trace / SQL 展示还没有完成。

## 14.4 Retrieval Intelligence(检索智能增强)

Vector Retrieval、Hybrid Retrieval、Reranker、Historical Query Retrieval 和 Enterprise RAG 仍属于后续增强方向，但不作为四天 Roadshow Release 的阻塞项。它们未来仍只能提供候选/排序，不得成为物理事实来源。

## 14.5 Evidence Wording(证据口径)

历史固定真实 Phase 1 E2E 记录为六类问题 × 3 次 = 18/18 符合预期。模型网关后来迁移到 aiyallm，仓库当前没有一份迁移后成功的真实模型回归记录，因此不得把历史验收描述成“当前所有模型/数据库环境总体 100% 通过”。

---
# 15. 最终结论

Qaneris 已完成第一阶段“可信地问数据库”的核心闭环：

```text
Natural Language
→ Trusted Business Intent
→ Governed Semantic Binding
→ Graph-confirmed Physical Facts
→ Validated Read-only Query
→ Typed Result + Evidence
```

当前短期主线切换为 Roadshow Release 产品化收口：把已经完成的可信查询核心交付到真实多数据库部署、SSL/TLS、Excel、Web / CLI / Skill / MCP、Streaming、Trace / SQL 和 Visualization。Phase 2 Retrieval Intelligence 保留为后续增强方向，但不作为四天路演版本的阻塞项。

Phase 2 不改变 Phase 1 的信任边界：模型、Embedding、Vector 和 Reranker负责理解、建议、召回与排序；Semantic Assets + Neo4j + Grounding 继续决定最终可执行事实。
