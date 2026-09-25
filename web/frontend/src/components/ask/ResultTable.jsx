/**
 * The result table.
 *
 * Every cell is rendered as text. A value that arrived as an object or an array is shown as its JSON
 * text rather than interpreted, and nothing here ever builds markup from backend content.
 */
function cellText(value) {
  if (value === null || value === undefined) {
    return "—";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

export function ResultTable({ table }) {
  if (!table || table.columns.length === 0) {
    return null;
  }

  return (
    <div className="result-table">
      <div className="result-table__meta">
        <strong>{table.rowCount}</strong> row{table.rowCount === 1 ? "" : "s"}
        {table.truncated ? <span className="result-table__truncated">truncated</span> : null}
      </div>
      <div className="result-table__scroll">
        <table>
          <thead>
            <tr>
              {table.columns.map((column) => (
                <th key={column} scope="col">
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.rows.map((row, index) => (
              <tr key={index}>
                {table.columns.map((column) => (
                  <td key={column}>{cellText(row?.[column])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
