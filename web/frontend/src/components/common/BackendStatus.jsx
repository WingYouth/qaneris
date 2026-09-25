const STATUS_COPY = {
  checking: "正在检查后端",
  available: "后端可用",
  unavailable: "后端不可用",
};

export function BackendStatus({ availability, version }) {
  const label = STATUS_COPY[availability] || STATUS_COPY.checking;

  return (
    <div
      className={`backend-status backend-status--${availability}`}
      role="status"
      aria-live="polite"
    >
      <span className="backend-status__dot" aria-hidden="true" />
      <span>{label}</span>
      {availability === "available" && version ? (
        <span className="backend-status__version">v{version}</span>
      ) : null}
    </div>
  );
}
