const api = {
  // 生成客户端幂等键：同一次提交在双击/重试/超时重发时只生效一次
  idemKey() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return "idem-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
  },
  async request(method, url, body, idemKey) {
    const headers = { "Content-Type": "application/json" };
    // 写操作携带幂等键，服务端据此去重，避免重复扣减/重复履约
    if (idemKey) headers["Idempotency-Key"] = idemKey;
    const res = await fetch(url, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
      credentials: "same-origin",
    });
    if (res.status === 401) {
      location.hash = "#/login";
      throw new Error("未登录");
    }
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.detail || "请求失败");
    }
    return res.json();
  },
  get(url) { return this.request("GET", url); },
  post(url, body, idemKey) { return this.request("POST", url, body, idemKey); },
  put(url, body) { return this.request("PUT", url, body); },
};

const fmtNum = (v, digits = 2) =>
  v === null || v === undefined ? "-" : Number(v).toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });

const StatusBadge = (s) => {
  const map = {
    active: ["active", "正常"],
    inactive: ["muted", "停用"],
    pending: ["warn", "待处理"],
    allocated: ["info", "已分配"],
    frozen: ["warn", "已冻结"],
    cleared: ["ok", "已清缴"],
    compliant: ["ok", "履约达标"],
    deficit: ["danger", "配额缺口"],
    reversed: ["muted", "已冲正"],
    draft: ["muted", "草稿"],
    submitted: ["info", "已提交"],
    approved: ["ok", "已批准"],
    open: ["info", "报价开放中"],
    matched: ["warn", "已撮合"],
    settled: ["ok", "已结算"],
    cancelled: ["danger", "已撤销"],
    partial: ["warn", "部分成交"],
    unmatched: ["muted", "未成交"],
    reserved: ["warn", "待结算"],
  };
  const [cls, label] = map[s] || ["muted", s];
  return `<span class="badge ${cls}">${label}</span>`;
};

const scopeLabel = (s) => ({ 1: "范围一", 2: "范围二", 3: "范围三" }[s] || s);
const txLabel = {
  allocation: "配额分配",
  buy: "买入",
  sell: "卖出",
  transfer_in: "转入",
  transfer_out: "转出",
  offset: "抵消",
  freeze: "履约冻结",
  frozen_clear: "冻结清缴",
  reversal: "报告冲正退还",
  reversal_unfreeze: "报告冲正解冻",
  clear: "履约清缴",
  trade_reserve: "订单交易占用",
  trade_release: "撤销释放占用",
  trade_deliver_out: "订单交割划出",
  trade_deliver_in: "订单交割受让",
  trade_deficit_clear: "订单到账清缴缺口",
  auction_bid_reserve: "竞价报价占用",
  auction_bid_release: "竞价撤单/未成交释放",
  auction_reserve_release: "竞价撤场释放",
  auction_deliver_out: "竞价结算划出",
  auction_deliver_in: "竞价结算受让",
  auction_deficit_clear: "竞价到账清缴缺口",
  auction_clear_refund: "冲正退还补缴",
  auction_clear_unfreeze: "冲正解除冻结",
  auction_clawback_out: "冲正收回配额",
  auction_clawback_in: "冲正退回配额",
  auction_default_repay_out: "违约欠额追偿划出",
  auction_default_repay_in: "违约欠额追偿到账",
};

const orderStatusMap = {
  pending: ["warn", "待对方确认"],
  confirmed: ["info", "双方已确认"],
  delivered: ["ok", "已交割"],
  cancelled: ["muted", "已撤销"],
};
