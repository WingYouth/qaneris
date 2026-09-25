/**
 * A clarification the grounding step asked for.
 *
 * The text is the backend's own question, shown as-is. The UI never picks an interpretation on the
 * user's behalf, never adds a field the backend did not bind, and never retries the question by
 * itself - the user narrows the question and asks again.
 */
export function ClarificationPanel({ clarification, onUseOption }) {
  if (!clarification || clarification.length === 0) {
    return null;
  }

  return (
    <div className="clarification-panel" role="status" aria-live="polite">
      <strong>请补充问题范围</strong>
      <ol>
        {clarification.map((item, index) => (
          <li key={`${item.question}-${index}`}>
            <p>{item.question}</p>
            {item.options?.length ? (
              <div className="clarification-panel__options">
                {item.options.map((option) => (
                  <button
                    className="secondary-button"
                    type="button"
                    key={option}
                    onClick={() => onUseOption(option)}
                  >
                    {option}
                  </button>
                ))}
              </div>
            ) : null}
          </li>
        ))}
      </ol>
      <p className="clarification-panel__hint">
        请明确要查询的指标、维度或时间范围，然后重新发问。SmartData 不会自行猜测业务含义或数据库字段。
      </p>
    </div>
  );
}
