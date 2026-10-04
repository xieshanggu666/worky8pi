views.CalculationView = () => {
  const [companies, setCompanies] = React.useState([]);
  const [companyId, setCompanyId] = React.useState("");
  const [year, setYear] = React.useState(2025);
  const [results, setResults] = React.useState(null);
  const [loading, setLoading] = React.useState(false);
  const [msg, setMsg] = React.useState({ type: "", text: "" });

  React.useEffect(() => {
    api.get("/api/companies").then(setCompanies).catch((e) => setMsg({ type: "err", text: e.message }));
  }, []);

  const run = async () => {
    if (!companyId) { setMsg({ type: "err", text: "请先选择企业" }); return; }
    setLoading(true);
    setMsg({ type: "", text: "" });
    try {
      const r = await api.post(`/api/companies/${companyId}/calculate?year=${year}`);
      setMsg({
        type: r.unverified_count ? "warn" : "ok",
        text: `核算完成：共 ${r.count} 条已核验活动数据，年度排放 ${fmtNum(r.total)} tCO2e`
          + (r.warning ? `。${r.warning}` : ""),
      });
      const res = await api.get(`/api/companies/${companyId}/results?year=${year}`);
      setResults({ list: res, totals: r.totals, total: r.total });
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    } finally {
      setLoading(false);
    }
  };

  const selected = companies.find((c) => c.id === Number(companyId));

  return html`
    <div class="panel">
      <h3>执行排放核算</h3>
      <div class="filter-bar">
        <div class="field"><label>控排企业</label>
          <select value=${companyId} onChange=${(e) => setCompanyId(e.target.value)}>
            <option value="">请选择企业</option>
            ${companies.map((c) => html`<option key=${c.id} value=${c.id}>${c.name}</option>`)}
          </select>
        </div>
        <div class="field"><label>核算年度</label>
          <select value=${year} onChange=${(e) => setYear(Number(e.target.value))}>
            ${[2023, 2024, 2025, 2026].map((y) => html`<option value=${y}>${y}</option>`)}
          </select>
        </div>
        <button class="btn" onClick=${run} disabled=${loading}>${loading ? "核算中..." : "开始核算"}</button>
      </div>
      ${msg.text && html`<div class="msg ${msg.type}">${msg.text}</div>`}
    </div>

    ${results && html`
      <div class="cards">
        ${Object.entries(results.totals).map(([s, v]) => html`
          <div class="card" key=${s}><div class="label">${scopeLabel(s)}排放</div><div class="value">${fmtNum(v)} tCO2e</div></div>`)}
        <div class="card"><div class="label">年度合计（${selected ? selected.name : ""} ${year}）</div><div class="value">${fmtNum(results.total)} tCO2e</div></div>
      </div>
      <div class="panel">
        <h3>核算明细（${results.list.length} 条）</h3>
        <table>
          <thead><tr><th>ID</th><th>活动量</th><th>因子值</th><th>方法</th><th>排放量 (tCO2e)</th></tr></thead>
          <tbody>
            ${results.list.map((r) => html`
              <tr key=${r.id}>
                <td class="mono">#${r.id}</td>
                <td>${fmtNum(r.activity_quantity)}</td>
                <td>${r.factor_value}</td>
                <td class="mono">${r.method_code || "activity_factor"}</td>
                <td style=${{fontWeight: "600"}}>${fmtNum(r.emission_amount)}</td>
              </tr>`)}
          </tbody>
        </table>
      </div>`}
  `;
};
