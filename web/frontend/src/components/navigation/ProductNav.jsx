const ITEMS = [
  { id: "ask", label: "智能问数", hint: "ASK", icon: <path d="m12 2 1.8 6.2L20 10l-6.2 1.8L12 18l-1.8-6.2L4 10l6.2-1.8L12 2Zm7 14 .8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8L19 16Z" /> },
  { id: "datasources", label: "数据源", hint: "SOURCES", icon: <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12v7c0 1.7 3.6 3 8 3s8-1.3 8-3v-7" /></> },
  { id: "excel", label: "Excel 导入", hint: "IMPORT", icon: <><path d="M4 3h11l5 5v13H4V3Z" /><path d="M15 3v5h5M8 12h8M8 16h8" /></> },
  { id: "governance", label: "关联映射", hint: "GOVERNANCE", icon: <><path d="M9 7H6a4 4 0 0 0 0 8h3" /><path d="M15 7h3a4 4 0 0 1 0 8h-3" /><path d="M8 11h8" /></> },
];

export function ProductNav({ activeView, onNavigate }) {
  return <nav className="primary-nav" aria-label="产品导航">
    <span className="primary-nav__label">工作区</span>
    {ITEMS.map(({ id, icon, label, hint }) => <button key={id} type="button"
      className={`primary-nav__item ${activeView === id ? "primary-nav__item--active" : ""}`}
      aria-current={activeView === id ? "page" : undefined} onClick={() => onNavigate(id)}>
      <span className="primary-nav__icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">{icon}</svg></span>
      <span className="primary-nav__copy"><strong>{label}</strong><small>{hint}</small></span>
      <span className="primary-nav__arrow" aria-hidden="true">↗</span>
    </button>)}
  </nav>;
}
