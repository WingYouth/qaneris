/**
 * The Ask Scope: which datasource the next question is asked against.
 *
 * Importing a workbook must leave the user with that workbook selected. The browser never copies a
 * datasource id by hand, and a scope is only ever a datasource id the API itself reported.
 */

export function readyDatasources(datasources = []) {
  return datasources.filter((datasource) => datasource?.status === "ready");
}

/** After an import: the imported datasource becomes the scope, without the user doing anything. */
export function scopeAfterImport(importResult) {
  return importResult?.datasourceId || "";
}

/** After a refresh: keep the current scope when it still exists and is still usable. */
export function scopeAfterRefresh(datasources, currentScope) {
  const ready = readyDatasources(datasources);
  if (currentScope && ready.some((datasource) => datasource.id === currentScope)) {
    return currentScope;
  }
  return ready.length ? ready[0].id : "";
}

export function scopeDatasource(datasources, scope) {
  return datasources.find((datasource) => datasource.id === scope) || null;
}

export function scopeLabel(datasources, scope) {
  const datasource = scopeDatasource(datasources, scope);
  if (!datasource) {
    return "";
  }
  return datasource.name ? `${datasource.name}（${datasource.id}）` : datasource.id;
}
