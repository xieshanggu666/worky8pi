views.FactorsView = () => {
  const [list, setList] = React.useState([]);
  const [showForm, setShowForm] = React.useState(false);
  const [form, setForm] = React.useState({ factor_code: "", name: "", scope: "1", unit: "tCO2/单位", value: "", source: "", valid_from: "", valid_to: "" });
  const [msg, setMsg] = React.useState({ type: "", text: "" });

  const load = () => api.get("/api/factors").then(setList);
  React.useEffect(() => { load(); }, []);

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  const save = async (e) => {
    e.preventDefault();
    try {
      await api.post("/api/factors", { ...form, value: Number(form.value) });
      setForm({ factor_code: "", name: "", scope: "1", unit: "tCO2/单位", value: "", source: "", valid_from: "", valid_to: "" });
      setShowForm(false);
      setMsg({ type: "ok", text: "排放因子已创建" });
      load();
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    }
  };

  const revise = async (f) => {
    const v = prompt(`输入「${f.name}」的新因子值：`, f.value);
    if (v === null) return;
    try {
      await api.put(`/api/factors/${f.id}`, { ...f, value: Number(v) });
      setMsg({ type: "ok", text: "因子已修订并记录版本" });
      load();
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    }
  };

  const isAdmin = window.__user.role === "admin";

  return html`
    <div class="panel">
      <div style=${{display: "flex", justifyContent: "space-between", alignItems: "center"}}>
        <h3>排放因子库</h3>
        ${isAdmin && html`<button class="btn" onClick=${() => setShowForm(!showForm)}>${showForm ? "收起" : "新增因子"}</button>`}
      </div>
      ${msg.text && html`<div class="msg ${msg.type}">${msg.text}</div>`}
      ${showForm && html`
        <form class="form-grid" onSubmit=${save} style=${{marginTop: "12px"}}>
          <div class="field"><label>因子编号</label><input value=${form.factor_code} onChange=${set("factor_code")} placeholder="如 ELEC-GRID" required /></div>
          <div class="field"><label>因子名称（与活动类型对应）</label><input value=${form.name} onChange=${set("name")} placeholder="如 外购电力" required /></div>
          <div class="field"><label>范围</label>
            <select value=${form.scope} onChange=${set("scope")}>
              <option value="1">范围一</option><option value="2">范围二</option><option value="3">范围三</option>
            </select>
          </div>
          <div class="field"><label>因子单位</label><input value=${form.unit} onChange=${set("unit")} /></div>
          <div class="field"><label>因子值</label><input type="number" step="0.000001" value=${form.value} onChange=${set("value")} required /></div>
          <div class="field"><label>数据来源</label><input value=${form.source} onChange=${set("source")} placeholder="方法学 / 标准" /></div>
          <div class="field"><label>生效日期</label><input value=${form.valid_from} onChange=${set("valid_from")} placeholder="YYYY-MM-DD" /></div>
          <div class="field"><label>失效日期（留空长期有效）</label><input value=${form.valid_to} onChange=${set("valid_to")} placeholder="YYYY-MM-DD" /></div>
          <div class="actions"><button class="btn" type="submit">保存</button></div>
        </form>`}
      <table style=${{marginTop: "14px"}}>
        <thead><tr><th>编号</th><th>名称</th><th>范围</th><th>因子值</th><th>单位</th><th>生效期</th><th>数据来源</th><th></th></tr></thead>
        <tbody>
          ${list.map((f) => html`
            <tr key=${f.id}>
              <td class="mono">${f.factor_code}</td>
              <td>${f.name}</td>
              <td>${scopeLabel(f.scope)}</td>
              <td style=${{fontWeight: "600"}}>${f.value}</td>
              <td>${f.unit}</td>
              <td>${f.valid_from}${f.valid_to ? " ~ " + f.valid_to : " ~"}</td>
              <td>${f.source || "-"}</td>
              <td>${isAdmin && html`<button class="btn ghost sm" onClick=${() => revise(f)}>修订</button>`}</td>
            </tr>`)}
          ${list.length === 0 && html`<tr><td colspan="8" class="empty">暂无排放因子</td></tr>`}
        </tbody>
      </table>
    </div>
  `;
};
