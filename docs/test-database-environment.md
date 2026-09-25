# 服务器测试数据库环境

> 本文说明 Qaneris(问数项目) 当前可使用的服务器数据库测试环境。本文不定义 Adapter(适配器) 配置；数据库部署与连接参数的权威来源是独立的 `NLQuery-Test-Dataset` 仓库。

## 1. 权威数据源

测试数据库及 fixture(测试数据文件)由以下仓库维护：

- `JingJIang96200/NLQuery-Test-Dataset`
- 部署说明：`docs/database-test-environment.md`
- 连接速查：`docs/database-connection-reference.md`

Qaneris(问数项目)不复制数据库部署逻辑，也不成为数据库端口、数据生成或 loader(装载器)的 Source of Truth(唯一事实来源)。

## 2. 测试环境目的

这套服务器环境用于让 Qaneris(问数项目)在真实数据库服务上进行：

- 数据源连通性测试；
- readonly(只读)执行测试；
- SQL(结构化查询语言)、CQL(Cassandra查询语言)、Cypher(图查询语言)、搜索查询、时序查询与向量检索测试；
- 多数据库一致业务世界下的查询结果比对；
- Query Contract(查询契约)与回归测试；
- 真实 TLS(传输层安全协议)/mTLS(双向传输层安全协议)连接验证。

当前数据库部署完成度：

```text
17 / 17
```

服务器公网地址：

```text
114.66.43.195
```

## 3. 已提供的数据库

关系与事务：

- PostgreSQL(关系型数据库)
- MySQL(关系型数据库)
- SQL Server(关系型数据库)
- SQLite(嵌入式数据库)

文档、键值和宽列：

- MongoDB(文档数据库)
- CouchDB(文档数据库)
- Redis(键值数据库)
- Cassandra(分布式宽列数据库)

分析与时序：

- ClickHouse(列式分析数据库)
- TimescaleDB(时序关系型数据库)
- InfluxDB(时序数据库)

搜索：

- Elasticsearch(分布式搜索引擎)
- OpenSearch(开源搜索引擎)

图与向量：

- Neo4j(图数据库)
- Milvus(向量数据库)
- Qdrant(向量数据库)
- Weaviate(向量数据库)

完整端口、协议、连接示例和数据基线不要在本仓库重复维护，统一查看 `NLQuery-Test-Dataset/docs/database-connection-reference.md`。

## 4. 数据语义

这 17 种数据库来自同一个零售/电商业务世界，但根据数据库用途采用不同物理模型。

示例：

- PostgreSQL(关系型数据库)、MySQL(关系型数据库)、SQL Server(关系型数据库)：交易关系数据；
- MongoDB(文档数据库)、CouchDB(文档数据库)：文档数据；
- Redis(键值数据库)：缓存/键值数据；
- Cassandra(分布式宽列数据库)：行为事件；
- ClickHouse(列式分析数据库)：分析事实数据；
- TimescaleDB(时序关系型数据库)：行为时序数据；
- InfluxDB(时序数据库)：销售、库存、访问指标；
- Elasticsearch(分布式搜索引擎)、OpenSearch(开源搜索引擎)：搜索索引；
- Neo4j(图数据库)：客户、订单、商品、品类、品牌及关系；
- Milvus(向量数据库)、Qdrant(向量数据库)、Weaviate(向量数据库)：共享冻结 embedding(向量嵌入)。

因此，不应期待 17 个数据库拥有完全相同的表、collection(集合)或字段。

## 5. 固定数据基线

测试数据 seed(随机种子)：

```text
20260701
```

常用 sanity check(合理性检查)：

```text
orders                           = 1000
behavior_events                  = 300
ClickHouse order_items           = 2502
InfluxDB metrics                 = 28 / 1 / 1
Search products/orders/reviews   = 27 / 1000 / 501
Vector products/customers        = 27 / 100
```

不同数据库的完整基线以数据集仓库正式文档和 loader(装载器)验收结果为准。

## 6. 凭据与证书

Qaneris(问数项目)仓库不得保存：

- 数据库真实密码；
- API Key(接口密钥)；
- Token(令牌)；
- CA private key(CA私钥)；
- client private key(客户端私钥)。

PostgreSQL(关系型数据库)、MongoDB(文档数据库)、Redis(键值数据库)的服务器测试环境已实际使用 mTLS(双向传输层安全协议)。客户端证书应从独立安全渠道提供，而不是提交到本仓库。

测试代码、CI(持续集成)、Issue(议题)、日志或 Pull Request(拉取请求)不得复制生产或测试环境秘密值。

## 7. 推荐测试流程

当 Qaneris(问数项目)需要使用服务器测试数据库时，建议按以下顺序：

1. 根据数据集仓库 Connection Reference(连接速查表)确认目标数据库的公网端口与协议。
2. 从安全渠道取得目标数据库所需的凭据或证书。
3. 先完成独立客户端 Smoke Test(冒烟测试)，确认从当前开发机能够访问目标数据库。
4. 再运行 Qaneris(问数项目)对应的真实环境测试。
5. 对查询结果使用数据集仓库的固定基线或 Query Contract(查询契约)进行校验。
6. 测试完成后不要把凭据、证书私钥或临时 Token(令牌)写入提交记录。

## 8. 环境问题归属

出现问题时先区分责任边界：

- 容器没有启动、端口不通、fixture(测试数据文件)缺失、数据计数不对、readonly(只读)权限异常：优先在 `NLQuery-Test-Dataset` 排查。
- Qaneris(问数项目)自身的查询规划、校验、执行链路、结果结构或产品行为异常：在本仓库排查。
- 证书、密码、API Key(接口密钥)、Token(令牌)缺失：走独立安全凭据流程，不在代码仓库临时补明文。

## 9. 变更同步

如果测试数据库的公网 IP(互联网协议地址)、端口、协议、安全方式或数据基线发生变化：

1. 先更新 `NLQuery-Test-Dataset` 的权威文档；
2. 本文只在“测试环境用途、责任边界或引用位置”变化时更新；
3. 不在两个仓库维护两份独立的完整连接矩阵。
