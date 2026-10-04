const html = htm.bind(React.createElement);

const parseHash = () => {
  const m = location.hash.match(/^#\/([a-z]+)(?:\/(\d+))?/);
  const path = m ? m[1] : "dashboard";
  const id = m && m[2] ? Number(m[2]) : null;
  return { path, params: { id } };
};

const TITLES = {
  dashboard: "工作台",
  companies: "控排企业",
  activity: "活动数据台账",
  factors: "排放因子库",
  calculation: "排放核算",
  quotas: "配额与交易",
  orders: "企业间订单",
  auctions: "集中竞价市场",
  reports: "MRV 报告",
};

const NAV = [
  ["dashboard", "工作台"],
  ["companies", "控排企业"],
  ["activity", "活动数据"],
  ["factors", "排放因子"],
  ["calculation", "排放核算"],
  ["quotas", "配额与交易"],
  ["orders", "企业间订单"],
  ["auctions", "集中竞价"],
  ["reports", "MRV 报告"],
];

function AppShell() {
  const [route, setRoute] = React.useState(parseHash());
  const [user, setUser] = React.useState(null);
  const [booted, setBooted] = React.useState(false);

  React.useEffect(() => {
    const onHash = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHash);
    api
      .get("/api/auth/me")
      .then((u) => {
        window.__user = u;
        setUser(u);
      })
      .catch(() => {})
      .finally(() => setBooted(true));
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  React.useEffect(() => {
    if (!booted) return;
    if (user) {
      if (route.path === "login") location.hash = "#/dashboard";
      window.scrollTo(0, 0);
      return;
    }
    if (route.path === "login") return;
    api
      .get("/api/auth/me")
      .then((u) => {
        window.__user = u;
        setUser(u);
      })
      .catch(() => {
        location.hash = "#/login";
      });
  }, [booted, user, route.path]);

  if (!booted) return html`<div class="empty" style=${{paddingTop: "40vh"}}>加载中...</div>`;
  if (!user) return html`<${views.LoginView} />`;

  const nav = (p) => () => { location.hash = `#/${p}`; };
  const logout = async () => {
    try { await api.post("/api/auth/logout"); } catch (_) {}
    window.__user = null;
    setUser(null);
    location.hash = "#/login";
  };

  let view;
  const { path, params } = route;
  if (path === "login") view = html`<${views.LoginView} />`;
  else if (path === "dashboard") view = html`<${views.DashboardView} />`;
  else if (path === "companies" && params.id) view = html`<${views.CompanyDetailView} id=${params.id} />`;
  else if (path === "companies") view = html`<${views.CompaniesView} />`;
  else if (path === "activity") view = html`<${views.ActivityView} />`;
  else if (path === "factors") view = html`<${views.FactorsView} />`;
  else if (path === "calculation") view = html`<${views.CalculationView} />`;
  else if (path === "quotas") view = html`<${views.QuotaView} />`;
  else if (path === "orders") view = html`<${views.TradeOrdersView} />`;
  else if (path === "auctions") view = html`<${views.AuctionView} />`;
  else if (path === "reports") view = html`<${views.ReportsView} />`;
  else view = html`<${views.DashboardView} />`;

  return html`
    <div class="layout">
      <aside class="sidebar">
        <div class="brand">碳排放核算<br/>与交易管理系统</div>
        ${NAV.map(([p, label]) => html`
          <button class="nav-item ${path === p ? "active" : ""}" key=${p} onClick=${nav(p)}>${label}</button>`)}
      </aside>
      <main class="main">
        <div class="topbar">
          <h1>${TITLES[path] || "工作台"}</h1>
          <div class="user">
            <span>${user.display_name || user.username}（${user.role === "admin" ? "监管管理员" : user.role === "verifier" ? "核查员" : "控排企业"}）</span>
            <button onClick=${logout}>退出</button>
          </div>
        </div>
        ${view}
      </main>
    </div>
  `;
}

window.__renderErr = null;
window.addEventListener("error", (ev) => {
  window.__renderErr = (ev.error && (ev.error.stack || ev.error.message)) || ev.message;
});
window.addEventListener("unhandledrejection", (ev) => {
  window.__renderErr = "REJ: " + (ev.reason && (ev.reason.stack || ev.reason.message));
});

const root = ReactDOM.createRoot(document.getElementById("app"));
try {
  root.render(html`<${AppShell} />`);
} catch (e) {
  window.__renderErr = "SYNC: " + (e.stack || e.message);
}
