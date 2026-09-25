/**
 * Upload lifecycle and the product view of an import result.
 *
 * The phases are the real ones the browser can observe: bytes going out, the server importing, then
 * a decision. There is no backend Streaming contract yet, so no other stage is invented, and the
 * only percentage shown is the one XHR reports for bytes actually sent.
 */

export const IMPORT_PHASE = {
  IDLE: "idle",
  UPLOADING: "uploading",
  IMPORTING: "importing",
  READY: "ready",
  ERROR: "error",
};

export const IMPORT_PHASE_LABEL = {
  idle: "未开始",
  uploading: "上传中",
  importing: "服务端导入中",
  ready: "已就绪",
  error: "导入失败",
};

export function initialImportState() {
  return { phase: IMPORT_PHASE.IDLE, progress: null, result: null, error: null, file: null };
}

export function importStarted(state, file) {
  return { ...initialImportState(), phase: IMPORT_PHASE.UPLOADING, file: file || null };
}

export function importProgress(state, { loaded, total } = {}) {
  if (state.phase !== IMPORT_PHASE.UPLOADING && state.phase !== IMPORT_PHASE.IMPORTING) {
    return state;
  }
  const usable = Number.isFinite(loaded) && Number.isFinite(total) && total > 0;
  return {
    ...state,
    progress: {
      loaded: Number.isFinite(loaded) ? loaded : null,
      total: usable ? total : null,
      percent: usable ? Math.min(100, Math.round((loaded / total) * 100)) : null,
    },
  };
}

/** Every request byte has been handed to the transport; the server is now importing. */
export function importBytesSent(state) {
  if (state.phase !== IMPORT_PHASE.UPLOADING) {
    return state;
  }
  return {
    ...state,
    phase: IMPORT_PHASE.IMPORTING,
    progress: state.progress ? { ...state.progress, percent: 100 } : null,
  };
}

export function importSucceeded(state, payload) {
  return { ...state, phase: IMPORT_PHASE.READY, progress: null, result: payload, error: null };
}

export function importFailed(state, error) {
  return {
    ...state,
    phase: IMPORT_PHASE.ERROR,
    progress: null,
    // A failed attempt never leaves an earlier workbook's result on screen next to its error.
    result: null,
    error: importErrorView(error),
  };
}

/**
 * The shape every Excel refusal shares.
 *
 * The stable code, the sheet and the cell coordinate are the facts a user can act on. A formula body
 * or a stored cell value is never part of the contract, so nothing here tries to show one.
 */
export function importErrorView(error) {
  const detail = error?.payload?.error;
  if (detail && typeof detail === "object") {
    return {
      code: detail.code || "excel_import_failed",
      message: detail.message || "导入失败",
      sheet: detail.sheet || null,
      coordinate: detail.coordinate || null,
      status: error.status ?? null,
    };
  }
  return {
    code: error?.code || "excel_import_failed",
    message: error?.message || "导入失败",
    sheet: null,
    coordinate: null,
    status: error?.status ?? null,
  };
}

export function importErrorLocation(error) {
  if (!error) {
    return "";
  }
  const parts = [];
  if (error.sheet) {
    parts.push(`工作表 ${error.sheet}`);
  }
  if (error.coordinate) {
    parts.push(`单元格 ${error.coordinate}`);
  }
  return parts.join(" · ");
}

/** The product view of a READY import: identity, shape and warnings; never a server location. */
export function importResultView(payload) {
  if (!payload) {
    return null;
  }
  return {
    status: payload.status,
    importId: payload.import_id,
    datasourceId: payload.datasource_id,
    snapshotId: payload.snapshot_id,
    scanVersion: payload.scan_version,
    filename: payload.original_filename,
    fileSha256: payload.file_sha256,
    policyVersion: payload.policy_version,
    publicationVerified: payload.neo4j_publication_verified === true,
    warnings: payload.warnings || [],
    sheets: (payload.sheets || []).map((sheet) => ({
      tableName: sheet.table_name,
      originalName: sheet.original_name,
      rowCount: sheet.row_count,
      columnCount: sheet.column_count,
      columns: (sheet.columns || []).map((column) => ({
        name: column.column_name,
        type: column.inferred_type,
        nullable: column.nullable,
      })),
    })),
  };
}

export function importIsReady(state) {
  return state.phase === IMPORT_PHASE.READY && state.result?.status === "READY";
}
