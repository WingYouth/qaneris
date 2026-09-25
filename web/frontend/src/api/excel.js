import { getJson } from "./client.js";
import { uploadFormData } from "./upload.js";

/** The one HTTP entry point for an Excel import. The browser never chooses a server path. */
export const EXCEL_IMPORT_ENDPOINT = "/api/datasources/import-excel";

/** The only multipart fields the endpoint accepts. */
export const IMPORT_FORM_FIELDS = ["file", "name", "workspace_id"];

/**
 * Build the multipart body.
 *
 * Only the workbook bytes, an optional display name and the workspace are sent. There is no field
 * for a server path, a validation switch, a scan switch or a graph switch: those do not exist as
 * inputs, so no caller can ask for them.
 */
export function buildImportFormData({ file, name, workspaceId = "default" }) {
  const form = new FormData();
  form.append("file", file, file?.name || "upload.xlsx");
  if (name) {
    form.append("name", name);
  }
  form.append("workspace_id", workspaceId);
  return form;
}

export async function importExcel({
  file,
  name,
  workspaceId = "default",
  transport,
  onUploadProgress,
  onUploadComplete,
}) {
  const send = transport || uploadFormData;
  const form = buildImportFormData({ file, name, workspaceId });
  return send(EXCEL_IMPORT_ENDPOINT, form, { onUploadProgress, onUploadComplete });
}

export function listDatasources(workspaceId = "default") {
  return getJson(`/api/datasources?workspace_id=${encodeURIComponent(workspaceId)}`);
}
