import { useEffect, useRef, useState } from "react";
import { createSecureDatasource, listAdapters, listTlsCapabilities, scanDatasource, testSecureDatasource, updateSecureDatasource } from "../../api/datasources.js";
import { createTextCredential, uploadCaCertificate, uploadClientIdentity, deleteCredential } from "../../api/credentials.js";
import { DriverPicker, driverName } from "./DriverPicker.jsx";

const DEFAULT_PORTS = { postgresql: "5432", timescaledb: "5432", mysql: "3306", oracle: "1521", sqlserver: "1433", mongodb: "27017", redis: "6379", cassandra: "9042", hbase: "9090", neo4j: "7687", couchdb: "5984", influxdb: "8086", clickhouse: "8123", elasticsearch: "9200", opensearch: "9200", milvus: "19530", qdrant: "6333", weaviate: "8080" };
const AUTH_OPTIONS = [
  ["password", "用户名和密码"], ["none", "无需认证"], ["token", "Token"],
  ["api_key", "API Key"], ["private_key", "已有私钥凭据"], ["service_account", "已有服务账号凭据"],
  ["cloud_identity", "云端身份"],
];
const DRIVER_AUTH = {
  sqlite: ["none"], postgresql: ["password", "none"], timescaledb: ["password", "none"],
  mysql: ["password", "none"], oracle: ["password", "none"], sqlserver: ["password", "none"],
  clickhouse: ["password", "none"], snowflake: ["password", "none"], bigquery: ["cloud_identity", "none"],
  redis: ["password", "none"], mongodb: ["password", "none"], couchdb: ["password", "none"],
  cassandra: ["password", "none"], hbase: ["none"], neo4j: ["password", "none"],
  influxdb: ["token"], elasticsearch: ["password", "api_key", "none"], opensearch: ["password", "none"],
  milvus: ["password", "token", "none"], qdrant: ["api_key", "none"], weaviate: ["api_key", "none"],
};
const MANAGED_TEXT_AUTH = new Set(["password", "token", "api_key"]);
const HOST_ONLY = new Set(["cassandra", "hbase"]);
const SPECIAL_ENDPOINT = new Set(["sqlite", "bigquery", "snowflake"]);
const DATABASE_DRIVERS = new Set(["postgresql", "timescaledb", "mysql", "oracle", "sqlserver", "mongodb", "redis", "couchdb", "neo4j", "clickhouse", "milvus"]);

const emptyForm = (workspaceId, existing) => ({
  name: existing?.name || "", workspace: existing?.workspace_id || workspaceId,
  driver: existing?.driver || "", mode: "local", locatorMode: "host",
  host: "", port: existing?.driver ? DEFAULT_PORTS[existing.driver] || "" : "",
  path: "", url: "", database: "", namespace: "", project: "", account: "", org: "", bucket: "",
  options: "{}", auth: existing?.driver === "influxdb" ? "token" : "none", username: "", source: "create", secretId: "", rawSecret: "", allowInsecureKeyHttp: false,
  tls: ["sqlserver", "opensearch"].includes(existing?.driver), verifyServer: true, tlsVersion: "1.2", serverName: "", caId: "", caFile: null,
  certId: "", keyId: "", certFile: null, keyFile: null, keyPassword: "",
});

function Field({ label, hint, className = "", children }) {
  return <label className={`connection-field ${className}`}><span>{label}</span>{children}{hint ? <small>{hint}</small> : null}</label>;
}

function buildProfile(draft) {
  const endpoint = {};
  for (const key of ["database", "namespace", "project", "account"]) {
    if (draft[key].trim()) endpoint[key] = draft[key].trim();
  }
  if (draft.driver === "sqlite") endpoint.path = draft.path.trim();
  else if (draft.driver === "bigquery") endpoint.url = `bigquery://${draft.project.trim()}/${draft.namespace.trim()}`;
  else if (draft.driver === "snowflake") endpoint.url = `snowflake://${draft.account.trim()}/${draft.database.trim()}/${draft.namespace.trim()}`;
  else if (HOST_ONLY.has(draft.driver) || draft.locatorMode === "host") {
    endpoint.hosts = [{ host: draft.host.trim(), ...(draft.port.trim() ? { port: Number(draft.port) } : {}) }];
  } else endpoint.url = draft.url.trim();

  const authentication = { method: draft.auth };
  if (draft.username.trim()) authentication.username = draft.username.trim();
  const authField = { password: "password", token: "token", api_key: "api_key", private_key: "private_key", service_account: "service_account" }[draft.auth];
  if (authField && draft.secretId.trim()) authentication[authField] = { provider: "managed", identifier: draft.secretId.trim() };

  const tls = { enabled: draft.tls, verify_server: draft.verifyServer, minimum_version: draft.tlsVersion };
  if (draft.tls) {
    if (draft.serverName.trim()) tls.server_name = draft.serverName.trim();
    if (draft.caId.trim()) tls.ca_certificate = { provider: "managed", identifier: draft.caId.trim() };
    if (draft.certId.trim() && draft.keyId.trim()) {
      tls.client_certificate = { provider: "managed", identifier: draft.certId.trim() };
      tls.client_private_key = { provider: "managed", identifier: draft.keyId.trim() };
    }
  }
  let options;
  try { options = JSON.parse(draft.options || "{}"); }
  catch { throw new Error("高级选项需要填写有效的 JSON 对象。"); }
  if (!options || Array.isArray(options) || typeof options !== "object") throw new Error("高级选项需要填写 JSON 对象。");
  if (draft.driver === "influxdb") {
    options.org = draft.org.trim();
    options.bucket = draft.bucket.trim();
  }
  if (draft.driver === "qdrant" && !draft.tls && draft.allowInsecureKeyHttp) options.allow_insecure_api_key_http = true;
  return { driver: draft.driver, deployment_mode: draft.mode, endpoint, authentication, tls, options };
}

function validateForm(form, capability, kind, existing) {
  if (!kind) return "请选择可用的数据库驱动。";
  if (!existing && !form.name.trim()) return "请填写连接名称。";
  if (!form.workspace.trim()) return "请填写工作区。";
  if (form.driver === "sqlite" && !form.path.trim()) return "请填写服务器可访问的 SQLite 文件路径。";
  if (form.driver === "bigquery" && (!form.project.trim() || !form.namespace.trim())) return "请填写 Project 和 Dataset。";
  if (form.driver === "snowflake" && (!form.account.trim() || !form.database.trim() || !form.namespace.trim())) return "请填写 Account、Database 和 Schema。";
  if (form.driver === "redis" && form.database.trim() && !/^\d+$/.test(form.database.trim())) return "Redis 数据库请填写非负整数编号，例如 0。";
  if (form.driver === "oracle" && form.locatorMode === "host" && !form.database.trim()) return "请填写 Oracle Service Name。";
  if (form.driver === "influxdb" && (!form.org.trim() || !form.bucket.trim())) return "请填写 InfluxDB Organization 和 Bucket。";
  if (form.driver === "qdrant" && form.auth === "api_key" && !form.tls && !form.allowInsecureKeyHttp) return "Qdrant API Key 通过 HTTP 传输前，请开启 HTTPS，或明确允许明文传输。";
  if (!(DRIVER_AUTH[form.driver] || ["none"]).includes(form.auth)) return "当前驱动不支持所选的认证方式。";
  if (!SPECIAL_ENDPOINT.has(form.driver)) {
    if (HOST_ONLY.has(form.driver) || form.locatorMode === "host") {
      if (!form.host.trim()) return "请填写主机地址。";
      if (form.port.trim() && (!/^\d+$/.test(form.port) || Number(form.port) < 1 || Number(form.port) > 65535)) return "端口应为 1 到 65535 之间的数字。";
    } else if (!form.url.trim()) return "请填写不含账号密码的连接 URL。";
  }
  if (form.auth === "password" && !form.username.trim()) return "请填写用户名。";
  if (MANAGED_TEXT_AUTH.has(form.auth)) {
    if (form.source === "create" && !form.rawSecret && !form.secretId.trim()) return "请填写密码或凭据值。";
    if (form.source === "existing" && !form.secretId.trim()) return "请填写已有 Secret ID。";
  } else if (["private_key", "service_account"].includes(form.auth) && !form.secretId.trim()) return "请填写已有 Secret ID。";
  if (form.tls && !capability) return "当前驱动没有可用的受管 TLS 配置。";
  if (form.tls && (Boolean(form.certFile) !== Boolean(form.keyFile))) return "客户端证书和私钥文件必须成对上传。";
  if (form.tls && (Boolean(form.certId.trim()) !== Boolean(form.keyId.trim()))) return "已有客户端证书和私钥 Secret ID 必须成对填写。";
  return "";
}

export function DatasourceWizard({ workspaceId = "default", onWorkspaceChange, existing, onCancel, onSaved }) {
  const [screen, setScreen] = useState(existing ? "configure" : "picker");
  const [tab, setTab] = useState("basic");
  const [form, setForm] = useState(() => emptyForm(workspaceId, existing));
  const [adapters, setAdapters] = useState([]);
  const [capabilities, setCapabilities] = useState([]);
  const [validatedProfile, setValidatedProfile] = useState(null);
  const [testResult, setTestResult] = useState(null);
  const [saved, setSaved] = useState(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState(null);
  const createdSecrets = useRef(new Set());

  useEffect(() => {
    listAdapters().then(setAdapters).catch(setError);
    listTlsCapabilities().then(setCapabilities).catch(setError);
  }, []);

  const adapter = adapters.find((item) => item.driver === form.driver);
  const capability = capabilities.find((item) => item.driver === form.driver);
  const kind = adapter?.kind || existing?.kind;
  const isBusy = Boolean(busy);
  const targetWorkspace = form.workspace.trim() || workspaceId;

  const update = (key, value) => {
    setForm((current) => ({ ...current, [key]: value }));
    setValidatedProfile(null);
    setTestResult(null);
    setError(null);
  };

  const chooseDriver = (driver) => {
    if (form.driver === driver) return;
    setForm((current) => ({
      ...emptyForm(current.workspace, null), name: current.name, workspace: current.workspace,
      driver, port: DEFAULT_PORTS[driver] || "",
      tls: ["sqlserver", "opensearch"].includes(driver),
      auth: ["postgresql", "timescaledb", "mysql", "sqlserver"].includes(driver) ? "password" : driver === "influxdb" ? "token" : "none",
    }));
    setValidatedProfile(null);
    setTestResult(null);
    setError(null);
  };

  const materialize = async () => {
    const draft = { ...form };
    if (MANAGED_TEXT_AUTH.has(draft.auth) && draft.source === "create" && draft.rawSecret) {
      const secret = await createTextCredential({ kind: draft.auth, value: draft.rawSecret });
      createdSecrets.current.add(secret.secret_id);
      draft.secretId = secret.secret_id;
      draft.rawSecret = "";
      draft.source = "existing";
      setForm((current) => ({ ...current, secretId: secret.secret_id, rawSecret: "", source: "existing" }));
    }
    if (draft.tls && draft.caFile && capability?.custom_ca) {
      const secret = await uploadCaCertificate(draft.caFile);
      createdSecrets.current.add(secret.secret_id);
      draft.caId = secret.secret_id;
      draft.caFile = null;
      setForm((current) => ({ ...current, caId: secret.secret_id, caFile: null }));
    }
    if (draft.tls && draft.certFile && draft.keyFile && capability?.mtls) {
      const pair = await uploadClientIdentity({ certificate: draft.certFile, privateKey: draft.keyFile, privateKeyPassword: draft.keyPassword });
      const certId = pair.client_certificate.secret_id;
      const keyId = pair.client_private_key.secret_id;
      createdSecrets.current.add(certId);
      createdSecrets.current.add(keyId);
      Object.assign(draft, { certId, keyId, certFile: null, keyFile: null, keyPassword: "" });
      setForm((current) => ({ ...current, certId, keyId, certFile: null, keyFile: null, keyPassword: "" }));
    }
    return draft;
  };

  const runTest = async () => {
    const problem = validateForm(form, capability, kind, existing);
    if (problem) { setError(new Error(problem)); return; }
    setBusy("testing");
    setError(null);
    try {
      const draft = await materialize();
      const candidate = buildProfile(draft);
      const response = await testSecureDatasource({ kind, connection_profile: candidate });
      setValidatedProfile(candidate);
      setTestResult(response);
    } catch (failure) {
      setValidatedProfile(null);
      setTestResult(null);
      setError(failure);
    } finally { setBusy(""); }
  };

  const finishScan = async (id) => {
    setBusy("scanning");
    try {
      await scanDatasource(id);
      onWorkspaceChange?.(targetWorkspace);
      await onSaved(id, targetWorkspace);
    } catch (failure) { setError(failure); }
    finally { setBusy(""); }
  };

  const saveAndScan = async () => {
    if (!validatedProfile || isBusy) return;
    setBusy("saving");
    setError(null);
    try {
      const response = existing
        ? await updateSecureDatasource(existing.id, { connection_profile: validatedProfile })
        : await createSecureDatasource({ name: form.name.trim(), workspace_id: targetWorkspace, kind, connection_profile: validatedProfile });
      setSaved(response);
      setScreen("saved");
      await finishScan(existing?.id || response.id);
    } catch (failure) { setError(failure); }
    finally { setBusy(""); }
  };

  const cancel = async () => {
    if (isBusy) return;
    if (!saved) {
      setBusy("cleaning");
      const failures = [];
      for (const id of createdSecrets.current) {
        try { await deleteCredential(id); }
        catch { failures.push(id); }
      }
      setBusy("");
      if (failures.length) { setError(new Error("部分未使用的凭据清理失败，请检查凭据管理记录。")); return; }
    } else onWorkspaceChange?.(targetWorkspace);
    onCancel();
  };

  const textInput = (key, label, options = {}) => <Field label={label} hint={options.hint} className={options.className || ""}><input type={options.type || "text"} value={form[key]} placeholder={options.placeholder || ""} autoComplete={options.autoComplete} onChange={(event) => update(key, event.target.value)} /></Field>;
  const secretSummary = (id, file) => file?.name || (id ? "已选择受管凭据" : "");
  const fileInput = (key, label, summary, accept) => <Field label={label} className="connection-field--file"><input type="file" accept={accept} aria-label={label} onChange={(event) => update(key, event.target.files?.[0] || null)} /><span className={summary ? "connection-field__file is-selected" : "connection-field__file"}><span>{summary || "选择文件上传"}</span><span>{summary ? "更换 ↗" : "上传 ↗"}</span></span></Field>;

  if (screen === "picker") return <DriverPicker adapters={adapters} selected={form.driver} onSelect={chooseDriver} onNext={() => setScreen("configure")} onCancel={cancel} />;

  const isSaved = screen === "saved";
  return <div className="connection-flow">
    <div className="connection-flow__back"><button type="button" disabled={isBusy} onClick={existing || isSaved ? cancel : () => setScreen("picker")}>← {existing || isSaved ? "返回数据源" : "返回数据库选择"}</button></div>
    <header className="connection-flow__heading"><div className="connection-flow__heading-main"><span className="connection-flow__driver-mark" aria-hidden="true">{driverName(form.driver).replace(/[^A-Za-z]/g, "").slice(0, 2).toUpperCase()}</span><div><h1>{existing ? `重新连接 ${driverName(form.driver)}` : `${driverName(form.driver)} 连接`}</h1><p>{isSaved ? "连接设置已保存。完成扫描后即可在工作区使用。" : "填写连接信息与证书，再从 Qaneris 后端测试连接。"}</p></div></div><span>{isSaved ? "03 / 03 · 扫描" : "02 / 03 · 连接设置"}</span></header>

    {isSaved ? <section className="surface-card connection-saved"><span className="connection-saved__icon">✓</span><h2>连接已保存</h2><p>数据源记录已写入后端目录。扫描会读取结构并发布查询所需的信息。</p><dl><div><dt>连接名称</dt><dd>{form.name}</dd></div><div><dt>驱动</dt><dd>{driverName(form.driver)}</dd></div><div><dt>工作区</dt><dd>{targetWorkspace}</dd></div></dl>{error ? <div className="connection-feedback connection-feedback--error" role="alert"><strong>扫描未完成</strong><p>{error.message}</p></div> : null}<button type="button" className="primary-button" disabled={isBusy} onClick={() => finishScan(existing?.id || saved?.id)}>{busy === "scanning" ? "扫描中…" : "重试扫描"}</button></section> :
      <section className="surface-card connection-editor">
        <div className="connection-tabs" role="tablist" aria-label="连接设置">{[["basic", "基本设置"], ["tls", "SSL / 证书"], ["advanced", "高级选项"]].map(([id, label]) => <button key={id} type="button" role="tab" aria-selected={tab === id} className={tab === id ? "is-active" : ""} onClick={() => setTab(id)}>{label}</button>)}</div>
        <fieldset disabled={isBusy} className="connection-editor__fieldset">
          {tab === "basic" ? <div className="connection-editor__layout"><div className="connection-editor__primary">
            <section className="connection-editor__section"><h2>连接信息</h2><div className="connection-editor__fields">{existing ? <div className="connection-editor__fixed"><strong>{existing.name}</strong><span>名称、驱动和工作区保持不变。请填写完整的新连接配置。</span></div> : <>{textInput("name", "连接名称", { placeholder: "例如：生产报表库" })}{textInput("workspace", "工作区")}</>}<Field label="部署方式"><select value={form.mode} onChange={(event) => update("mode", event.target.value)}><option value="local">本地</option><option value="cloud">云端</option><option value="managed">受管服务</option></select></Field></div></section>
            <section className="connection-editor__section"><h2>服务器</h2>{!SPECIAL_ENDPOINT.has(form.driver) && !HOST_ONLY.has(form.driver) ? <div className="connection-editor__segment" role="group" aria-label="连接方式"><button type="button" className={form.locatorMode === "host" ? "is-active" : ""} aria-pressed={form.locatorMode === "host"} onClick={() => update("locatorMode", "host")}>Host + Port</button><button type="button" className={form.locatorMode === "url" ? "is-active" : ""} aria-pressed={form.locatorMode === "url"} onClick={() => update("locatorMode", "url")}>URL</button></div> : null}
              <div className="connection-editor__fields">{form.driver === "sqlite" ? textInput("path", "服务器可访问的 SQLite 路径", { placeholder: "/data/example.db" }) : form.driver === "bigquery" ? <>{textInput("project", "Project")}{textInput("namespace", "Dataset")}</> : form.driver === "snowflake" ? <>{textInput("account", "Account")}{textInput("database", "Database")}{textInput("namespace", "Schema")}</> : <>
                {HOST_ONLY.has(form.driver) || form.locatorMode === "host" ? <div className="connection-editor__host">{textInput("host", "主机地址", { placeholder: "db.example.com" })}{textInput("port", "端口", { type: "number", placeholder: DEFAULT_PORTS[form.driver] || "" })}</div> : textInput("url", "连接 URL", { placeholder: "不含用户名与密码的连接地址" })}
                {form.driver === "cassandra" ? textInput("namespace", "Keyspace", { hint: "连接由 Qaneris 后端发起。" }) : form.driver === "influxdb" ? <>{textInput("org", "Organization")}{textInput("bucket", "Bucket")}</> : DATABASE_DRIVERS.has(form.driver) ? textInput("database", form.driver === "oracle" ? "Service Name" : "数据库", { hint: form.driver === "redis" ? "填写数字逻辑库编号，通常为 0；留空也使用 0。" : "连接由 Qaneris 后端发起；TLS 主机名须与证书匹配。" }) : null}
              </>}</div></section>
            <section className="connection-editor__section"><h2>身份认证</h2><div className="connection-editor__fields"><Field label="认证方式"><select value={form.auth} onChange={(event) => update("auth", event.target.value)}>{AUTH_OPTIONS.filter(([value]) => (DRIVER_AUTH[form.driver] || ["none"]).includes(value)).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field>
              {form.auth === "password" ? textInput("username", "用户名", { autoComplete: "username" }) : null}
              {MANAGED_TEXT_AUTH.has(form.auth) ? <><div className="connection-editor__segment" role="group" aria-label="凭据来源"><button type="button" className={form.source === "create" ? "is-active" : ""} aria-pressed={form.source === "create"} onClick={() => update("source", "create")}>新建受管凭据</button><button type="button" className={form.source === "existing" ? "is-active" : ""} aria-pressed={form.source === "existing"} onClick={() => update("source", "existing")}>已有 Secret ID</button></div>{form.source === "create" ? textInput("rawSecret", form.auth === "password" ? "密码" : "凭据值", { type: "password", autoComplete: "new-password", hint: "凭据由后端加密保存，不会写入连接地址。" }) : textInput("secretId", "Secret ID", { hint: "使用已保存在受管凭据库中的凭据。" })}</> : null}
              {["private_key", "service_account"].includes(form.auth) ? textInput("secretId", "已有 Secret ID") : null}
              {["none", "cloud_identity"].includes(form.auth) ? <p className="connection-editor__hint">当前认证方式不需要单独输入凭据。</p> : null}
              {form.driver === "qdrant" && form.auth === "api_key" && !form.tls ? <label className="connection-editor__hint"><input type="checkbox" checked={form.allowInsecureKeyHttp} onChange={(event) => update("allowInsecureKeyHttp", event.target.checked)} /> 我确认在未加密的 HTTP 连接中发送 API Key</label> : null}
            </div></section></div><aside className="connection-editor__aside"><span className="eyebrow">连接路径</span><h3>从后端验证连接</h3><p>Qaneris 后端直接连接数据库。浏览器不会接收数据库密码。</p><div className="connection-editor__flow"><span>浏览器</span><i>→</i><span>后端</span><i>→</i><span>{driverName(form.driver)}</span></div><p>如服务器要求证书，请切换到“SSL / 证书”上传 CA、客户端证书和私钥。</p></aside></div> : null}
          {tab === "tls" ? <div className="connection-editor__layout"><div className="connection-editor__primary">{capability ? <><div className="connection-editor__tls-toggle"><div><h2>启用加密连接</h2><p>验证服务器证书和主机名，避免连接到错误的服务器。</p></div><label className="connection-switch"><input type="checkbox" checked={form.tls} onChange={(event) => update("tls", event.target.checked)} /><span /></label></div>{form.tls ? <>
            {capability.custom_ca ? <section className="connection-editor__section"><h2>服务器证书</h2><p className="connection-editor__hint">上传云服务商提供的 CA 证书。使用系统信任链时可留空。</p>{fileInput("caFile", "CA 证书", secretSummary(form.caId, form.caFile), ".pem,.crt,.cer")}</section> : null}
            {capability.mtls ? <section className="connection-editor__section"><h2>客户端身份 <small>可选</small></h2><p className="connection-editor__hint">仅在数据库要求双向 TLS 时填写；客户端证书和私钥必须成对上传。</p>{fileInput("certFile", "客户端证书", secretSummary(form.certId, form.certFile), ".pem,.crt,.cer")}{fileInput("keyFile", "客户端私钥", secretSummary(form.keyId, form.keyFile), ".pem,.key")}{textInput("keyPassword", "私钥解密密码", { type: "password", placeholder: "仅在私钥已加密时填写", autoComplete: "new-password" })}</section> : null}
            <section className="connection-editor__section"><div className="connection-editor__tls-options">{capability.tls13_control ? <Field label="最低 TLS 版本"><select value={form.tlsVersion} onChange={(event) => update("tlsVersion", event.target.value)}><option value="1.2">TLS 1.2</option><option value="1.3">TLS 1.3</option></select></Field> : null}<Field label="服务器身份验证">{form.driver === "sqlserver" ? <select value={form.verifyServer ? "strict" : "trust"} onChange={(event) => update("verifyServer", event.target.value === "strict")}><option value="strict">严格验证（推荐）</option><option value="trust">信任自签名证书</option></select> : <span className="connection-editor__readonly">严格验证</span>}</Field>{capability.server_name_override && form.verifyServer ? textInput("serverName", "证书服务器名称") : null}</div>{form.driver === "sqlserver" && !form.verifyServer ? <p className="connection-editor__hint">连接仍会加密，但不验证服务器身份。仅在确认服务器地址可信时使用。</p> : null}<details className="connection-editor__existing"><summary>使用已有证书 Secret ID</summary>{capability.custom_ca ? textInput("caId", "CA Secret ID") : null}{capability.mtls ? <>{textInput("certId", "客户端证书 Secret ID")}{textInput("keyId", "客户端私钥 Secret ID")}</> : null}</details></section>
          </> : <p className="connection-editor__hint">打开后可配置该驱动支持的证书和 TLS 选项。</p>}</> : <div className="connection-editor__unsupported">当前驱动没有受管 TLS 配置。请检查驱动能力后选择其他连接方式。</div>}</div><aside className="connection-editor__aside"><span className="eyebrow">证书安全</span><h3>证书由后端保存</h3><p>上传文件进入受管凭据库。连接测试时临时读取，不向浏览器返回私钥内容。</p><p>上传前需配置凭据存储目录和主密钥；连接域名应与服务器证书匹配。</p></aside></div> : null}
          {tab === "advanced" ? <div className="connection-editor__layout"><div className="connection-editor__primary"><section className="connection-editor__section"><h2>其他连接字段</h2><div className="connection-editor__fields">{!["bigquery", "snowflake", "cassandra"].includes(form.driver) ? <>{textInput("namespace", "Namespace / Schema")}{textInput("project", "Project")}{textInput("account", "Account")}</> : <p className="connection-editor__hint">此驱动的必要字段已显示在“基本设置”。</p>}</div></section><section className="connection-editor__section"><h2>驱动选项</h2><Field label="高级选项 JSON" hint="仅在数据库驱动需要额外参数时填写。"><textarea rows={7} spellCheck={false} value={form.options} onChange={(event) => update("options", event.target.value)} /></Field></section></div><aside className="connection-editor__aside"><span className="eyebrow">高级设置</span><h3>保持配置可检查</h3><p>额外参数会随连接配置保存。连接测试成功后才能保存并扫描数据源。</p></aside></div> : null}
        </fieldset>
      </section>}

    {!isSaved && error ? <div className="connection-feedback connection-feedback--error" role="alert"><strong>连接未通过</strong><p>{error.message}</p>{error.code === "credential_store_configuration_error" ? <small>请先在后端配置 QANERIS_SECRET_STORE_DIR 和 QANERIS_MASTER_KEY，然后重启后端。</small> : null}</div> : null}
    {!isSaved && testResult ? <div className="connection-feedback connection-feedback--success" role="status"><strong>连接已验证</strong><p>{driverName(testResult.driver || form.driver)} · TLS {testResult.tls_enabled ? "已启用" : "未启用"}。可以保存并扫描。</p></div> : null}
    <footer className="connection-flow__footer"><p className={error ? "connection-flow__footer-error" : ""} role={error ? "alert" : undefined}>{error ? `操作未完成：${error.message}` : isSaved ? "数据源已保存；扫描失败时可在此重试。" : validatedProfile ? "连接已验证 · 修改任一设置后需重新测试" : "未测试 · 保存前需要验证连接"}</p><div><button type="button" className="connection-flow__button" disabled={isBusy} onClick={cancel}>取消</button>{!isSaved ? <><button type="button" className="connection-flow__button" disabled={isBusy || !form.driver} onClick={runTest}>{busy === "testing" ? "测试中…" : "测试连接"}</button><button type="button" className="primary-button" disabled={isBusy || !validatedProfile} onClick={saveAndScan}>{busy === "saving" ? "保存中…" : busy === "scanning" ? "扫描中…" : existing ? "更新并扫描" : "保存并扫描"}</button></> : null}</div></footer>
  </div>;
}
