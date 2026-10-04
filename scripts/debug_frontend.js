const path = require("path");
const base = process.cwd();
const React = require(path.join(base, "static/vendor/react.production.min.js"));
const ReactDOM = require(path.join(base, "static/vendor/react-dom.production.min.js"));
const htm = require(path.join(base, "static/vendor/htm.js"));
global.React = React;
global.ReactDOM = ReactDOM;
global.htm = htm;
global.html = htm.bind(React.createElement);
global.window = global;
global.location = { hash: "#/dashboard" };

require(path.join(base, "static/js/api.js"));
require(path.join(base, "static/js/views/login.js"));
require(path.join(base, "static/js/views/dashboard.js"));
require(path.join(base, "static/js/views/companies.js"));
require(path.join(base, "static/js/views/activity.js"));
require(path.join(base, "static/js/views/factors.js"));
require(path.join(base, "static/js/views/calculation.js"));
require(path.join(base, "static/js/views/quotas.js"));
require(path.join(base, "static/js/views/reports.js"));

console.log("views keys:", Object.keys(global.views || {}));
console.log("LoginView type:", typeof (global.views && global.views.LoginView));
console.log("DashboardView type:", typeof (global.views && global.views.DashboardView));

const el = React.createElement(global.views.LoginView, {});
console.log("createElement(LoginView) ok, type:", el.type.name);

const el2 = html`<div className="x">hello ${"world"}</div>`;
console.log("htm template ok, type:", el2.type, "children:", el2.props.children);
