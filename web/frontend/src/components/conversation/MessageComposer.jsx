export function MessageComposer({ value, onChange, onSend, disabled, hint }) {
  return <form className="message-composer" onSubmit={(event) => { event.preventDefault(); onSend(); }}>
    <label htmlFor="conversation-question">继续提问</label>
    <textarea id="conversation-question" rows={2} value={value} onChange={(event) => onChange(event.target.value)} disabled={disabled}
      onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); onSend(); } }}
      placeholder="例如：今年销售额是多少？" />
    <div><small>{hint}</small><button className="primary-button" type="submit" aria-label="发送问题" disabled={disabled || !value.trim()}>发送 ↗</button></div>
  </form>;
}
