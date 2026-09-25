export const formatNumber = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 4 });

export function numericDomain(values, includeZero = false) {
  const finite = values.filter((value) => typeof value === "number" && Number.isFinite(value));
  if (!finite.length) return null;
  let min = Math.min(...finite); let max = Math.max(...finite);
  if (includeZero) { min = Math.min(min, 0); max = Math.max(max, 0); }
  if (min === max) { const pad = Math.abs(min) * 0.1 || 1; min -= pad; max += pad; }
  return [min, max];
}

export function linearScale(domain, range) {
  const [min, max] = domain; const [start, end] = range;
  return (value) => start + (value - min) * (end - start) / (max - min);
}

export function pieSlices(values) {
  if (!values.length || values.some((value) => typeof value !== "number" || !Number.isFinite(value) || value < 0)) return null;
  const total = values.reduce((sum, value) => sum + value, 0);
  if (!Number.isFinite(total) || total <= 0) return null;
  let start = 0;
  return values.map((value) => {
    const end = start + value / total * 2 * Math.PI;
    const slice = { start, end, value, fraction: value / total };
    start = end;
    return slice;
  });
}

export function lineSegments(points, field) {
  const groups = []; let current = [];
  points.forEach((point, index) => {
    const value = point.values[field];
    if (value == null) { if (current.length) groups.push(current); current = []; }
    else current.push({ index, value, category: point.category });
  });
  if (current.length) groups.push(current);
  return groups;
}
