import { getJson, postJson } from "./client.js";

const id = encodeURIComponent;

/**
 * The published-graph structure a JoinMapping must reference.
 *
 * Confirmation resolves both sides against Neo4j, so the only identifiers that can be confirmed are
 * the graph's own ``obj_...`` / ``field_...`` node ids. They are not derivable from a table name,
 * which is why the form reads them from the backend instead of letting a user type them.
 */
export const listPublishedStructure = (workspaceId = "default", datasourceId) => {
  const query = new URLSearchParams({ workspace_id: workspaceId });
  if (datasourceId) query.set("datasource_id", datasourceId);
  return getJson(`/api/governance/published-structure?${query.toString()}`);
};

export const listJoinMappings = (workspaceId = "default") =>
  getJson(`/api/join-mappings?workspace_id=${id(workspaceId)}`);

/**
 * Register a candidate mapping. This never confirms one: creation only records the intent, and the
 * graph is not read until confirmation, so a candidate with a stale field can still be recorded.
 */
export const createJoinMapping = (body) => postJson("/api/join-mappings", body);

/** Confirm a candidate. The backend re-reads the graph here and rejects a field that is not published. */
export const confirmJoinMapping = (mappingId, confirmedBy) =>
  postJson(`/api/join-mappings/${id(mappingId)}/confirm`, { confirmed_by: confirmedBy });

export const rejectJoinMapping = (mappingId) =>
  postJson(`/api/join-mappings/${id(mappingId)}/reject`, {});
