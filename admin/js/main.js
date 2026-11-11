// 入口：视图懒加载注册（routeTo 时动态 import，首屏只载当前路由）+ 壳绑定 + 登录流程 + 主题/时钟。
import { registerLazy } from "./core/router.js";
registerLazy("dashboard",   () => import("./views/dashboard.js"));
registerLazy("loop",        () => import("./views/loop.js"));
registerLazy("alerts",      () => import("./views/alerts.js"));
registerLazy("mirrors",     () => import("./views/mirrors.js"));
registerLazy("sources",     () => import("./views/sources.js"));
registerLazy("browse",      () => import("./views/browse.js"));
registerLazy("sync",        () => import("./views/sync.js"));
registerLazy("synchistory", () => import("./views/synchistory.js"));
registerLazy("crontab",     () => import("./views/crontab.js"));
registerLazy("ansible",     () => import("./views/ansible.js"));
registerLazy("nodes",       () => import("./views/nodes.js"));
registerLazy("monitor",     () => import("./views/monitor.js"));
registerLazy("traffic",     () => import("./views/traffic.js"));
registerLazy("storage",     () => import("./views/storage.js"));
registerLazy("firewall",    () => import("./views/firewall.js"));
registerLazy("upstreams",   () => import("./views/upstreams.js"));
registerLazy("logs",        () => import("./views/logs.js"));
registerLazy("settings",    () => import("./views/settings.js"));

import { api, API, setUnauthorizedHandler } from "./core/api.js";
import { setCsrf, fetchAlertsCached } from "./core/state.js";
import { $, initConfirm, initModal } from "./core/dom.js";
import { routeTo, initNav, initDelegation } from "./core/router.js";
// 预取高频视图（空闲时拉取，不阻塞首屏）
if ("requestIdleCallback" in window) {
  requestIdleCallback(() => { import("./views/mirrors.js"); import("./views/sync.js"); }, { timeout: 5000 });
}

async function refreshNavAlertBadge() {
  const badge = $("navAlertBadge");
  if (!badge) return;
  try {
    const d = await fetchAlertsCached(api);
    const n = (d && (d.count ?? (d.alerts || []).length)) || 0;
    badge.textContent = n > 99 ? "99+" : String(n);
    badge.classList.toggle("show", n > 0);
  } catch {
    badge.classList.remove("show");
  }
}

// ========== 主题 ==========
function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  localStorage.setItem("admin_theme", t);
  const use = $("themeBtn")?.querySelector("use");
  if (use) use.setAttribute("href", t === "dark" ? "#i-sun" : "#i-moon");
}
function toggleTheme() {
  applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
}

// ========== 侧栏折叠 ==========
function applyNavCollapsed() {
  const collapsed = localStorage.getItem("admin_nav_collapsed") === "1";
  document.body.classList.toggle("nav-collapsed", collapsed);
  const use = $("collapseBtn")?.querySelector("use");
  if (use) use.setAttribute("href", collapsed ? "#i-chev-r" : "#i-chev-l");
  $("collapseBtn")?.setAttribute("title", collapsed ? "展开侧栏" : "收起侧栏");
}
function toggleNavCollapsed() {
  localStorage.setItem("admin_nav_collapsed", localStorage.getItem("admin_nav_collapsed") === "1" ? "0" : "1");
  applyNavCollapsed();
}

// ========== 当前用户 ==========
function setTopbarUser(user, via) {
  const el = $("topbarUser");
  if (!el) return;
  if (!user) { el.textContent = ""; el.removeAttribute("title"); return; }
  el.textContent = user;
  el.title = via ? `${user} · ${via}` : user;
}

// ========== 登录 ==========
async function checkLogin() {
  try {
    const r = await fetch(API("/check")); const d = await r.json();
    if (d.csrf_token) setCsrf(d.csrf_token);
    if (d.logged_in) setTopbarUser(d.user, d.login_via);
    return !!d.logged_in;
  } catch { return false; }
}
function showLogin() {
  $("loginPage").classList.add("show");
  setTopbarUser("");
}
function showAdmin() {
  $("loginPage").classList.remove("show");
  initDelegation();
  if (!location.hash) location.hash = "#/dashboard";
  else routeTo(location.hash.slice(2));
  refreshNavAlertBadge();
  if (!window._navAlertTimer) {
    window._navAlertTimer = setInterval(refreshNavAlertBadge, 60000);
  }
}

$("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const user = String(fd.get("username") || "").trim();
  const pw = String(fd.get("password") || "");
  const errEl = $("loginErr");
  errEl.classList.remove("show");
  if (!user || !pw) { errEl.textContent = "请输入账号和密码"; errEl.classList.add("show"); return; }
  const btn = $("loginSubmit") || e.target.querySelector("button[type=submit]");
  const old = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span><span>登录中…</span>';
  try {
    const r = await fetch(API("/login"), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: user, password: pw }) });
    if (r.ok) {
      const d = await r.json().catch(() => ({}));
      if (d.csrf_token) setCsrf(d.csrf_token);
      setTopbarUser(d.user || user, d.login_via || (user === "admin" ? "本地账号" : "AD 域认证"));
      e.target.reset();
      showAdmin();
    } else {
      const d = await r.json().catch(() => ({}));
      errEl.textContent = d.error || (r.status === 429 ? "登录尝试过多，请稍后再试" : "登录失败");
      errEl.classList.add("show");
    }
  } catch {
    errEl.textContent = "网络错误，请稍后重试";
    errEl.classList.add("show");
  } finally { btn.disabled = false; btn.innerHTML = old; }
});

// 密码显示/隐藏
$("loginPassToggle")?.addEventListener("click", () => {
  const inp = $("loginPass");
  const btn = $("loginPassToggle");
  if (!inp || !btn) return;
  const show = inp.type === "password";
  inp.type = show ? "text" : "password";
  btn.setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
  btn.setAttribute("aria-pressed", show ? "true" : "false");
  btn.title = show ? "隐藏密码" : "显示密码";
  const use = btn.querySelector("use");
  if (use) use.setAttribute("href", show ? "#i-eye-off" : "#i-eye");
});

// api() 401/会话过期 -> 弹登录
setUnauthorizedHandler(showLogin);

// ========== 绑定 ==========
initConfirm();
initModal();
initNav();

$("themeBtn").addEventListener("click", toggleTheme);
$("menuBtn").addEventListener("click", toggleNavCollapsed);
$("collapseBtn").addEventListener("click", toggleNavCollapsed);
$("homeBtn").addEventListener("click", () => window.open("/", "_blank"));
$("logoutBtn").addEventListener("click", async () => {
  try { await api("/logout", { method: "POST" }); } catch {}
  setCsrf("");
  showLogin();
});

// ========== 启动 ==========
// Chart.js 由 dashboard/monitor/storage/synchistory 按需 ensureChart()，首屏不再拉 ~205KB。
applyTheme(localStorage.getItem("admin_theme") || "light");
applyNavCollapsed();
(async () => {
  if (await checkLogin()) showAdmin(); else showLogin();
})();
