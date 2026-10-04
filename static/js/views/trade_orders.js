views.TradeOrdersView = () => {
  const [companies, setCompanies] = React.useState([]);
  const [orders, setOrders] = React.useState([]);
  const [selYear, setSelYear] = React.useState(2025);
  const [selStatus, setSelStatus] = React.useState("");
  const [form, setForm] = React.useState({
    seller_id: "", buyer_id: "", amount: "", price: "", year: 2025,
    initiator: "seller", tx_date: "", remark: "", autoClear: true,
  });
  const [msg, setMsg] = React.useState({ type: "", text: "" });
  const [submitting, setSubmitting] = React.useState(false);
  const user = window.__user;
  const isAdmin = user.role === "admin";

  const load = React.useCallback(async () => {
    const params = new URLSearchParams();
    if (selYear) params.set("year", selYear);
    if (selStatus) params.set("status", selStatus);
    try {
      setOrders(await api.get(`/api/trade-orders?${params.toString()}`));
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    }
  }, [selYear, selStatus]);

  React.useEffect(() => {
    api.get("/api/companies").then(setCompanies).catch(() => {});
  }, []);
  React.useEffect(() => { load(); }, [load]);

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  const refresh = (type, text) => {
    setMsg({ type, text });
    load();
  };

  // 企业用户挂单时自动把本方填入买方/卖方，避免越权
  React.useEffect(() => {
    if (isAdmin || !user.company_id) return;
    setForm((f) => ({
      ...f,
      seller_id: form.initiator === "seller" ? user.company_id : f.seller_id,
      buyer_id: form.initiator === "buyer" ? user.company_id : f.buyer_id,
    }));
  }, [form.initiator]);  // eslint-disable-line react-hooks/exhaustive-deps

  const create = async (e) => {
    e.preventDefault();
    if (submitting) return;
    if (!form.seller_id || !form.buyer_id) { setMsg({ type: "err", text: "请选择买卖双方企业" }); return; }
    if (Number(form.seller_id) === Number(form.buyer_id)) { setMsg({ type: "err", text: "买卖双方不能为同一企业" }); return; }
    setSubmitting(true);
    try {
      const r = await api.post("/api/trade-orders", {
        seller_id: Number(form.seller_id),
        buyer_id: Number(form.buyer_id),
        year: Number(form.year),
        amount: Number(form.amount),
        price: Number(form.price || 0),
        initiator: form.initiator,
        tx_date: form.tx_date,
        remark: form.remark,
        auto_clear_deficit: form.autoClear,
      }, api.idemKey());
      refresh("ok", `订单 ${r.order_no} 已创建，状态：${orderStatusMap[r.status][1]}`);
      setForm({ ...form, amount: "", price: "", tx_date: "", remark: "" });
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    } finally {
      setSubmitting(false);
    }
  };

  const act = async (o, action, label, needConfirm = true, body = null) => {
    if (needConfirm && !confirm(`确认对订单 ${o.order_no} 执行「${label}」？`)) return;
    try {
      const r = await api.post(`/api/trade-orders/${o.id}/${action}`, body, api.idemKey());
      let extra = "";
      if (action === "deliver" && r.buyer_clearance) {
        const c = r.buyer_clearance;
        if (c.status === "compliant") {
          extra = `；买方${r.year}年度履约已达标（累计清缴 ${fmtNum(c.cleared_amount, 4)} 吨）`;
        } else {
          extra = `；买方尚有缺口 ${fmtNum(c.deficit, 4)} 吨（已清缴 ${fmtNum(c.cleared_amount, 4)} 吨）`;
        }
      }
      refresh("ok", `订单 ${r.order_no} 已${label}，当前状态：${orderStatusMap[r.status][1]}${extra}`);
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    }
  };

  const partyOf = (o) => {
    if (isAdmin) return null;
    if (user.company_id === o.seller_id) return "seller";
    if (user.company_id === o.buyer_id) return "buyer";
    return null;
  };

  const companyName = (id) => companies.find((c) => c.id === id)?.name || id;

  const now = new Date().toISOString().slice(0, 10);

  return html`
    <div class="panel">
      <h3>发起企业间交易订单</h3>
      <form class="form-grid" onSubmit=${create}>
        <div class="field"><label>我是</label>
          <select value=${form.initiator} onChange=${set("initiator")}>
            <option value="seller">卖方（出让配额）</option>
            <option value="buyer">买方（求购配额）</option>
          </select>
        </div>
        <div class="field"><label>卖方企业</label>
          <select value=${form.seller_id} onChange=${set("seller_id")}
            ${!isAdmin && form.initiator === "seller" ? "disabled" : ""} required>
            <option value="">请选择卖方</option>
            ${companies.map((c) => html`<option key=${c.id} value=${c.id}>${c.name}</option>`)}
          </select>
        </div>
        <div class="field"><label>买方企业</label>
          <select value=${form.buyer_id} onChange=${set("buyer_id")}
            ${!isAdmin && form.initiator === "buyer" ? "disabled" : ""} required>
            <option value="">请选择买方</option>
            ${companies.map((c) => html`<option key=${c.id} value=${c.id}>${c.name}</option>`)}
          </select>
        </div>
        <div class="field"><label>年度</label>
          <select value=${form.year} onChange=${set("year")}>
            ${[2023, 2024, 2025, 2026].map((y) => html`<option value=${y}>${y}</option>`)}
          </select>
        </div>
        <div class="field"><label>交易数量 (t)</label>
          <input type="number" min="0" step="0.0001" value=${form.amount} onChange=${set("amount")} required /></div>
        <div class="field"><label>单价 (元/t)</label>
          <input type="number" min="0" value=${form.price} onChange=${set("price")} /></div>
        <div class="field"><label>成交日期</label>
          <input value=${form.tx_date} onChange=${set("tx_date")} placeholder=${now} /></div>
        <div class="field"><label>备注</label><input value=${form.remark} onChange=${set("remark")} /></div>
        <div class="field"><label>交割后履约</label>
          <label style=${{display: "flex", alignItems: "center", gap: "6px", fontWeight: "normal"}}>
            <input type="checkbox" checked=${form.autoClear}
              onChange=${(e) => setForm({ ...form, autoClear: e.target.checked })} />
            交割到账自动清缴买方${form.year}年度缺口
          </label>
        </div>
        <div class="actions"><button class="btn" type="submit" disabled=${submitting}>
          ${submitting ? "提交中…" : "创建订单（发起方即确认）"}
        </button></div>
      </form>
      <div class="empty" style=${{textAlign: "left", marginTop: "8px"}}>
        双方确认后，卖方相应配额将转为<b>交易占用</b>（不影响持仓，但不可卖出/划出/被履约冻结）；
        交割时划转给买方，并在同一事务内自动核销买方同年度履约缺口（先冻结核销、后到账补缴），
        履约状态与统计同步更新。交割前任一方可撤销，占用自动释放。
      </div>
    </div>

    <div class="panel">
      <h3>交易订单</h3>
      <div class="filter-bar">
        <div class="field"><label>年度</label>
          <select value=${selYear} onChange=${(e) => setSelYear(Number(e.target.value))}>
            <option value="">全部</option>
            ${[2023, 2024, 2025, 2026].map((y) => html`<option value=${y}>${y}</option>`)}
          </select>
        </div>
        <div class="field"><label>状态</label>
          <select value=${selStatus} onChange=${(e) => setSelStatus(e.target.value)}>
            <option value="">全部</option>
            <option value="pending">待对方确认</option>
            <option value="confirmed">双方已确认</option>
            <option value="delivered">已交割</option>
            <option value="cancelled">已撤销</option>
          </select>
        </div>
      </div>
      <table>
        <thead><tr>
          <th>订单号</th><th>年度</th><th>卖方</th><th>买方</th><th>数量 (t)</th><th>单价</th>
          <th>卖方确认</th><th>买方确认</th><th>状态</th><th>履约闭环</th><th>操作</th>
        </tr></thead>
        <tbody>
          ${orders.map((o) => {
            const side = partyOf(o);
            const iConfirmed = side === "seller" ? o.seller_confirmed : side === "buyer" ? o.buyer_confirmed : true;
            const canWrite = isAdmin || side !== null;
            return html`
            <tr key=${o.id}>
              <td class="mono">${o.order_no}</td>
              <td>${o.year}</td>
              <td>${o.seller_name}${side === "seller" ? "（我）" : ""}</td>
              <td>${o.buyer_name}${side === "buyer" ? "（我）" : ""}</td>
              <td style=${{fontWeight: "600"}}>${fmtNum(o.amount, 4)}</td>
              <td>${o.price ? fmtNum(o.price) + " 元" : "-"}</td>
              <td>${o.seller_confirmed ? "✓" : "—"}</td>
              <td>${o.buyer_confirmed ? "✓" : "—"}</td>
              <td>${html([StatusBadge(o.status)])}${o.cancel_reason ? html`<div class="muted" style=${{fontSize: "12px"}}>${o.cancel_reason}</div>` : ""}</td>
              <td>${o.auto_clear_deficit ? html`<span title="交割时自动核销买方同年度履约缺口">清缴联动</span>` : html`<span class="muted">不联动</span>`}</td>
              <td style=${{whiteSpace: "nowrap"}}>
                ${canWrite && o.status === "pending" && !iConfirmed && html`
                  <button class="btn sm" onClick=${() => act(o, "confirm", "确认", false)}>确认</button>`}
                ${canWrite && (o.status === "pending" || o.status === "confirmed") && html`
                  <button class="btn sm" onClick=${() => {
                    const reason = prompt("撤销原因（可留空）") || "";
                    if (reason === null) return;
                    act(o, "cancel", "撤销", false, { reason });
                  }}>撤销</button>`}
                ${canWrite && o.status === "confirmed" && html`
                  <button class="btn sm" onClick=${() => act(o, "deliver", "交割")}>交割</button>`}
                ${o.status === "delivered" || o.status === "cancelled" ? html`<span class="muted">-</span>` : ""}
              </td>
            </tr>`;
          })}
          ${orders.length === 0 && html`<tr><td colspan="11" class="empty">暂无交易订单</td></tr>`}
        </tbody>
      </table>
    </div>
    ${msg.text && html`<div class="msg ${msg.type}">${msg.text}</div>`}
  `;
};
