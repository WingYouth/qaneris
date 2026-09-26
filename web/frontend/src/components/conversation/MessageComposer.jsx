/**
 * The one Ask composer.
 *
 * The example questions are shortcuts into the same draft state the textarea owns: choosing one
 * fills the box and the user still sends it, so nothing is submitted on the user's behalf.
 */
export function MessageComposer({ value, onChange, onSend, disabled, hint, confirming = false, scopeNames = [], automaticScope = false }) {
  const examples = scopeNames.length === 1
    ? ["这个数据源的数据结构是什么？", "这个数据源有哪些字段？"]
    : [
      ...(automaticScope ? ["当前工作区有哪些数据源？"] : []),
      ...scopeNames.slice(0, 2).map((name) => `${name} 的数据结构是什么？`),
    ];
  return <div className="message-composer">
    <form className="composer__box" onSubmit={(event) => { event.preventDefault(); onSend(); }}>
      <label className="sr-only" htmlFor="conversation-question">继续提问</label>
      <textarea id="conversation-question" rows={2} value={value} onChange={(event) => onChange(event.target.value)} disabled={disabled}
        onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); onSend(); } }}
        placeholder={confirming ? "在这里输入对上方问题的确认内容" : scopeNames.length === 1 ? "继续提问，例如：这个数据源有哪些字段？" : scopeNames.length ? `请写明数据源，例如：${scopeNames[0]} 的数据结构是什么？` : "先添加数据源，再提问"} />
      <div className="composer__actions">
        <button className="icon-button" type="button" disabled title="暂不支持上传附件" aria-label="上传附件">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M21.4 11.1 12.3 20a5.5 5.5 0 0 1-7.8-7.8l8.5-8.5a3.7 3.7 0 0 1 5.2 5.2l-8.5 8.5a1.8 1.8 0 0 1-2.6-2.6l7.8-7.8" />
          </svg>
        </button>
        <button className="icon-button" type="button" disabled title="暂不支持语音输入" aria-label="语音输入">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <rect x="9" y="2.5" width="6" height="11" rx="3" /><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7" />
          </svg>
        </button>
        <button className="composer__send" type="submit" aria-label={confirming ? "发送确认" : "发送问题"} disabled={disabled || !value.trim()}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M4.5 12h13M12 6.5 17.5 12 12 17.5" />
          </svg>
        </button>
      </div>
    </form>
    {!confirming ? <div className="composer__examples"><span>示例问题</span>
      {examples.map((question) => <button key={question} type="button" disabled={disabled} onClick={() => onChange(question)}>{question}</button>)}
    </div> : null}
    {hint ? <p className="composer__hint">{hint}</p> : null}
  </div>;
}
