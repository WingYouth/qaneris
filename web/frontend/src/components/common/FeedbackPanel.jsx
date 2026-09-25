export function FeedbackPanel({ title, children, tone = "neutral", action }) {
  const isError = tone === "error";

  return (
    <section
      className={`feedback-panel feedback-panel--${tone}`}
      role={isError ? "alert" : "status"}
    >
      <span className="feedback-panel__icon" aria-hidden="true">
        {isError ? "!" : "…"}
      </span>
      <div className="feedback-panel__copy">
        <strong>{title}</strong>
        {children ? <p>{children}</p> : null}
      </div>
      {action ? <div className="feedback-panel__action">{action}</div> : null}
    </section>
  );
}
