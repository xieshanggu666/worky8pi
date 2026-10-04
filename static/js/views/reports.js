views.ReportsView = () => {
  const [companies, setCompanies] = React.useState([]);
  const [selCompany, setSelCompany] = React.useState("");
  const [reports, setReports] = React.useState([]);
  const [detail, setDetail] = React.useState(null);
  const [msg, setMsg] = React.useState({ type: "", text: "" });

  const isVerifier = window.__user.role === "verifier" || window.__user.role === "admin";
  const canSubmit = window.__user.role === "enterprise" || window.__user.role === "admin";

  React.useEffect(() => {
    api.get("/api/companies").then(setCompanies).catch(() => {});
  }, []);

  const loadReports = (companyId) => {
    if (!companyId) { setReports([]); return; }
    api.get(`/api/companies/${companyId}/reports`).then(setReports).catch((e) => setMsg({ type: "err", text: e.message }));
  };
  React.useEffect(() => { loadReports(selCompany); }, [selCompany]);

  const generate = async () => {
    if (!selCompany) { setMsg({ type: "err", text: "请先选择企业" }); return; }
    const year = prompt("请输入报告年度（如 2025）：", "2025");
    if (!year) return;
    try {
      const r = await api.post(`/api/companies/${selCompany}/reports/generate?year=${Number(year)}`);
      setMsg({ type: "ok", text: `报告已生成：年度排放 ${fmtNum(r.total_emission)} tCO2e（状态：${r.status}）` });
      loadReports(selCompany);
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    }
  };

  const action = async (id, act) => {
    try {
      const r = await api.post(`/api/reports/${id}/${act}`);
      setMsg({ type: "ok", text: `操作成功，当前状态：${r.status}` });
      loadReports(selCompany);
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    }
  };

  const reverse = async (id) => {
    const reason = prompt("请输入批准报告冲正原因（将解冻/退还相关配额）：", "");
    if (reason === null) return;
    if (reason.trim().length < 2) {
      setMsg({ type: "err", text: "冲正原因至少 2 个字符" });
      return;
    }
    if (!confirm("冲正将归档履约记录并回滚冻结、清缴结果，确认继续？")) return;
    try {
      const r = await api.post(`/api/reports/${id}/reverse`, { reason });
      setMsg({ type: "err", text: "" });
      setMsg({ type: "ok", text: `报告已冲正：${r.status}` });
      loadReports(selCompany);
      setDetail(null);
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    }
  };

  const showDetail = async (id) => {
    try {
      const d = await api.get(`/api/reports/${id}`);
      setDetail(d);
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    }
  };

  return html`
    <div class="panel">
      <h3>MRV 报告</h3>
      <div class="filter-bar">
        <div class="field"><label>企业</label>
          <select value=${selCompany} onChange=${(e) => setSelCompany(e.target.value)}>
            <option value="">请选择企业</option>
            ${companies.map((c) => html`<option key=${c.id} value=${c.id}>${c.name}</option>`)}
          </select>
        </div>
        <button class="btn" onClick=${generate}>生成报告</button>
      </div>
      ${msg.text && html`<div class="msg ${msg.type}">${msg.text}</div>`}
      <table>
        <thead><tr><th>年度</th><th>范围一</th><th>范围二</th><th>范围三</th><th>排放合计 (tCO2e)</th><th>状态</th><th>生成时间</th><th></th></tr></thead>
        <tbody>
          ${reports.map((r) => html`
            <tr key=${r.id}>
              <td>${r.year}</td>
              <td>${fmtNum(r.scope1)}</td>
              <td>${fmtNum(r.scope2)}</td>
              <td>${fmtNum(r.scope3)}</td>
              <td style=${{fontWeight: "600"}}>${fmtNum(r.total_emission)}</td>
              <td>${html([StatusBadge(r.status)])}</td>
              <td>${new Date(r.generated_at).toLocaleDateString("zh-CN")}</td>
              <td style=${{whiteSpace: "nowrap"}}>
                <button class="btn ghost sm" onClick=${() => showDetail(r.id)}>详情</button>
                ${r.status === "draft" && canSubmit && html`<button class="btn ghost sm" onClick=${() => action(r.id, "submit")}>提交</button>`}
                ${r.status === "submitted" && isVerifier && html`<button class="btn sm" onClick=${() => action(r.id, "approve")}>批准</button>`}
                ${r.status === "approved" && isVerifier && html`<button class="btn danger sm" onClick=${() => reverse(r.id)}>冲正</button>`}
              </td>
            </tr>`)}
          ${reports.length === 0 && html`<tr><td colspan="8" class="empty">暂无报告，选择企业后生成</td></tr>`}
        </tbody>
      </table>
    </div>

    ${detail && html`
      <div class="panel">
        <h3>报告明细（${companies.find((c) => c.id === detail.company_id)?.name || ""} ${detail.year} 年）</h3>
        <div class="cards">
          <div class="card"><div class="label">范围一</div><div class="value">${fmtNum(detail.scope1)} tCO2e</div></div>
          <div class="card"><div class="label">范围二</div><div class="value">${fmtNum(detail.scope2)} tCO2e</div></div>
          <div class="card"><div class="label">范围三</div><div class="value">${fmtNum(detail.scope3)} tCO2e</div></div>
          <div class="card"><div class="label">排放合计</div><div class="value">${fmtNum(detail.total_emission)} tCO2e</div></div>
        </div>
        <p style=${{color: "var(--text-dim)", fontSize: "12px"}}>
          状态：${detail.status} ｜ 生成：${new Date(detail.generated_at).toLocaleString("zh-CN")}
          ${detail.approved_at ? ` ｜ 批准：${new Date(detail.approved_at).toLocaleString("zh-CN")}` : ""}
          ${detail.reversed_at ? ` ｜ 冲正：${new Date(detail.reversed_at).toLocaleString("zh-CN")}` : ""}
        </p>
        ${detail.reversal_reason && html`<p style=${{color: "var(--red)", fontSize: "12px"}}>冲正原因：${detail.reversal_reason}</p>`}
        <pre style=${{background: "var(--bg-soft)", border: "1px solid var(--line)", borderRadius: "8px", padding: "14px", marginTop: "12px", overflow: "auto", fontSize: "12px"}}>${JSON.stringify(JSON.parse(detail.report_json || "{}"), null, 2)}</pre>
      </div>`}
  `;
};
