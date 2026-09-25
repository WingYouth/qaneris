import { deleteRequest, getJson, postJson, putJson } from "./client.js";

export const listDatasources = (workspaceId = "default") =>
  getJson(`/api/datasources?workspace_id=${encodeURIComponent(workspaceId)}`);
export const inspectDatasource = (id) => getJson(`/api/datasources/${encodeURIComponent(id)}`);
export const testSavedDatasource = (id) => postJson(`/api/datasources/${encodeURIComponent(id)}/test`, {});
export const listAdapters = () => getJson("/api/adapters");
export const listTlsCapabilities = () => getJson("/api/tls-capabilities");
export const testSecureDatasource = (body) => postJson("/api/datasources/test", body);
export const createSecureDatasource = (body) => postJson("/api/datasources/secure", body);
export const updateSecureDatasource = (id, body) => putJson(`/api/datasources/${encodeURIComponent(id)}/secure`, body);
export const deleteDatasource = (id) => deleteRequest(`/api/datasources/${encodeURIComponent(id)}`);
export const scanDatasource = (id) => postJson(`/api/datasources/${encodeURIComponent(id)}/scan`, {});
