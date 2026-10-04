window.views = window.views || {};

views.LoginView = () => {
  const [username, setUsername] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [error, setError] = React.useState("");

  const submit = async (e) => {
    e.preventDefault();
    setError("");
    try {
      await api.post("/api/auth/login", { username, password });
      location.hash = "#/dashboard";
    } catch (err) {
      setError(err.message);
    }
  };

  return html`
    <div class="login-wrap">
      <form class="login-box" onSubmit=${submit}>
        <h2>碳排放核算与交易管理系统</h2>
        <div class="slogan">Carbon Emission MRV & Allowance Trading Platform</div>
        <div class="field">
          <label>用户名</label>
          <input value=${username} onChange=${(e) => setUsername(e.target.value)} placeholder="请输入用户名" required />
        </div>
        <div class="field">
          <label>密码</label>
          <input type="password" value=${password} onChange=${(e) => setPassword(e.target.value)} placeholder="请输入密码" required />
        </div>
        ${error && html`<div class="msg err">${error}</div>`}
        <button class="btn" type="submit">登 录</button>
      </form>
    </div>
  `;
};
