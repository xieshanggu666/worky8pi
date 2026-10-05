views.LoansView = () => {
  const [companies, setCompanies] = React.useState([]);
  const [loans, setLoans] = React.useState([]);
  const [selYear, setSelYear] = React.useState(2026);
  const [selStatus, setSelStatus] = React.useState("");
  const [form, setForm] = React.useState({
    lender_id: "", borrower_id: "", amount: "", price: "", year: 2026,
    initiator: "lender", due_date: "", remark: "", autoClear: true, autoRecover: true,
  });
  const [msg, setMsg] = React.useState({ type: "", text: "" });
  const [submitting, setSubmitting] = React.useState(false);
  const user = window.__user;
  const isAdmin = user.role === "admin";
  const isVerifier = user.role === "verifier";

  const load = React.useCallback(async () => {
    const params = new URLSearchParams();
    if (selYear) params.set("year", selYear);
    if (selStatus) params.set("status", selStatus);
    try {
      setLoans(await api.get(`/api/loans?${params.toString()}`));
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    }
  }, [selYear, selStatus]);

  React.useEffect(() => { api.get("/api/companies").then(setCompanies).catch(() => {}); }, []);
  React.useEffect(() => { load(); }, [load]);

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  const refresh = (type, text) => { setMsg({ type, text }); load(); };

  // 企业用户自动把本方填入出借/借入方
  React.useEffect(() => {
    if (isAdmin || isVerifier || !user.company_id) return;
    setForm((f) => ({
      ...f,
      lender_id: form.initiator === "lender" ? user.company_id : f.lender_id,
      borrower_id: form.initiator === "borrower" ? user.company_id : f.borrower_id,
    }));
  }, [form.initiator]);  // eslint-disable-line react-hooks/exhaustive-deps

  const create = async (e) => {
    e.preventDefault();
    if (submitting) return;
    if (!form.lender_id || !form.borrower_id) { setMsg({ type: "err", text: "请选择出借/借入企业" }); return; }
    if (Number(form.lender_id) === Number(form.borrower_id)) { setMsg({ type: "err", text: "出借方与借入方不能为同一企业" }); return; }
    if (!form.due_date) { setMsg({ type: "err", text: "请选择到期日" }); return; }
    setSubmitting(true);
    try {
      const r = await api.post("/api/loans", {
        lender_id: Number(form.lender_id),
        borrower_id: Number(form.borrower_id),
        year: Number(form.year),
        amount: Number(form.amount),
        price: Number(form.price || 0),
        initiator: form.initiator,
        due_date: form.due_date,
        remark: form.remark,
        auto_clear_deficit: form.autoClear,
        auto_recover_default: form.autoRecover,
      }, api.idemKey());
      refresh("ok", `借贷单 ${r.loan_no} 已创建，状态：${StatusBadge(r.status).props.children}`);
      setForm({ ...form, amount: "", price: "", remark: "" });
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    } finally {
      setSubmitting(false);
    }
  };

  const act = async (l, action, label, body = null, needConfirm = true) => {
    if (needConfirm && !confirm(`确认对借贷单 ${l.loan_no} 执行「${label}」？`)) return;
    try {
      const r = await api.post(`/api/loans/${l.id}/${action}`, body, api.idemKey());
      const cur = r.loan || r;
      let extra = "";
      if (action === "repay" && r.loan) {
        extra = `，已还 ${fmtNum(r.loan.repaid_amount, 4)} / ${fmtNum(r.loan.amount, 4)} 吨`;
      }
      refresh("ok", `借贷单 ${l.loan_no} 已${label}，当前状态：${cur.status}${extra}`);
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    }
  };

  const scan = async () => {
    try {
      const r = await api.post("/api/loans/overdue/scan", {}, api.idemKey());
      refresh("ok", `逾期巡检完成：新标记 ${r.marked} 笔`);
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    }
  };

  const declareDefault = async (l) => {
    const reason = prompt("宣布违约原因（至少 2 个字符）");
    if (reason === null) return;
    if (reason.trim().length < 2) { setMsg({ type: "err", text: "违约原因至少 2 个字符" }); return; }
    try {
      await api.post(`/api/loans/${l.id}/default`, { reason: reason.trim() }, api.idemKey());
      refresh("ok", `借贷单 ${l.loan_no} 已宣布违约，欠额 ${fmtNum(l.outstanding_amount, 4)} 吨`);
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    }
  };

  const recoverAll = async (l) => {
    if (!confirm(`确认按借入方 ${l.borrower_name} 汇总追偿 ${l.year} 年度全部逾期/违约欠额？`)) return;
    try {
      const r = await api.post(
        `/api/loans/defaults/${l.borrower_id}/recover?year=${l.year}`, {}, api.idemKey()
      );
      refresh("ok", `监管追偿完成：收回 ${fmtNum(r.recovered_volume, 4)} 吨`);
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    }
  };

  const sideOf = (l) => {
    if (isAdmin || isVerifier) return null;
    if (user.company_id === l.lender_id) return "lender";
    if (user.company_id === l.borrower_id) return "borrower";
    return null;
  };

  const years = [2023, 2024, 2025, 2026, 2027];

  return html`
    ${(user.role === "enterprise") && html`
    <div class="panel">
      <h3>发起配额借贷</h3>
      <form class="form-grid" onSubmit=${create}>
        <div class="field"><label>我是</label>
          <select value=${form.initiator} onChange=${set("initiator")}>
            <option value="lender">出借方（出让配额）</option>
            <option value="borrower">借入方（申请借入）</option>
          </select>
        </div>
        <div class="field"><label>出借企业</label>
          <select value=${form.lender_id} onChange=${set("lender_id")}
            ${form.initiator === "lender" ? "disabled" : ""} required>
            <option value="">请选择出借方</option>
            ${companies.map((c) => html`<option key=${c.id} value=${c.id}>${c.name}</option>`)}
          </select>
        </div>
        <div class="field"><label>借入企业</label>
          <select value=${form.borrower_id} onChange=${set("borrower_id")}
            ${form.initiator === "borrower" ? "disabled" : ""} required>
            <option value="">请选择借入方</option>
            ${companies.map((c) => html`<option key=${c.id} value=${c.id}>${c.name}</option>`)}
          </select>
        </div>
        <div class="field"><label>年度</label>
          <select value=${form.year} onChange=${set("year")}>
            ${years.map((y) => html`<option value=${y}>${y}</option>`)}
          </select>
        </div>
        <div class="field"><label>借贷数量 (t)</label>
          <input type="number" min="0" step="0.0001" value=${form.amount} onChange=${set("amount")} required /></div>
        <div class="field"><label>约定费用 (元/t)</label>
          <input type="number" min="0" value=${form.price} onChange=${set("price")} /></div>
        <div class="field"><label>到期日</label>
          <input type="date" value=${form.due_date} onChange=${set("due_date")} required /></div>
        <div class="field"><label>备注</label><input value=${form.remark} onChange=${set("remark")} /></div>
        <div class="field"><label>放款后履约</label>
          <label style=${{display: "flex", alignItems: "center", gap: "6px", fontWeight: "normal"}}>
            <input type="checkbox" checked=${form.autoClear}
              onChange=${(e) => setForm({ ...form, autoClear: e.target.checked })} />
            放款到账自动清缴借入方${form.year}年度缺口
          </label>
        </div>
        <div class="field"><label>欠额追偿</label>
          <label style=${{display: "flex", alignItems: "center", gap: "6px", fontWeight: "normal"}}>
            <input type="checkbox" checked=${form.autoRecover}
              onChange=${(e) => setForm({ ...form, autoRecover: e.target.checked })} />
            借入方后续交易到账时自动追偿逾期/违约欠额
          </label>
        </div>
        <div class="actions"><button class="btn" type="submit" disabled=${submitting}>
          ${submitting ? "提交中…" : "创建借贷单（发起方即确认）"}
        </button></div>
      </form>
      <div class="empty" style=${{textAlign: "left", marginTop: "8px"}}>
        双方确认后，出借方对应配额转为<b>交易占用</b>（不影响持仓，但不可卖出/被冻结）；
        放款时划转给借入方，并在同一事务内自动核销借入方同年度履约缺口。
        放款前任一方可撤销、占用自动释放；到期后借入方用自有自由可用配额归还，
        逾期由监管标记并可宣布违约、手动追偿，或在后续交易/竞价到账后自动追偿。
      </div>
    </div>`}

    <div class="panel">
      <h3>配额借贷单
        ${isAdmin && html`
          <button class="btn sm" style=${{float: "right"}} onClick=${scan}>监管逾期巡检</button>`}
      </h3>
      <div class="filter-bar">
        <div class="field"><label>年度</label>
          <select value=${selYear} onChange=${(e) => setSelYear(Number(e.target.value))}>
            <option value="">全部</option>
            ${years.map((y) => html`<option value=${y}>${y}</option>`)}
          </select>
        </div>
        <div class="field"><label>状态</label>
          <select value=${selStatus} onChange=${(e) => setSelStatus(e.target.value)}>
            <option value="">全部</option>
            <option value="pending">待确认</option>
            <option value="confirmed">双方已确认</option>
            <option value="active">在贷</option>
            <option value="overdue">已逾期</option>
            <option value="defaulted">已违约</option>
            <option value="repaid">已清偿</option>
            <option value="cancelled">已撤销</option>
          </select>
        </div>
      </div>
      <table>
        <thead><tr>
          <th>借贷单号</th><th>年度</th><th>出借方</th><th>借入方</th><th>数量 (t)</th>
          <th>到期日</th><th>已还 (t)</th><th>状态</th><th>操作</th>
        </tr></thead>
        <tbody>
          ${loans.map((l) => {
            const side = sideOf(l);
            const iConfirmed = side === "lender" ? l.lender_confirmed : side === "borrower" ? l.borrower_confirmed : true;
            const canWrite = side !== null;
            return html`
            <tr key=${l.id}>
              <td class="mono">${l.loan_no}</td>
              <td>${l.year}</td>
              <td>${l.lender_name}${side === "lender" ? "（我）" : ""}</td>
              <td>${l.borrower_name}${side === "borrower" ? "（我）" : ""}</td>
              <td style=${{fontWeight: "600"}}>${fmtNum(l.amount, 4)}</td>
              <td>${l.due_date}</td>
              <td>${fmtNum(l.repaid_amount, 4)}${l.defaulted_amount > 0 ? html`<div class="muted" style=${{fontSize: "12px"}}>违约欠额 ${fmtNum(l.defaulted_amount, 4)}</div>` : ""}</td>
              <td>${html([StatusBadge(l.status)])}
                ${l.default_reason ? html`<div class="muted" style=${{fontSize: "12px"}}>${l.default_reason}</div>` : ""}
                ${l.cancel_reason ? html`<div class="muted" style=${{fontSize: "12px"}}>${l.cancel_reason}</div>` : ""}
              </td>
              <td style=${{whiteSpace: "nowrap"}}>
                ${canWrite && l.status === "pending" && !iConfirmed && html`
                  <button class="btn sm" onClick=${() => act(l, "confirm", "确认", null, false)}>确认</button>`}
                ${canWrite && (l.status === "pending" || l.status === "confirmed") && html`
                  <button class="btn sm" onClick=${() => {
                    const reason = prompt("撤销原因（可留空）") || "";
                    if (reason === null) return;
                    act(l, "cancel", "撤销", { reason }, false);
                  }}>撤销</button>`}
                ${canWrite && l.status === "confirmed" && html`
                  <button class="btn sm" onClick=${() => act(l, "disburse", "放款")}>放款</button>`}
                ${canWrite && side === "borrower" && ["active", "overdue", "defaulted"].includes(l.status) && l.outstanding_amount > 0 && html`
                  <button class="btn sm" onClick=${() => act(l, "repay", "归还", { amount: null })}>归还</button>`}
                ${isAdmin && l.status === "overdue" && html`
                  <button class="btn sm danger" onClick=${() => declareDefault(l)}>宣布违约</button>`}
                ${isAdmin && ["overdue", "defaulted"].includes(l.status) && l.outstanding_amount > 0 && html`
                  <button class="btn sm" onClick=${() => recoverAll(l)}>监管追偿</button>`}
                ${(l.status === "repaid" || l.status === "cancelled") ? html`<span class="muted">-</span>` : ""}
              </td>
            </tr>`;
          })}
          ${loans.length === 0 && html`<tr><td colspan="9" class="empty">暂无配额借贷单</td></tr>`}
        </tbody>
      </table>
    </div>
    ${msg.text && html`<div class="msg ${msg.type}">${msg.text}</div>`}
  `;
};
