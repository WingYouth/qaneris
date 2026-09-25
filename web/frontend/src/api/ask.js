import { postJson } from "./client.js";

/** The existing unified Ask endpoint. The Web slice adds no query path of its own. */
export const ASK_ENDPOINT = "/api/ask";

/**
 * Ask one question scoped to one datasource.
 *
 * The body is exactly the existing ``AskRequest`` contract: ``question``, ``workspace_id`` and
 * ``datasource_id``. No SQL, no plan, no field list and no ``max_rows`` override is offered by the
 * UI, so the browser cannot shape the query - it only says what it wants to know.
 */
export function buildAskRequest({ question, workspaceId = "default", datasourceId }) {
  return {
    question,
    workspace_id: workspaceId,
    datasource_id: datasourceId || null,
  };
}

export function askQuestion({ question, workspaceId, datasourceId }) {
  return postJson(ASK_ENDPOINT, buildAskRequest({ question, workspaceId, datasourceId }));
}
