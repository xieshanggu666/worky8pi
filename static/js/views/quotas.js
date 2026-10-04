views.QuotaView = () => {
  const [companies, setCompanies] = React.useState([]);
  const [quotas, setQuotas] = React.useState([]);
  const [compliance, setCompliance] = React.useState([]);
  const [account, setAccount] = React.useState(null);
  const [txs, setTxs] = React.useState([]);
  const [selCompany, setSelCompany] = React.useState("");
  const [selYear, setSelYear] = React.useState(2025);
  const [form, setForm] = React.useState({ company_id: "", year: 2025, baseline: "", allocation_amount: "", adjustment: "" });
  const [txForm, setTxForm] = React.useState({ amount: "", tx_type: "sell", counterparty: "", price: "", tx_date: "", remark: "" });
  const [msg, setMsg] = React.useState({ type: "", text: "" });
  // 提交中状态：禁用按钮，防止双击造成重复提交（服务端另有幂等键兜底）
  const [submitting, setSubmitting] = React.useState(false);

  const isAdmin = window.__user.role === "admin";

  React.useEffect(() => {
    api.get("/api/companies").then(setCompanies).catch(() => {});
    api.get("/api/quotas").then(setQuotas).catch(() => {});
    api.get("/api/compliance").then(setCompliance).catch(() => {});
  }, []);

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  const setTx = (k) => (e) => setTxForm({ ...txForm, [k]: e.target.value });

  const loadAccount = async () => {
    if (!selCompany) return;
    setMsg({ type: "", text: "" });
    try {
      const acc = await api.get(`/api/companies/${selCompany}/account?year=${selYear}`);
      setAccount(acc);
      const t = await api.get(`/api/accounts/${acc.id}/transactions`);
      setTxs(t);
    } catch (err) {
      setAccount(null);
      setTxs([]);
      setMsg({ type: "err", text: err.message });
    }
  };
  React.useEffect(() => { loadAccount(); }, [selCompany, selYear]);

  const allocate = async (e) => {
    e.preventDefault();
    try {
      const r = await api.post("/api/quotas", {
        company_id: Number(form.company_id),
        year: Number(form.year),
        baseline: Number(form.baseline || 0),
        allocation_amount: Number(form.allocation_amount),
        adjustment: Number(form.adjustment || 0),
      });
      setMsg({ type: "ok", text: `配额已分配（${fmtNum(r.total)} 吨）` });
      api.get("/api/quotas").then(setQuotas);
      loadAccount();
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    }
  };

  const doTransfer = async (e) => {
    e.preventDefault();
    if (!account) { setMsg({ type: "err", text: "请先加载配额账户" }); return; }
    if (submitting) return;
    setSubmitting(true);
    try {
      // 每次提交生成一个幂等键：双击/网络重试只入账一次
      const r = await api.post(`/api/accounts/${account.id}/transfer`, {
        amount: Number(txForm.amount),
        tx_type: txForm.tx_type,
        counterparty: txForm.counterparty,
        price: txForm.price ? Number(txForm.price) : null,
        tx_date: txForm.tx_date,
        remark: txForm.remark,
      }, api.idemKey());
      setMsg({ type: "ok", text: `交易成功：${txLabel[r.tx_type]} ${fmtNum(r.amount)} 吨，余额 ${fmtNum(r.balance_after)}` });
      setTxForm({ amount: "", tx_type: "sell", counterparty: "", price: "", tx_date: "", remark: "" });
      loadAccount();
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    } finally {
      setSubmitting(false);
    }
  };

  const doClear = async (c) => {
    if (!confirm(`确认对 ${c.name}（${c.year} 年）执行履约清缴？`)) return;
    try {
      // 清缴幂等键：重复点击/超时重发返回同一履约记录，不重复扣减
      const r = await api.post(`/api/companies/${c.company_id}/clear?year=${c.year}&deadline=${c.year}-12-31`, null, api.idemKey());
      setMsg({ type: "ok", text: `清缴完成：状态 ${r.status === "deficit" ? "缺口" : r.status === "compliant" ? "达标" : "待清缴"}，缺口 ${fmtNum(r.deficit)} 吨` });
      api.get("/api/compliance").then(setCompliance);
      loadAccount();
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    }
  };

  return html`
    <div class="panel">
      <h3>配额分配</h3>
      ${isAdmin ? html`
        <form class="form-grid" onSubmit=${allocate}>
          <div class="field"><label>企业</label>
            <select value=${form.company_id} onChange=${set("company_id")} required>
              <option value="">请选择企业</option>
              ${companies.map((c) => html`<option key=${c.id} value=${c.id}>${c.name}</option>`)}
            </select>
          </div>
          <div class="field"><label>年度</label>
            <select value=${form.year} onChange=${set("year")}>
              ${[2023, 2024, 2025, 2026].map((y) => html`<option value=${y}>${y}</option>`)}
            </select>
          </div>
          <div class="field"><label>历史基准排放 (tCO2e)</label><input type="number" value=${form.baseline} onChange=${set("baseline")} /></div>
          <div class="field"><label>免费配额 (t)</label><input type="number" value=${form.allocation_amount} onChange=${set("allocation_amount")} required /></div>
          <div class="field"><label>调整量</label><input type="number" value=${form.adjustment} onChange=${set("adjustment")} placeholder="可为负" /></div>
          <div class="actions"><button class="btn" type="submit">分配配额</button></div>
        </form>` : html`<div class="empty">配额分配由监管管理员执行</div>`}
    </div>

    <div class="panel">
      <h3>配额账户与交易台账</h3>
      <div class="filter-bar">
        <div class="field"><label>企业</label>
          <select value=${selCompany} onChange=${(e) => setSelCompany(e.target.value)}>
            <option value="">请选择企业</option>
            ${companies.map((c) => html`<option key=${c.id} value=${c.id}>${c.name}</option>`)}
          </select>
        </div>
        <div class="field"><label>年度</label>
          <select value=${selYear} onChange=${(e) => setSelYear(Number(e.target.value))}>
            ${[2023, 2024, 2025, 2026].map((y) => html`<option value=${y}>${y}</option>`)}
          </select>
        </div>
      </div>
      ${account ? html`
        <div class="cards">
          <div class="card"><div class="label">期初配额</div><div class="value">${fmtNum(account.opening_balance)} t</div></div>
          <div class="card"><div class="label">当前持仓</div><div class="value">${fmtNum(account.current_balance)} t</div></div>
          <div class="card"><div class="label">履约冻结</div><div class="value">${fmtNum(account.frozen_balance)} t</div></div>
          <div class="card"><div class="label">交易占用</div><div class="value">${fmtNum(account.reserved_balance || 0)} t</div></div>
          <div class="card"><div class="label">自由可用</div><div class="value">${fmtNum(account.available_balance)} t</div></div>
        </div>
        <form class="form-grid" onSubmit=${doTransfer}>
          <div class="field"><label>类型</label>
            <select value=${txForm.tx_type} onChange=${setTx("tx_type")}>
              <option value="buy">买入</option><option value="sell">卖出</option>
              <option value="transfer_in">划入</option><option value="transfer_out">划出</option>
            </select>
          </div>
          <div class="field"><label>数量 (t)</label><input type="number" value=${txForm.amount} onChange=${setTx("amount")} required /></div>
          <div class="field"><label>对手方</label><input value=${txForm.counterparty} onChange=${setTx("counterparty")} /></div>
          <div class="field"><label>单价 (元/t)</label><input type="number" value=${txForm.price} onChange=${setTx("price")} /></div>
          <div class="field"><label>日期</label><input value=${txForm.tx_date} onChange=${setTx("tx_date")} placeholder="YYYY-MM-DD" /></div>
          <div class="field"><label>备注</label><input value=${txForm.remark} onChange=${setTx("remark")} /></div>
          <div class="actions"><button class="btn" type="submit" disabled=${submitting}>${submitting ? "提交中…" : "提交交易"}</button></div>
        </form>
        <table style=${{marginTop: "16px"}}>
          <thead><tr><th>ID</th><th>类型</th><th>数量 (t)</th><th>对手方</th><th>单价</th><th>日期</th><th>持仓</th><th>履约冻结</th><th>交易占用</th><th>备注</th></tr></thead>
          <tbody>
            ${txs.map((t) => html`
              <tr key=${t.id}>
                <td class="mono">#${t.id}</td>
                <td>${txLabel[t.tx_type] || t.tx_type}</td>
                <td style=${{fontWeight: "600"}}>${fmtNum(t.amount)}</td>
                <td>${t.counterparty || "-"}</td>
                <td>${t.price !== null ? fmtNum(t.price) + " 元" : "-"}</td>
                <td>${t.tx_date || "-"}</td>
                <td>${fmtNum(t.balance_after)}</td>
                <td>${fmtNum(t.frozen_after)}</td>
                <td>${fmtNum(t.reserved_after || 0)}</td>
                <td>${t.remark || "-"}</td>
              </tr>`)}
            ${txs.length === 0 && html`<tr><td colspan="10" class="empty">暂无交易记录</td></tr>`}
          </tbody>
        </table>` : html`<div class="msg err">${msg.text || "该年度尚无配额账户，请先分配配额"}</div>`}
    </div>

    <div class="panel">
      <h3>年度履约</h3>
      <table>
        <thead><tr><th>企业</th><th>年度</th><th>核查排放 (tCO2e)</th><th>已清缴 (t)</th><th>冻结 (t)</th><th>缺口 (t)</th><th>状态</th><th></th></tr></thead>
        <tbody>
          ${compliance.map((r) => html`
            <tr key=${r.id}>
              <td>${companies.find((c) => c.id === r.company_id)?.name || r.company_id}</td>
              <td>${r.year}</td>
              <td>${fmtNum(r.verified_emission)}</td>
              <td>${fmtNum(r.cleared_amount)}</td>
              <td>${fmtNum(r.frozen_amount)}</td>
              <td>${fmtNum(r.deficit)}</td>
              <td>${html([StatusBadge(r.status)])}</td>
              <td>${isAdmin && (r.status === "pending" || r.status === "deficit") && html`<button class="btn sm" onClick=${() => doClear({ ...r, name: companies.find((c) => c.id === r.company_id)?.name })}>${r.status === "deficit" ? "补缴/清缴" : "清缴"}</button>`}</td>
            </tr>`)}
          ${compliance.length === 0 && html`<tr><td colspan="8" class="empty">暂无履约记录，报告批准或清缴后展示</td></tr>`}
        </tbody>
      </table>
    </div>
    ${msg.text && html`<div class="msg ${msg.type}">${msg.text}</div>`}
  `;
};
