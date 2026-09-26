/**
 * View model for the Join Mapping screen.
 *
 * Kept free of React and of ``fetch`` so the rules that decide what a user sees - which pairs are
 * offerable, which mappings need attention, what a failure means - can be tested directly. The
 * component only renders what this returns.
 */

export const STATUS_LABEL = {
  CANDIDATE: "待确认",
  CONFIRMED: "已确认",
  STALE: "需重新确认",
  REJECTED: "已拒绝",
};

/** Only a CONFIRMED mapping can be executed by a federated plan; the rest are shown for audit. */
export const EXECUTABLE_STATUSES = new Set(["CONFIRMED"]);

const pairKey = (left, right) => [left, right].sort().join("\u0000");

/**
 * Datasource pairs that can be offered.
 *
 * The backend refuses a mapping whose two sides are the same datasource, and a federated plan needs
 * at least two sources, so a pair of one is never offered instead of being refused after the fact.
 */
export function datasourcePairs(objects) {
  const ids = [...new Set(objects.map((item) => item.datasource_id))].sort();
  const names = new Map(objects.map((item) => [item.datasource_id, item.datasource_name]));
  const pairs = [];
  for (let i = 0; i < ids.length; i += 1) {
    for (let j = i + 1; j < ids.length; j += 1) {
      pairs.push({
        key: pairKey(ids[i], ids[j]),
        left: { id: ids[i], name: names.get(ids[i]) || ids[i] },
        right: { id: ids[j], name: names.get(ids[j]) || ids[j] },
      });
    }
  }
  return pairs;
}

export function objectsFor(objects, datasourceId) {
  return objects.filter((item) => item.datasource_id === datasourceId);
}

export function fieldsFor(objects, datasourceId, objectNodeId) {
  return objectsFor(objects, datasourceId).find((item) => item.node_id === objectNodeId)?.fields || [];
}

/**
 * Fields on both sides that look like the same key, offered as a starting point.
 *
 * This is a suggestion only. Nothing is confirmed automatically: the plan validator refuses an
 * unconfirmed mapping on purpose, so the UI must never present a same-named pair as an established
 * relationship. It is deliberately literal - two shapes match, and nothing else:
 *
 * 1. the same name once a trailing ``id`` is dropped, so ``customer_id`` matches ``customerid``;
 * 2. a bare ``id`` against a foreign key naming it, so ``customers.id`` matches ``orders.customer_id``.
 *
 * Prefix matching and plural folding are left out on purpose: ``customer_id`` and ``customer_code``
 * share a prefix without being the same key, and a suggestion list that offers them teaches the
 * wrong thing about a confirmation that is enforced strictly.
 */
export function suggestedKeys(leftFields, rightFields) {
  const shape = (name) => {
    const raw = String(name || "").toLowerCase().replace(/[^a-z0-9]/g, "");
    const stem = raw.replace(/id$/, "");
    return { raw, stem: stem || raw };
  };
  const sameKey = (a, b) => {
    if (a.stem === b.stem) return true;
    // One side is the literal primary key ``id``; the other names it as a foreign key.
    if (a.stem === "id" && b.stem !== "id") return b.raw.endsWith("id");
    if (b.stem === "id" && a.stem !== "id") return a.raw.endsWith("id");
    return false;
  };
  const suggestions = [];
  for (const left of leftFields) {
    for (const right of rightFields) {
      if (sameKey(shape(left.path), shape(right.path))) suggestions.push({ left, right });
    }
  }
  return suggestions.slice(0, 20);
}

export function pairKeyOf(mapping) {
  return pairKey(mapping.left_datasource_id, mapping.right_datasource_id);
}

/** Mappings that are not CONFIRMED, which is what a user still has to act on. */
export function needingAttention(mappings) {
  return mappings.filter((item) => !EXECUTABLE_STATUSES.has(item.status));
}

/**
 * Turn a failed confirm into a sentence that names the actual next step.
 *
 * A confirmation fails for a few distinct reasons, and a generic "操作失败" would hide which one.
 * The backend sends a stable code for each, so this maps them and never invents a cause.
 */
export function confirmFailureMessage(error) {
  const code = error?.code;
  if (code === "backend_unavailable") return "无法连接 Qaneris 后端，请确认服务正在运行。";
  if (code === "resource_not_found") return "该关联映射已不存在，请刷新列表。";
  if (code === "invalid_request") {
    return error?.message || "关联映射的内容不合法，请检查所选字段与粒度。";
  }
  return error?.message || "关联映射未确认，请稍后重试。";
}

export function createFailureMessage(error) {
  const code = error?.code;
  if (code === "backend_unavailable") return "无法连接 Qaneris 后端，请确认服务正在运行。";
  if (code === "graph_unavailable") {
    return "已发布的图结构不可用，无法确认字段是否存在。请确认 Neo4j 正在运行并重新扫描数据源。";
  }
  return error?.message || "无法创建关联映射。";
}

/** One-line summary of a mapping, so the list does not render two raw field paths unlabelled. */
export function mappingSummary(mapping) {
  return `${mapping.left_field_path} → ${mapping.right_field_path}`;
}
