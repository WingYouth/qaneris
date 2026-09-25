import { IMPORT_PHASE, IMPORT_PHASE_LABEL, importErrorLocation } from "../../excel/importState.js";

/**
 * The Excel Import Card.
 *
 * `accept=".xlsx"` is a convenience for the file picker only. It is not a security check: the file
 * that actually gets uploaded is validated by the Core, deterministically, and a refusal comes back
 * as a stable code with the sheet and cell the user has to fix.
 */
export function ExcelImportCard({
  state,
  fileName,
  datasourceName,
  workspaceId,
  disabled,
  onFileNameChange,
  onDatasourceNameChange,
  onWorkspaceChange,
  onSubmit,
}) {
  const busy =
    state.phase === IMPORT_PHASE.UPLOADING || state.phase === IMPORT_PHASE.IMPORTING;
  const result = state.result;

  return (
    <section className="surface-card import-card" aria-labelledby="excel-import-title">
      <div className="foundation-card__header">
        <span className="step-label">Excel 导入</span>
        <span className={`status-pill status-pill--upload-${state.phase}`}>
          {IMPORT_PHASE_LABEL[state.phase]}
        </span>
      </div>

      <h2 id="excel-import-title">导入工作簿</h2>
      <p>
        浏览器直接上传文件。数据校验、受管文件、不可变 SQLite、扫描和 Neo4j 发布均由服务器完成。
      </p>

      <form
        className="import-form"
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
      >
        <label className="field">
          <span className="field__label">工作簿（.xlsx）</span>
          <input
            className="field__input"
            type="file"
            accept=".xlsx"
            disabled={busy}
            onChange={(event) => onFileNameChange(event.target.files?.[0] || null)}
          />
        </label>

        <div className="field-row">
          <label className="field">
            <span className="field__label">数据源名称（可选）</span>
            <input
              className="field__input"
              type="text"
              maxLength={100}
              disabled={busy}
              value={datasourceName}
              placeholder="Excel: <filename>"
              onChange={(event) => onDatasourceNameChange(event.target.value)}
            />
          </label>
          <label className="field">
            <span className="field__label">Workspace</span>
            <input
              className="field__input"
              type="text"
              value={workspaceId}
              disabled={busy}
              onChange={(event) => onWorkspaceChange(event.target.value)}
            />
          </label>
        </div>

        <button className="primary-button" type="submit" disabled={disabled || busy || !fileName}>
          {busy ? "导入中…" : "上传并导入"}
        </button>
      </form>

      {state.phase === IMPORT_PHASE.UPLOADING ? (
        <p className="import-progress" role="status" aria-live="polite">
          {state.progress?.percent === null || state.progress?.percent === undefined
            ? "正在上传工作簿…"
            : `已上传 ${state.progress.percent}%`}
        </p>
      ) : null}

      {state.phase === IMPORT_PHASE.IMPORTING ? (
        <p className="import-progress" role="status" aria-live="polite">
          文件已上传，服务器正在校验并发布数据源…
        </p>
      ) : null}

      {state.error ? (
        <div className="import-error" role="alert">
          <strong>{state.error.code}</strong>
          <p>{state.error.message}</p>
          {importErrorLocation(state.error) ? <small>{importErrorLocation(state.error)}</small> : null}
        </div>
      ) : null}

      {result ? (
        <div className="import-result" role="status">
          <p className="import-result__headline">
            数据源 {result.status} · {result.filename}
          </p>
          <dl className="identity-grid">
            <div>
              <dt>数据源</dt>
              <dd>{result.datasourceId}</dd>
            </div>
            <div>
              <dt>快照</dt>
              <dd>{result.snapshotId}</dd>
            </div>
            <div>
              <dt>扫描版本</dt>
              <dd>{result.scanVersion}</dd>
            </div>
            <div>
              <dt>Neo4j 发布校验</dt>
              <dd>{result.publicationVerified ? "已验证" : "未验证"}</dd>
            </div>
          </dl>

          <ul className="sheet-list">
            {result.sheets.map((sheet) => (
              <li key={sheet.tableName}>
                <strong>{sheet.tableName}</strong>
                <span>
                  {sheet.rowCount} rows · {sheet.columnCount} columns
                </span>
                <span className="sheet-list__columns">
                  {sheet.columns.map((column) => `${column.name}:${column.type}`).join(", ")}
                </span>
              </li>
            ))}
          </ul>

          {result.warnings.length ? (
            <ul className="warning-list">
              {result.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
