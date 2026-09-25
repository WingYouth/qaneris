import { getJson, postJson } from "./client.js";

const id = encodeURIComponent;
export const createConversation = ({ workspaceId = "default", datasourceIds = [], title = "" }) =>
  postJson("/api/conversations", { workspace_id: workspaceId, datasource_ids: datasourceIds, title });
export const listConversations = (workspaceId = "default") =>
  getJson(`/api/conversations?workspace_id=${id(workspaceId)}`);
export const getConversation = (conversationId) => getJson(`/api/conversations/${id(conversationId)}`);
export const createRun = (conversationId, { question, maxRows = 200, clientRequestId }) =>
  postJson(`/api/conversations/${id(conversationId)}/runs`, { question, max_rows: maxRows, client_request_id: clientRequestId });
export const getRun = (runId) => getJson(`/api/runs/${id(runId)}`);
export const clarifyRun = (runId, answer) => postJson(`/api/runs/${id(runId)}/clarification`, { answer });
export const cancelRun = (runId) => postJson(`/api/runs/${id(runId)}/cancel`, {});
export const retryRun = (runId) => postJson(`/api/runs/${id(runId)}/retry`, {});
