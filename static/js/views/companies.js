views.CompaniesView = () => {
  const [list, setList] = React.useState([]);
  const [showForm, setShowForm] = React.useState(false);
  const [form, setForm] = React.useState({ code: "", name: "", industry: "", region: "", boundary_desc: "" });
  const [msg, setMsg] = React.useState({ type: "", text: "" });

  const load = () => api.get("/api/companies").then(setList);
  React.useEffect(() => { load(); }, []);

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  const create = async (e) => {
    e.preventDefault();
    try {
      await api.post("/api/companies", form);
      setForm({ code: "", name: "", industry: "", region: "", boundary_desc: "" });
      setShowForm(false);
      setMsg({ type: "ok", text: "企业创建成功" });
      load();
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    }
  };

  return html`
    <div class="panel">
      <div style=${{display: "flex", justifyContent: "space-between", alignItems: "center"}}>
        <h3>控排企业</h3>
        ${window.__user.role === "admin" && html`
          <button class="btn" onClick=${() => setShowForm(!showForm)}>${showForm ? "收起" : "新建企业"}</button>`}
      </div>
      ${msg.text && html`<div class="msg ${msg.type}">${msg.text}</div>`}
      ${showForm && html`
        <form class="form-grid" onSubmit=${create} style=${{marginTop: "12px"}}>
          <div class="field"><label>企业编号</label><input value=${form.code} onChange=${set("code")} required /></div>
          <div class="field"><label>企业名称</label><input value=${form.name} onChange=${set("name")} required /></div>
          <div class="field"><label>行业</label><input value=${form.industry} onChange=${set("industry")} placeholder="电力/钢铁/水泥/化工" /></div>
          <div class="field"><label>地区</label><input value=${form.region} onChange=${set("region")} /></div>
          <div class="field" style=${{gridColumn: "1/-1"}}><label>核算边界说明</label>
            <textarea value=${form.boundary_desc} onChange=${set("boundary_desc")} /></div>
          <div class="actions"><button class="btn" type="submit">保存</button></div>
        </form>`}
      <table style=${{marginTop: "14px"}}>
        <thead><tr><th>编号</th><th>名称</th><th>行业</th><th>地区</th><th>状态</th><th></th></tr></thead>
        <tbody>
          ${list.map((c) => html`
            <tr key=${c.id}>
              <td class="mono">${c.code}</td>
              <td>${c.name}</td>
              <td>${c.industry || "-"}</td>
              <td>${c.region || "-"}</td>
              <td>${html([StatusBadge(c.status)])}</td>
              <td><button class="btn ghost sm" onClick=${() => (location.hash = `#/companies/${c.id}`)}>详情</button></td>
            </tr>`)}
          ${list.length === 0 && html`<tr><td colspan="6" class="empty">暂无企业</td></tr>`}
        </tbody>
      </table>
    </div>
  `;
};

views.CompanyDetailView = ({ id }) => {
  const [company, setCompany] = React.useState(null);
  const [year, setYear] = React.useState(2025);
  const [totals, setTotals] = React.useState(null);
  const [msg, setMsg] = React.useState({ type: "", text: "" });

  React.useEffect(() => {
    api.get(`/api/companies/${id}`).then(setCompany).catch((e) => setMsg({ type: "err", text: e.message }));
    api.get(`/api/companies/${id}/totals?year=${year}`).then(setTotals).catch(() => setTotals(null));
  }, [id, year]);

  if (!company) return html`<div class="msg err">${msg.text || "加载中..."}</div>`;

  const scopes = company.scopes || [];
  const scope1 = scopes.filter((s) => s.scope === "1").length;
  const scope2 = scopes.filter((s) => s.scope === "2").length;
  const scope3 = scopes.filter((s) => s.scope === "3").length;

  return html`
    <div class="panel">
      <h3>${company.name} <span class="mono" style=${{color: "var(--text-dim)", fontWeight: "400"}}>（${company.code}）</span></h3>
      <p style=${{color: "var(--text-dim)", lineHeight: "1.9", marginBottom: "10px"}}>
        行业：${company.industry || "-"} ｜ 地区：${company.region || "-"} ｜ 状态：${html([StatusBadge(company.status)])}
      </p>
      ${company.boundary_desc && html`<p style=${{color: "var(--text-dim)", lineHeight: "1.8"}}>核算边界：${company.boundary_desc}</p>`}
    </div>
    <div class="cards">
      <div class="card"><div class="label">范围一边界</div><div class="value">${scope1}</div><div class="sub">直接排放/燃料燃烧</div></div>
      <div class="card"><div class="label">范围二边界</div><div class="value">${scope2}</div><div class="sub">外购电力/热力</div></div>
      <div class="card"><div class="label">范围三边界</div><div class="value">${scope3}</div><div class="sub">上下游间接排放</div></div>
    </div>
    <div class="panel">
      <h3>年度排放核算</h3>
      <div class="filter-bar">
        <div class="field"><label>核算年度</label>
          <select value=${year} onChange=${(e) => setYear(Number(e.target.value))}>
            ${[2023, 2024, 2025, 2026].map((y) => html`<option value=${y}>${y} 年</option>`)}
          </select>
        </div>
      </div>
      ${totals ? html`
        <table>
          <thead><tr><th>核算边界</th><th>排放量 (tCO2e)</th></tr></thead>
          <tbody>
            ${Object.entries(totals.scopes).map(([s, v]) => html`
              <tr key=${s}><td>${scopeLabel(s)}</td><td>${fmtNum(v)}</td></tr>`)}
            <tr><td style=${{fontWeight: "700"}}>年度合计</td><td style=${{fontWeight: "700"}}>${fmtNum(totals.total)}</td></tr>
          </tbody>
        </table>` : html`<div class="empty">该年度暂无核算数据，请先录入活动数据并执行核算</div>`}
      ${msg.text && html`<div class="msg ${msg.type}">${msg.text}</div>`}
    </div>
  `;
};
