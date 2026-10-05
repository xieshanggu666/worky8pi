window.views = window.views || {};

const loanStatusMap = {
  pending: ["warn", "待批准"],
  reserved: ["info", "已冻结待拨付"],
  active: ["ok", "履约中"],
  overdue: ["danger", "已逾期"],
  defaulted: ["danger", "违约追偿中"],
  repaid: ["ok", "已清偿"],
  rejected: ["muted", "已驳回"],
  cancelled: ["muted", "已撤销"],
};

const loanRepaySourceMap = {
  repay: "主动归还",
  default_clawback: "违约收回",
  manual_recovery: "手动追偿",
  auto_recovery: "到账自动追偿",
};

views.LoanView = () => {
  const user = window.__user;
  const isAdmin = user.role === "admin";
  const isRegulator = user.role === "admin" || user.role === "verifier";

  const [loans, setLoans] = React.useState([]);
  const [selected, setSelected] = React.useState(null);
  const [companies, setCompanies] = React.useState([]);
  const [logs, setLogs] = React.useState([]);
  const [selYear, setSelYear] = React.useState(2026);
  const [selStatus, setSelStatus] = useState("");
  const [tab, setTab] = React.useState("loans");
  const [form, setForm] = React.useState({
    lender_id: "", borrower_id: "", amount: "", due_date: "",
    interest_rate: 0, initiator: "lender", remark: "", year: 2026,
    autoClear: true, autoRecover: true,
  });
  const [repayAmt, setRepayAmt] = React.useState("");
  const [reason, setReason] = React.useState("");
  const [msg, setMsg] = React.useState({ type: "", text: "" });
  const [busy, setBusy] = React.useState(false);

  const load = React.useCallback(async () => {
    const params = new URLSearchParams();
    if (selYear) params.set("year", selYear);
    if (selStatus) params.set("status", selStatus);
    try {
      setLoans(await api.get(`/api/loans?${params.toString()}`));
    } catch (e) { setMsg({ type: "err", text: e.message }); }
  }, [selYear, selStatus]);

  React.useEffect(() => {
    api.get("/api/companies").then(setCompanies).catch(() => {});
  }, []);
  React.useEffect(() => { load(); }, [load]);

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  const companyName = (id) => {
    const c = companies.find((x) => x.id === id);
    return c ? c.name : `企业${id}`;
  };

  const refresh = async (type, text) => {
    setMsg(text ? { type, text } : { type: "", text: "" });
    const params = new URLSearchParams();
    if (selYear) params.set("year", selYear);
    const list = await api.get(`/api/loans?${params.toString()}`);
    setLoans(list);
    if (selected) {
      const fresh = list.find((x) => x.id === selected.id);
      if (fresh) openDetail(fresh);
    }
  };

  const openDetail = async (loan) => {
    setSelected(loan);
    setReason("");
    setRepayAmt("");
    try {
      const detail = await api.get(`/api/loans/${loan.id}`);
      setSelected(detail);
      if (isRegulator) {
        setLogs(await api.get(`/api/loans/audit-logs?loan_id=${loan.id}`));
      }
    } catch (e) { setMsg({ type: "err", text: e.message }); }
  };

  // 企业发起时自动填入本方
  React.useEffect(() => {
    if (isAdmin || !user.company_id) return;
    setForm((f) => ({
      ...f,
      lender_id: f.initiator === "lender" ? user.company_id : f.lender_id,
      borrower_id: f.initiator === "borrower" ? user.company_id : f.borrower_id,
    }));
  }, [form.initiator]); // eslint-disable-line react-hooks/exhaustive-deps

  const createLoan = async (e) => {
    e.preventDefault();
    if (busy) return;
    if (!form.lender_id || !form.borrower_id) { setMsg({ type: "err", text: "请选择出借方与借入方" }); return; }
    if (Number(form.lender_id) === Number(form.borrower_id)) { setMsg({ type: "err", text: "出借方与借入方不能为同一企业" }); return; }
    if (!form.due_date) { setMsg({ type: "err", text: "请选择约定归还日期" }); return; }
    setBusy(true);
    try {
      const r = await api.post("/api/loans", {
        lender_id: Number(form.lender_id),
        borrower_id: Number(form.borrower_id),
        year: Number(form.year),
        amount: Number(form.amount),
        due_date: form.due_date,
        interest_rate: Number(form.interest_rate || 0),
        initiator: form.initiator,
        remark: form.remark,
        auto_clear_deficit: form.autoClear,
        auto_recover_default: form.autoRecover,
      }, api.idemKey());
      setMsg({ type: "ok", text: `借据 ${r.loan_no} 已发起，等待对方确认与监管批准` });
      setForm({ ...form, amount: "", due_date: "", remark: "" });
      await load();
      openDetail(r);
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    } finally { setBusy(false); }
  };

  const act = async (path, body, okText, label) => {
    if (busy) return;
    setBusy(true);
    try {
      const r = await api.post(`/api/loans/${selected.id}${path}`, body || {}, api.idemKey());
      await refresh("ok", label ? `${label}完成` : (okText || "操作完成"));
      return r;
    } catch (e) {
      setMsg({ type: "err", text: e.message });
    } finally { setBusy(false); }
  };

  const confirm = () => act("/confirm", null, "确认成功");
  const approve = () => act("/approve", null, "批准冻结成功");
  const disburse = () => act("/disburse", null, "拨付成功");
  const cancel = () => {
    if (!reason && !confirm("不填原因直接撤销？")) return;
    act("/cancel", { reason: reason || "企业撤销" }, "撤销成功");
  };
  const reject = () => {
    if (reason.length < 2) { setMsg({ type: "err", text: "请填写至少 2 个字符的驳回原因" }); return; }
    act("/reject", { reason }, "驳回成功");
  };
  const repay = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const body = {};
      if (repayAmt) body.amount = Number(repayAmt);
      const r = await api.post(`/api/loans/${selected.id}/repay`, body, api.idemKey());
      setRepayAmt("");
      await refresh("ok", `已归还 ${fmtNum(r.repayment.quantity)} 吨`);
    } catch (e) { setMsg({ type: "err", text: e.message }); }
    finally { setBusy(false); }
  };
  const declareDefault = () => {
    if (reason.length < 2) { setMsg({ type: "err", text: "请填写至少 2 个字符的违约原因" }); return; }
    act("/default", { reason }, "违约处理完成");
  };
  const markOverdue = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const r = await api.post(`/api/loans/mark-overdue?year=${selYear || form.year}`, {}, api.idemKey());
      await refresh("ok", `已标记 ${r.marked_count} 张逾期借据`);
    } catch (e) { setMsg({ type: "err", text: e.message }); }
    finally { setBusy(false); }
  };
  const recover = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const r = await api.post(
        `/api/loans/defaults/${selected.borrower_id}/recover?year=${selected.year}`,
        {}, api.idemKey());
      await refresh("ok", `已追偿 ${fmtNum(r.recovered_volume)} 吨`);
    } catch (e) { setMsg({ type: "err", text: e.message }); }
    finally { setBusy(false); }
  };

  const isParty = (l) => user.company_id && (user.company_id === l.lender_id || user.company_id === l.borrower_id);
  const asBorrower = (l) => user.company_id === l.borrower_id;

  const renderDetail = () => {
    const l = selected;
    if (!l) return html`<div class="empty">选择左侧借据查看详情</div>`;
    const [badgeCls, badgeText] = loanStatusMap[l.status] || ["muted", l.status];
    const repayments = l.repayments || [];
    return html`
      <div class="panel">
        <div class="card">
          <div class="label">借据编号</div>
          <div class="value mono">${l.loan_no} <span class="badge ${badgeCls}">${badgeText}</span></div>
        </div>
        <div class="form-grid">
          <div class="field"><label>出借方</label><div class="value">${l.lender_name}</div></div>
          <div class="field"><label>借入方</label><div class="value">${l.borrower_name}</div></div>
          <div class="field"><label>拆借量（吨）</label><div class="value mono">${fmtNum(l.amount)}</div></div>
          <div class="field"><label>约定费率（%）</label><div class="value mono">${fmtNum(l.interest_rate)}</div></div>
          <div class="field"><label>约定归还日</label><div class="value mono">${l.due_date}</div></div>
          <div class="field"><label>已归还（吨）</label><div class="value mono">${fmtNum(l.returned_amount)}</div></div>
          <div class="field"><label>未偿本金（吨）</label><div class="value mono">${fmtNum(l.outstanding_principal)}</div></div>
          <div class="field"><label>违约欠额（吨）</label>
            <div class="value mono">${fmtNum(l.default_outstanding)}${l.status === "defaulted" ? html` <span class="badge danger">追偿中</span>` : ""}</div></div>
        </div>
        ${l.remark ? html`<div class="sub muted">备注：${l.remark}</div>` : ""}
        ${l.reject_reason ? html`<div class="sub">驳回原因：${l.reject_reason}</div>` : ""}
        ${l.cancel_reason ? html`<div class="sub">撤销原因：${l.cancel_reason}</div>` : ""}

        <div class="actions" style="margin-top:12px;flex-wrap:wrap;gap:8px">
          ${l.status === "pending" && isParty(l) && html`
            ${((l.lender_id === user.company_id && !l.lender_confirmed)
              || (l.borrower_id === user.company_id && !l.borrower_confirmed))
              && html`<button class="btn sm" onClick=${confirm} disabled=${busy}>本方确认</button>`}
            <button class="btn sm ghost" onClick=${cancel} disabled=${busy}>撤销协商</button>`}
          ${l.status === "pending" && isAdmin && html`
            <input class="input" placeholder="驳回原因（≥2字）" value=${reason}
              onChange=${(e) => setReason(e.target.value)} style="min-width:200px"/>
            <button class="btn sm danger" onClick=${reject} disabled=${busy}>驳回</button>
            <button class="btn sm" onClick=${approve} disabled=${busy}>批准并冻结出借配额</button>`}
          ${l.status === "reserved" && isAdmin && html`
            <button class="btn sm" onClick=${disburse} disabled=${busy}>拨付（出库+到账+联动清缴）</button>
            <input class="input" placeholder="撤销原因" value=${reason}
              onChange=${(e) => setReason(e.target.value)} style="min-width:200px"/>
            <button class="btn sm ghost" onClick=${cancel} disabled=${busy}>撤销并释放冻结</button>`}
          ${(l.status === "active" || l.status === "overdue") && asBorrower(l) && html`
            <input class="input" type="number" placeholder="归还数量（空=全额）" value=${repayAmt}
              onChange=${(e) => setRepayAmt(e.target.value)} style="width:200px"/>
            <button class="btn sm" onClick=${repay} disabled=${busy}>归还配额</button>`}
          ${l.status === "overdue" && isAdmin && html`
            <input class="input" placeholder="违约原因（≥2字）" value=${reason}
              onChange=${(e) => setReason(e.target.value)} style="min-width:220px"/>
            <button class="btn sm danger" onClick=${declareDefault} disabled=${busy}>宣告违约（尽力收回）</button>`}
          ${l.status === "defaulted" && isAdmin && html`
            <button class="btn sm" onClick=${recover} disabled=${busy}>手动追偿欠额</button>`}
        </div>

        ${repayments.length > 0 && html`
          <h4 style="margin:16px 0 8px">归还 / 追偿明细</h4>
          <table>
            <thead><tr><th>单号</th><th>来源</th><th>数量（吨）</th><th>说明</th><th>时间</th></tr></thead>
            <tbody>
              ${repayments.map((r) => html`
                <tr key=${r.id}>
                  <td class="mono">${r.repay_no}</td>
                  <td>${loanRepaySourceMap[r.source] || r.source}</td>
                  <td class="mono">${fmtNum(r.quantity)}</td>
                  <td>${r.remark}</td>
                  <td class="mono muted">${r.created_at ? r.created_at.replace("T", " ").slice(0, 19) : ""}</td>
                </tr>`)}
            </tbody>
          </table>`}

        ${isRegulator && logs.length > 0 && html`
          <h4 style="margin:16px 0 8px">操作审计</h4>
          <table>
            <thead><tr><th>操作人</th><th>动作</th><th>结果</th><th>详情</th><th>时间</th></tr></thead>
            <tbody>
              ${logs.slice(0, 30).map((g) => html`
                <tr key=${g.id}>
                  <td>${g.operator_name}（${g.operator_role}）</td>
                  <td class="mono">${g.action}</td>
                  <td>${g.result === "success" ? html`<span class="badge ok">成功</span>` : html`<span class="badge danger">拒绝</span>`}</td>
                  <td>${g.detail}</td>
                  <td class="mono muted">${g.created_at.replace("T", " ").slice(0, 19)}</td>
                </tr>`)}
            </tbody>
          </table>`}
      </div>`;
  };

  return html`
    <div class="cards">
      <div class="panel" style="flex:1;min-width:520px">
        <div class="filter-bar">
          <label>年度
            <select value=${selYear} onChange=${(e) => setSelYear(Number(e.target.value))}>
              ${[2024, 2025, 2026, 2027].map((y) => html`<option key=${y} value=${y}>${y}</option>`)}
            </select>
          </label>
          <label>状态
            <select value=${selStatus} onChange=${(e) => setSelStatus(e.target.value)}>
              <option value="">全部</option>
              ${Object.entries(loanStatusMap).map(([k, [, t]]) => html`<option key=${k} value=${k}>${t}</option>`)}
            </select>
          </label>
          ${isAdmin && html`<button class="btn sm ghost" onClick=${markOverdue} disabled=${busy}>批量标记逾期</button>`}
          <button class="btn sm ghost" onClick=${load}>刷新</button>
        </div>
        ${msg.text && html`<div class="msg ${msg.type}">${msg.text}</div>`}
        <table>
          <thead>
            <tr><th>借据号</th><th>出借方</th><th>借入方</th><th>数量</th><th>归还日</th>
              <th>未偿</th><th>状态</th></tr>
          </thead>
          <tbody>
            ${loans.length === 0 && html`<tr><td colspan="7" class="empty">暂无借据</td></tr>`}
            ${loans.map((l) => {
              const [cls, txt] = loanStatusMap[l.status] || ["muted", l.status];
              return html`
                <tr key=${l.id} onClick=${() => openDetail(l)}
                  style="cursor:pointer;${selected && selected.id === l.id ? "background:var(--hover,#f1f5f9)" : ""}">
                  <td class="mono">${l.loan_no}</td>
                  <td>${l.lender_name}</td>
                  <td>${l.borrower_name}</td>
                  <td class="mono">${fmtNum(l.amount)}</td>
                  <td class="mono">${l.due_date}</td>
                  <td class="mono">${fmtNum(l.outstanding_principal)}</td>
                  <td><span class="badge ${cls}">${txt}</span></td>
                </tr>`;
            })}
          </tbody>
        </table>
      </div>

      <div style="width:520px;display:flex;flex-direction:column;gap:12px">
        ${user.role !== "verifier" && html`
        <div class="panel">
          <h4>${isAdmin ? "代企业发起拆借协商" : "发起配额拆借协商"}</h4>
          <form onSubmit=${createLoan}>
            <div class="form-grid">
              <div class="field">
                <label>本方角色</label>
                <select value=${form.initiator} onChange=${set("initiator")}>
                  <option value="lender">我是出借方（挂出可借配额）</option>
                  <option value="borrower">我是借入方（求借配额）</option>
                </select>
              </div>
              <div class="field">
                <label>年度</label>
                <select value=${form.year} onChange=${set("year")}>
                  ${[2024, 2025, 2026, 2027].map((y) => html`<option key=${y} value=${y}>${y}</option>`)}
                </select>
              </div>
              <div class="field">
                <label>出借方</label>
                <select value=${form.lender_id} onChange=${set("lender_id")}
                  disabled=${!isAdmin && form.initiator === "lender"}>
                  <option value="">请选择</option>
                  ${companies.map((c) => html`<option key=${c.id} value=${c.id}>${c.name}</option>`)}
                </select>
              </div>
              <div class="field">
                <label>借入方</label>
                <select value=${form.borrower_id} onChange=${set("borrower_id")}
                  disabled=${!isAdmin && form.initiator === "borrower"}>
                  <option value="">请选择</option>
                  ${companies.map((c) => html`<option key=${c.id} value=${c.id}>${c.name}</option>`)}
                </select>
              </div>
              <div class="field"><label>拆借量（吨）</label>
                <input type="number" min="0" step="0.0001" value=${form.amount} onChange=${set("amount")}/></div>
              <div class="field"><label>约定归还日</label>
                <input type="date" value=${form.due_date} onChange=${set("due_date")}/></div>
              <div class="field"><label>约定费率（%，资金侧结算）</label>
                <input type="number" min="0" step="0.01" value=${form.interest_rate} onChange=${set("interest_rate")}/></div>
              <div class="field"><label>备注</label>
                <input value=${form.remark} onChange=${set("remark")} placeholder="可选"/></div>
            </div>
            <label class="field" style="display:flex;gap:8px;align-items:center">
              <input type="checkbox" checked=${form.autoClear}
                onChange=${(e) => setForm({ ...form, autoClear: e.target.checked })}/>
              拨付到账时自动核销借入方同年度履约缺口
            </label>
            <label class="field" style="display:flex;gap:8px;align-items:center">
              <input type="checkbox" checked=${form.autoRecover}
                onChange=${(e) => setForm({ ...form, autoRecover: e.target.checked })}/>
              违约后借入方交易/竞价到账时自动追偿欠额
            </label>
            <button class="btn" type="submit" disabled=${busy}>发起协商</button>
          </form>
        </div>`}
        ${renderDetail()}
      </div>
    </div>`;
};
