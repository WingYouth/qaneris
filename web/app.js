const byId = (id) => document.getElementById(id);
const state = { sources: [], selectedId: null };

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.error?.message || body.detail || "请求失败，请稍后重试");
    error.code = body.error?.code || "request_failed";
    throw error;
  }
  return body;
}

function escapeHtml(value) {
  const element = document.createElement("div");
  element.textContent = String(value ?? "");
  return element.innerHTML;
}

function showToast(message, type = "") {
  const toast = byId("toast");
  toast.textContent = message;
  toast.className = `toast show ${type}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.className = "toast"; }, 2800);
}

function selectedSource() {
  return state.sources.find((source) => source.id === state.selectedId);
}

function renderActiveSource() {
  byId("active-source").textContent = selectedSource()?.name || "尚未选择";
}

async function checkHealth() {
  const indicator = byId("service-state");
  try {
    await request("/health");
    indicator.className = "service-state ready";
    indicator.querySelector("span:last-child").textContent = "服务正常";
  } catch {
    indicator.className = "service-state error";
    indicator.querySelector("span:last-child").textContent = "服务不可用";
  }
}

async function loadSources() {
  state.sources = await request("/api/datasources");
  if (!state.sources.some((source) => source.id === state.selectedId)) {
    state.selectedId = state.sources[0]?.id || null;
  }
  byId("source-count").textContent = String(state.sources.length);
  renderActiveSource();
  renderSources();
}

async function loadAdapters() {
  const adapters = await request("/api/adapters");
  const select = byId("driver");
  select.replaceChildren();
  adapters.forEach((adapter) => {
    const option = document.createElement("option");
    option.value = adapter.driver;
    option.textContent = adapter.driver;
    option.dataset.kind = adapter.kind;
    option.dataset.connection = JSON.stringify(adapter.connection_example || {});
    select.appendChild(option);
  });
  const sqlite = Array.from(select.options).find((option) => option.value === "sqlite");
  if (sqlite) select.value = "sqlite";
  select.dispatchEvent(new Event("change"));
}

function renderSources() {
  const container = byId("sources");
  if (!state.sources.length) {
    container.innerHTML = '<p class="empty">尚未连接数据源<br>请添加一个 SQLite 数据库</p>';
    return;
  }
  container.replaceChildren();
  state.sources.forEach((source) => {
    const row = document.createElement("div");
    row.className = `source-row${source.id === state.selectedId ? " selected" : ""}`;
    row.innerHTML = `
      <button type="button" class="source-select">
        <span class="source-icon" aria-hidden="true">DB</span>
        <span class="source-copy"><strong>${escapeHtml(source.name)}</strong><span>${escapeHtml(source.driver || source.kind)} · ${escapeHtml(source.status)}</span></span>
      </button>
      <button type="button" class="scan-button">扫描</button>`;
    row.querySelector(".source-select").addEventListener("click", () => {
      state.selectedId = source.id;
      renderActiveSource();
      renderSources();
    });
    const scan = row.querySelector(".scan-button");
    const scanSource = async (event) => {
      event.stopPropagation();
      scan.textContent = "扫描中";
      try {
        await request(`/api/datasources/${source.id}/scan`, { method: "POST" });
        await loadSources();
        showToast(`${source.name} 扫描完成`);
      } catch (error) {
        showToast(error.message, "error");
      } finally {
        scan.textContent = "扫描";
      }
    };
    scan.addEventListener("click", scanSource);
    container.appendChild(row);
  });
}

function selectPane(tab) {
  document.querySelectorAll(".tab").forEach((item) => {
    const active = item === tab;
    item.classList.toggle("active", active);
    item.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".pane").forEach((pane) => {
    pane.classList.toggle("active", pane.id === tab.dataset.pane);
  });
}

function renderTable(result) {
  if (!result.rows.length) {
    byId("result").innerHTML = '<p class="empty">没有找到匹配的数据</p>';
    return;
  }
  const header = result.columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("");
  const body = result.rows.map((row) => {
    const cells = result.columns.map((column) => `<td>${escapeHtml(row[column])}</td>`).join("");
    return `<tr>${cells}</tr>`;
  }).join("");
  byId("result").innerHTML = `<div class="table-wrap"><table><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function renderResponse(data) {
  byId("result-section").hidden = false;
  byId("answer").textContent = data.answer;
  byId("plan").textContent = JSON.stringify(data.plan, null, 2);
  byId("row-count").textContent = data.result.row_count.toLocaleString();
  byId("column-count").textContent = data.result.columns.length.toLocaleString();
  byId("result-status").textContent = data.result.truncated ? "已截断" : "完成";
  byId("result-badge").textContent = data.result.truncated ? "结果已截断" : "查询完成";
  renderTable(data.result);
  byId("result-section").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

document.querySelectorAll("[data-question]").forEach((button) => {
  button.addEventListener("click", () => {
    byId("question").value = button.dataset.question;
    byId("question").focus();
  });
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => selectPane(tab));
});

byId("question").addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") byId("ask").click();
});

byId("connect").addEventListener("click", async () => {
  const name = byId("name").value.trim();
  const driverSelect = byId("driver");
  const driver = driverSelect.value;
  const kind = driverSelect.selectedOptions[0].dataset.kind;
  const connectionText = byId("connection").value.trim();
  if (!name || !connectionText) {
    showToast("请填写数据源名称和连接配置", "error");
    return;
  }
  const button = byId("connect");
  try {
    button.disabled = true;
    button.textContent = "正在连接…";
    byId("form-status").textContent = "";
    const connection = JSON.parse(connectionText);
    const source = await request("/api/datasources", {
      method: "POST",
      body: JSON.stringify({ name, kind, connection: { ...connection, driver } }),
    });
    state.selectedId = source.id;
    await request(`/api/datasources/${source.id}/scan`, { method: "POST" });
    await loadSources();
    byId("form-status").textContent = "连接成功，元数据已就绪";
    showToast("数据源已连接并完成扫描");
  } catch (error) {
    byId("form-status").textContent = error.message;
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "测试连接并添加";
  }
});

byId("driver").addEventListener("change", (event) => {
  const example = event.target.selectedOptions[0]?.dataset.connection || "{}";
  byId("connection").value = JSON.stringify(JSON.parse(example), null, 2);
});

byId("ask").addEventListener("click", async () => {
  const question = byId("question").value.trim();
  if (!question) {
    showToast("请先输入一个数据问题", "error");
    byId("question").focus();
    return;
  }
  if (!state.selectedId) {
    showToast("请先连接并选择数据源", "error");
    return;
  }
  const button = byId("ask");
  try {
    button.disabled = true;
    button.textContent = "查询中…";
    const response = await request("/api/ask", {
      method: "POST",
      body: JSON.stringify({ question, datasource_id: state.selectedId }),
    });
    renderResponse(response);
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
    button.innerHTML = '查询数据 <span aria-hidden="true">→</span>';
  }
});

Promise.all([checkHealth(), loadSources(), loadAdapters()]).catch((error) => showToast(error.message, "error"));
