from __future__ import annotations

import json
from typing import Any

from smartdata.contracts.profile import (
    DataObjectProfile,
    DataSourceProfile,
    ScanSnapshot,
)

FORMAT_VERSION = "1.0"

_CATEGORY_DETAILS = {
    "relational": ("关系型数据库", "SQL", "Schema、表、视图、字段、约束和关系"),
    "key_value": ("键值数据库", "原生命令", "键分组、键模式、值类型和过期信息"),
    "document": ("文档数据库", "文档查询", "集合、嵌套字段、数组和文档索引"),
    "wide_column": ("宽列数据库", "CQL 或原生命令", "键空间、分区键和聚簇键"),
    "graph": ("图数据库", "图查询", "节点标签、边类型、属性和方向"),
    "search": ("搜索数据库", "搜索查询", "索引、字段映射、分析器和别名"),
    "time_series": ("时序数据库", "时序查询", "测量、时间字段、标签和保留策略"),
    "olap": ("分析型数据库", "SQL", "分区键、排序键、分布键和物化视图"),
    "vector": ("向量数据库", "向量查询", "集合、向量维度、距离算法和过滤字段"),
}

class MarkdownProfileRenderer:
    def render(self, snapshot: ScanSnapshot, workspace_id: str) -> dict[str, str]:
        if snapshot.profile is None:
            raise ValueError("profile document requires a scan profile")
        return {"database_profile.md": self._render_index(snapshot, workspace_id)}

    def _render_index(self, snapshot: ScanSnapshot, workspace_id: str) -> str:
        profile = self._profile(snapshot)
        category_name, query_language, structure = self._category(profile)
        objects = self._objects(profile)
        domains = self._business_domains(objects)
        lines = [
            self._front_matter(snapshot, workspace_id, "datasource", profile.datasource_id),
            f"# 数据库总览：{profile.name}",
            "",
            "## 这是什么数据库",
            "",
            f"> {self._database_summary(profile)}",
            "",
            "> 上述用途由 Scan 根据对象名称、字段和关系自动归纳，属于结构推断；数据库负责人确认后，才可作为正式业务定义。",
            "",
            "## 核心业务领域",
            "",
            *(f"- {domain}" for domain in domains),
            *([] if domains else ["- 暂未从物理结构识别出明确业务领域"]),
            "",
            "## 数据库基本信息",
            "",
            f"- 数据源标识：`{profile.datasource_id}`",
            f"- 数据库分类：{category_name}（`{profile.kind.value}`）",
            f"- 数据库驱动：`{profile.driver}`",
            f"- 查询方式：{query_language}",
            f"- 结构重点：{structure}",
            f"- 命名空间数量：{len(profile.namespaces)}",
            f"- 数据对象数量：{len(objects)}",
            f"- 已识别关系数量：{len(snapshot.relationships)}",
            f"- 扫描分析上限：每个对象 {snapshot.policy.profile_size} 条记录",
            f"- 字段样例上限：每个字段最多 {snapshot.policy.field_sample_size} 个去重脱敏值",
            "",
            "## 数据对象及用途",
            "",
            "| 命名空间 | 对象名称 | 推断用途 | 对象类型 | 字段数 | 关键字段 | 记录数估算 |",
            "|---|---|---|---|---:|---|---:|",
        ]
        for namespace in profile.namespaces:
            for data_object in namespace.data_objects:
                count = (
                    str(data_object.estimated_record_count)
                    if data_object.estimated_record_count is not None
                    else "未知"
                )
                lines.append(
                    "| "
                    f"{self._table(namespace.name)} | {self._table(data_object.name)} | "
                    f"{self._table(self._object_purpose(data_object))} | "
                    f"{self._table(data_object.object_kind.value)} | "
                    f"{len(data_object.fields)} | "
                    f"{self._table(self._key_fields(data_object))} | {count} |"
                )
        lines.extend(
            [
                "",
                "## 主要数据关系",
                "",
                "| 来源对象 | 来源字段 | 目标对象 | 目标字段 | 类型 | 置信度 |",
                "|---|---|---|---|---|---:|",
            ]
        )
        names = {item.id: self._qualified_name(item) for item in objects}
        for relation in snapshot.relationships:
            lines.append(
                "| "
                f"{self._table(names.get(relation.from_object_id, relation.from_object_id))} | "
                f"{self._table(relation.from_field_path or '')} | "
                f"{self._table(names.get(relation.to_object_id, relation.to_object_id))} | "
                f"{self._table(relation.to_field_path or '')} | "
                f"{self._table(relation.relationship_type)} | {relation.confidence:.2f} |"
            )
        if not snapshot.relationships:
            lines.append("| - | - | - | - | 未识别到关系 | - |")
        lines.extend(["", "## 完整字段目录", ""])
        for data_object in objects:
            lines.extend(
                [
                    f"### {self._qualified_name(data_object)}",
                    "",
                    f"- 推断用途：{self._object_purpose(data_object)}",
                    f"- 对象标识：`{data_object.id}`",
                    f"- 字段：{self._inline_list([field.path for field in data_object.fields])}",
                    "",
                ]
            )
        lines.extend(
            [
                "",
                "## 扫描警告",
                "",
            ]
        )
        lines.extend(f"- {warning}" for warning in snapshot.warnings)
        if not snapshot.warnings:
            lines.append("- 无")
        return "\n".join(lines) + "\n"

    @classmethod
    def _database_summary(cls, profile: DataSourceProfile) -> str:
        domains = cls._business_domains(cls._objects(profile))
        if domains:
            return f"该数据库主要承载{'、'.join(domains)}相关数据，用于保存和关联这些业务对象。"
        return "当前 Scan 仅确认了数据库物理结构，尚不足以可靠判断其具体业务用途。"

    @classmethod
    def _business_domains(cls, objects: list[DataObjectProfile]) -> list[str]:
        domains: list[str] = []
        for data_object in objects:
            domain, _ = cls._purpose_for_name(data_object.name)
            if domain and domain not in domains:
                domains.append(domain)
        return domains

    @classmethod
    def _object_purpose(cls, data_object: DataObjectProfile) -> str:
        description = data_object.comment or cls._semantic_description(data_object)
        if description:
            return description
        _, purpose = cls._purpose_for_name(data_object.name)
        return purpose or f"保存 {data_object.name} 对应的数据记录"

    @staticmethod
    def _purpose_for_name(name: str) -> tuple[str | None, str | None]:
        normalized = name.casefold().replace("-", "_")
        rules = (
            (("order_item",), "订单交易", "保存订单商品明细"),
            (("order",), "订单交易", "保存订单及交易过程信息"),
            (("payment", "pay_"), "支付结算", "保存支付与收款信息"),
            (("refund",), "支付结算", "保存退款与售后资金信息"),
            (("customer", "user", "member"), "客户管理", "保存客户或用户资料"),
            (("product", "sku", "brand", "categor"), "商品管理", "保存商品、品牌或分类信息"),
            (("inventory", "warehouse", "stock"), "库存仓储", "保存库存与仓储信息"),
            (("shipment", "delivery", "logistic"), "履约物流", "保存发货与物流履约信息"),
            (("supplier",), "供应链", "保存供应商信息"),
            (("coupon", "promotion", "campaign"), "营销活动", "保存优惠或营销活动信息"),
            (("review", "comment", "rating"), "客户反馈", "保存评价与反馈信息"),
            (("behavior", "event", "visit"), "用户行为", "保存用户行为事件"),
            (("address", "region"), "地址区域", "保存地址与区域信息"),
        )
        for terms, domain, purpose in rules:
            if any(term in normalized for term in terms):
                return domain, purpose
        return None, None

    @staticmethod
    def _key_fields(data_object: DataObjectProfile) -> str:
        preferred = [
            field.path
            for field in data_object.fields
            if field.primary_key or field.unique or field.indexed
        ]
        fields = preferred or [field.path for field in data_object.fields]
        suffix = "…" if len(fields) > 8 else ""
        return "、".join(fields[:8]) + suffix if fields else "无"

    def _render_relationships(self, snapshot: ScanSnapshot, workspace_id: str) -> str:
        profile = self._profile(snapshot)
        names = {
            item.id: self._qualified_name(item)
            for item in self._objects(profile)
        }
        lines = [
            self._front_matter(snapshot, workspace_id, "relationships", profile.datasource_id),
            f"# 数据关系：{profile.name}",
            "",
            "| 关系标识 | 来源对象 | 来源字段 | 目标对象 | 目标字段 | 类型 | 来源 | 置信度 | 已确认 |",
            "|---|---|---|---|---|---|---|---:|---|",
        ]
        for relation in snapshot.relationships:
            lines.append(
                "| "
                f"{self._table(relation.id)} | "
                f"{self._table(names.get(relation.from_object_id, relation.from_object_id))} | "
                f"{self._table(relation.from_field_path or '')} | "
                f"{self._table(names.get(relation.to_object_id, relation.to_object_id))} | "
                f"{self._table(relation.to_field_path or '')} | "
                f"{self._table(relation.relationship_type)} | {relation.origin.value} | "
                f"{relation.confidence:.2f} | {'是' if relation.confirmed else '否'} |"
            )
        if not snapshot.relationships:
            lines.append("| - | - | - | - | - | 未识别到关系 | - | - | - |")
        return "\n".join(lines) + "\n"

    def _render_object(
        self,
        snapshot: ScanSnapshot,
        workspace_id: str,
        data_object: DataObjectProfile,
    ) -> str:
        profile = self._profile(snapshot)
        related = [
            relation
            for relation in snapshot.relationships
            if data_object.id in (relation.from_object_id, relation.to_object_id)
        ]
        description = data_object.comment or self._semantic_description(data_object) or "暂无描述"
        estimated_count = (
            data_object.estimated_record_count
            if data_object.estimated_record_count is not None
            else "未知"
        )
        lines = [
            self._front_matter(snapshot, workspace_id, "data_object", data_object.id),
            f"# 数据对象：{self._qualified_name(data_object)}",
            "",
            "## 对象身份",
            "",
            f"- 对象标识：`{data_object.id}`",
            f"- 数据源标识：`{data_object.datasource_id}`",
            f"- 命名空间：`{data_object.namespace or 'default'}`",
            f"- 对象类型：`{data_object.object_kind.value}`",
            f"- 数据库分类：`{profile.kind.value}`",
            f"- 描述：{description}",
            f"- 扫描分析记录数：{data_object.profiled_record_count}",
            "",
            "## 字段结构",
            "",
            "| 字段路径 | 标准类型 | 原生类型 | 可空 | 主键 | 唯一 | 已索引 | 数组 | 观察数 | 字段样例 |",
            "|---|---|---|---|---|---|---|---|---:|---|",
        ]
        for field in data_object.fields:
            samples = json.dumps(field.sample_values, ensure_ascii=False, sort_keys=True)
            lines.append(
                "| "
                f"{self._table(field.path)} | {self._table(field.data_type)} | "
                f"{self._table(field.native_type or '')} | {'是' if field.nullable else '否'} | "
                f"{'是' if field.primary_key else '否'} | {'是' if field.unique else '否'} | "
                f"{'是' if field.indexed else '否'} | {'是' if field.array else '否'} | "
                f"{field.observed_count} | {self._table(samples)} |"
            )
        if not data_object.fields:
            lines.append("| - | - | - | - | - | - | - | - | 0 | [] |")
        lines.extend(
            [
                "",
                "## 物理索引与约束",
                "",
            ]
        )
        primary_keys = [field.path for field in data_object.fields if field.primary_key]
        indexed = [field.path for field in data_object.fields if field.indexed]
        unique = [field.path for field in data_object.fields if field.unique]
        lines.extend(
            [
                f"- 主键字段：{self._inline_list(primary_keys)}",
                f"- 唯一字段：{self._inline_list(unique)}",
                f"- 已索引字段：{self._inline_list(indexed)}",
                "- 命名索引及数据库专属索引参数：当前扫描器未提供时显示为空，不进行推断。",
                "",
                "## 对象关系",
                "",
            ]
        )
        for relation in related:
            lines.append(
                f"- `{relation.id}`：`{relation.from_object_id}.{relation.from_field_path or '*'}` "
                f"→ `{relation.to_object_id}.{relation.to_field_path or '*'}`；"
                f"类型 `{relation.relationship_type}`；来源 `{relation.origin.value}`；"
                f"置信度 {relation.confidence:.2f}。"
            )
        if not related:
            lines.append("- 未识别到关系。")
        lines.extend(
            [
                "",
                "## 数据统计",
                "",
                f"- 记录数估算：{estimated_count}",
                f"- 实际分析记录数：{data_object.profiled_record_count}",
                "- 完整业务记录：不写入画像文档",
                "",
                "## 查询限制",
                "",
                "- 仅使用只读查询。",
                "- 字段、关系和类型必须经过查询校验器确认。",
                "- 模型推断信息不得覆盖数据库扫描事实。",
            ]
        )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _profile(snapshot: ScanSnapshot) -> DataSourceProfile:
        if snapshot.profile is None:
            raise ValueError("scan snapshot has no profile")
        return snapshot.profile

    @staticmethod
    def _objects(profile: DataSourceProfile) -> list[DataObjectProfile]:
        return [item for namespace in profile.namespaces for item in namespace.data_objects]

    @staticmethod
    def _qualified_name(data_object: DataObjectProfile) -> str:
        return (
            f"{data_object.namespace}.{data_object.name}"
            if data_object.namespace
            else data_object.name
        )

    @staticmethod
    def _semantic_description(data_object: DataObjectProfile) -> str | None:
        return next(
            (item.description for item in data_object.semantic_metadata if item.description),
            None,
        )

    @staticmethod
    def _category(profile: DataSourceProfile) -> tuple[str, str, str]:
        return _CATEGORY_DETAILS.get(
            profile.kind.value,
            (profile.kind.value, "数据库原生查询", "数据库原生数据对象"),
        )

    @staticmethod
    def _front_matter(
        snapshot: ScanSnapshot,
        workspace_id: str,
        document_kind: str,
        subject_id: str,
    ) -> str:
        profile = MarkdownProfileRenderer._profile(snapshot)
        values: dict[str, Any] = {
            "format_version": FORMAT_VERSION,
            "document_kind": document_kind,
            "workspace_id": workspace_id,
            "datasource_id": snapshot.datasource_id,
            "datasource_name": profile.name,
            "snapshot_id": snapshot.id,
            "snapshot_version": snapshot.version,
            "subject_id": subject_id,
            "database_kind": profile.kind.value,
            "database_category": profile.kind.value,
            "driver": profile.driver,
            "scan_status": snapshot.status.value,
            "scanned_at": snapshot.completed_at.isoformat() if snapshot.completed_at else None,
        }
        lines = ["---"]
        lines.extend(
            f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in values.items()
        )
        lines.append("---")
        return "\n".join(lines)

    @staticmethod
    def _table(value: str) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    @staticmethod
    def _inline_list(values: list[str]) -> str:
        return "、".join(f"`{value}`" for value in values) if values else "无"
