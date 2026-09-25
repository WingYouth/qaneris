import { conversationTitle } from "../../conversation/conversationState.js";

export function ConversationSidebar({ conversations, selectedId, onSelect, onCreate, creating, disabled, ready, scope, onScopeChange }) {
  return <aside className="conversation-sidebar" aria-label="对话历史">
    <div className="conversation-sidebar__heading"><strong>对话历史</strong><small>{conversations.length} 个对话</small></div>
    <fieldset className="conversation-scope"><legend>新对话的数据范围</legend>
      <label><input type="radio" checked={scope.length === 0} onChange={() => onScopeChange([])} /> 工作区自动选择</label>
      {ready.map((item) => <label key={item.id}><input type="checkbox" checked={scope.includes(item.id)} onChange={(event) => onScopeChange(event.target.checked ? [...scope, item.id] : scope.filter((id) => id !== item.id))} /> {item.name || item.id}</label>)}
    </fieldset>
    <button className="primary-button conversation-new" type="button" onClick={onCreate} disabled={creating || disabled} aria-label="新对话">+ 新对话</button>
    <nav className="conversation-list" aria-label="已有对话">{conversations.map((item) => <button key={item.conversation_id} type="button"
      aria-current={selectedId === item.conversation_id ? "page" : undefined} className={selectedId === item.conversation_id ? "is-selected" : ""}
      onClick={() => onSelect(item.conversation_id)}>{conversationTitle(item)}<small>{new Date(item.updated_at).toLocaleDateString()}</small></button>)}</nav>
  </aside>;
}
