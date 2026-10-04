views.ActivityView = () => {
  const [list, setList] = React.useState([]);
  const [companies, setCompanies] = React.useState([]);
  const [filterCompany, setFilterCompany] = React.useState("");
  const [filterYear, setFilterYear] = React.useState("");
  const [form, setForm] = React.useState({ scope_id: "", year: 2025, period: "monthly", activity_type: "", unit: "", quantity: "", data_source: "" });
  const [scopes, setScopes] = React.useState([]);
  const [msg, setMsg] = React.useState({ type: "", text: "" });
  const [selected, setSelected] = React.useState(new Set());
  const [busy, setBusy] = React.useState(false);

  const load = () => {
    const params = new URLSearchParams();
    if (filterCompany) params.set("company_id", filterCompany);
    if (filterYear) params.set("year", filterYear);
    const qs = params.toString();
    api.get(`/api/activity${qs ? "?" + qs : ""}`).then((rows) => {
      setList(rows);
      setSelected(new Set());
    }).catch((e) => setMsg({ type: "err", text: e.message }));
  };
  React.useEffect(() => { load(); }, [filterCompany, filterYear]);

  React.useEffect(() => {
    api.get("/api/companies").then(setCompanies).catch(() => {});
  }, []);

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  const pickCompany = async (companyId) => {
    if (!companyId) { setScopes([]); return; }
    const c = await api.get(`/api/companies/${companyId}`).catch(() => null);
    setScopes((c && c.scopes) || []);
  };

  const create = async (e) => {
    e.preventDefault();
    setMsg({ type: "", text: "" });
    try {
      await api.post("/api/activity", { ...form, quantity: Number(form.quantity) });
      setForm({ scope_id: "", year: 2025, period: "monthly", activity_type: "", unit: "", quantity: "", data_source: "" });
      setMsg({ type: "ok", text: "活动数据已登记" });
      load();
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    }
  };

  const isVerifier = window.__user.role === "verifier" || window.__user.role === "admin";

  const pendingRows = list.filter((a) => !a.verified);
  const pendingIds = pendingRows.map((a) => a.id);

  const toggle = (id) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    setSelected((prev) => (prev.size === pendingIds.length ? new Set() : new Set(pendingIds)));
  };

  const summarize = (res) => {
    const lines = [];
    if (res.verified_count) {
      const years = (res.affected || []).map((a) => `${a.company_name}${a.year}年`).join("、");
      lines.push(`已批量核验 ${res.verified_count} 条（${years}）`);
    }
    (res.recalculated || []).forEach((r) => lines.push(`${r.year}年重算 ${r.result_count} 条排放结果`));
    const reportLines = (res.reports || [])
      .filter((r) => r.action === "updated" || r.action === "reset_submitted" || r.action === "created")
      .map((r) => (r.action === "reset_submitted"
        ? `${r.year}年已提交报告快照过期，已退回草稿`
        : `${r.year}年 MRV 草稿已联动刷新`));
    lines.push(...new Set(reportLines));
    (res.warnings || []).forEach((w) => lines.push(`⚠ ${w}`));
    return lines.length ? lines.join("；") : "没有符合条件的待核验数据";
  };

  const runBatch = async (payload) => {
    setBusy(true);
    setMsg({ type: "", text: "" });
    try {
      const res = await api.post("/api/activity/batch-verify", payload, api.idemKey());
      setMsg({ type: res.warnings && res.warnings.length ? "warn" : "ok", text: summarize(res) });
      load();
    } catch (err) {
      setMsg({ type: "err", text: err.message });
      setBusy(false);
      return;
    }
    setBusy(false);
  };

  const verifySelected = () => {
    if (!selected.size) return;
    runBatch({ activity_ids: Array.from(selected), recalculate: true });
  };

  const verifyAllFiltered = () => {
    if (!pendingIds.length) return;
    if (!filterYear) {
      setMsg({ type: "warn", text: "一键核验全部前请先按年度筛选，避免跨年度混批" });
      return;
    }
    const payload = { year: Number(filterYear), recalculate: true };
    if (filterCompany) payload.company_id = Number(filterCompany);
    runBatch(payload);
  };

  return html`
    <div class="panel">
      <h3>录入活动数据</h3>
      <form class="form-grid" onSubmit=${create}>
        <div class="field"><label>控排企业</label>
          <select value=${form.companySel || ""} onChange=${(e) => pickCompany(e.target.value)} required>
            <option value="">请选择企业</option>
            ${companies.map((c) => html`<option key=${c.id} value=${c.id}>${c.name}</option>`)}
          </select>
        </div>
        <div class="field"><label>核算边界</label>
          <select value=${form.scope_id} onChange=${set("scope_id")} required>
            <option value="">请选择边界</option>
            ${scopes.map((s) => html`<option key=${s.id} value=${s.id}>${scopeLabel(s.scope)} - ${s.name}</option>`)}
          </select>
        </div>
        <div class="field"><label>年度</label>
          <select value=${form.year} onChange=${set("year")}>
            ${[2023, 2024, 2025, 2026].map((y) => html`<option value=${y}>${y}</option>`)}
          </select>
        </div>
        <div class="field"><label>统计周期</label>
          <select value=${form.period} onChange=${set("period")}>
            <option value="monthly">月度</option><option value="quarterly">季度</option><option value="annual">年度</option>
          </select>
        </div>
        <div class="field"><label>活动类型（对应因子名称）</label>
          <input value=${form.activity_type} onChange=${set("activity_type")} placeholder="如：外购电力 / 燃煤消耗" required />
        </div>
        <div class="field"><label>单位</label>
          <input value=${form.unit} onChange=${set("unit")} placeholder="MWh / t / 万m³" required />
        </div>
        <div class="field"><label>活动量</label>
          <input type="number" step="0.0001" value=${form.quantity} onChange=${set("quantity")} required />
        </div>
        <div class="field"><label>数据来源</label>
          <input value=${form.data_source} onChange=${set("data_source")} placeholder="计量表 / 发票 / 台账" />
        </div>
        <div class="actions"><button class="btn" type="submit">登记</button></div>
      </form>
      ${msg.text && html`<div class="msg ${msg.type}">${msg.text}</div>`}
    </div>

    <div class="panel">
      <h3>活动数据台账</h3>
      <div class="filter-bar">
        <div class="field"><label>按企业</label>
          <select value=${filterCompany} onChange=${(e) => setFilterCompany(e.target.value)}>
            <option value="">全部</option>
            ${companies.map((c) => html`<option key=${c.id} value=${c.id}>${c.name}</option>`)}
          </select>
        </div>
        <div class="field"><label>按年度</label>
          <select value=${filterYear} onChange=${(e) => setFilterYear(e.target.value)}>
            <option value="">全部</option>
            ${[2023, 2024, 2025, 2026].map((y) => html`<option value=${y}>${y}</option>`)}
          </select>
        </div>
        ${isVerifier && html`
          <div class="actions" style=${{marginLeft: "auto"}}>
            <button class="btn" onClick=${verifySelected} disabled=${busy || !selected.size}>
              批量核验选中${selected.size ? `（${selected.size}）` : ""}
            </button>
            <button class="btn" onClick=${verifyAllFiltered} disabled=${busy || !pendingIds.length}>
              一键核验本页待核验${pendingIds.length ? `（${pendingIds.length}）` : ""}
            </button>
          </div>`}
      </div>
      <table>
        <thead><tr>
          ${isVerifier && html`<th style=${{width: 36}}>
            <input type="checkbox" checked=${pendingIds.length > 0 && selected.size === pendingIds.length}
              onChange=${toggleAll} disabled=${!pendingIds.length} title="全选待核验" />
          </th>`}
          <th>ID</th><th>企业</th><th>边界</th><th>年度</th><th>类型</th><th>活动量</th><th>单位</th><th>核验</th>
        </tr></thead>
        <tbody>
          ${list.map((a) => html`
            <tr key=${a.id}>
              ${isVerifier && html`<td>${!a.verified && html`
                <input type="checkbox" checked=${selected.has(a.id)} onChange=${() => toggle(a.id)} />`}</td>`}
              <td class="mono">#${a.id}</td>
              <td>${companies.find((c) => c.id === a.company_id)?.name || a.company_id}</td>
              <td>${a.scope_no ? scopeLabel(a.scope_no) : "-"}</td>
              <td>${a.year}</td>
              <td>${a.activity_type}</td>
              <td>${fmtNum(a.quantity)}</td>
              <td>${a.unit}</td>
              <td>${a.verified ? html`<span class="badge ok">已核验</span>` : html`<span class="badge warn">待核验</span>`}</td>
            </tr>`)}
          ${list.length === 0 && html`<tr><td colspan="${isVerifier ? 9 : 8}" class="empty">暂无活动数据</td></tr>`}
        </tbody>
      </table>
      <p class="sub" style=${{color: "var(--text-dim)"}}>
        批量核验在同一事务内完成“核验 → 年度重算 → MRV 草稿刷新”，任一失败整批回滚；
        已批准年度需先冲正报告，已提交报告快照过期会自动退回草稿。
      </p>
    </div>
  `;
};
