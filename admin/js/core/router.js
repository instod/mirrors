// 路由层：视图注册表 + routeTo + 导航/事件委托。
import { state } from "./state.js";
import { $, empty } from "./dom.js";

export const routes = {};
export function register(name, fn) { routes[name] = fn; }

// 懒加载注册表：routeTo 时动态 import 视图模块（import 即 register），首屏只载当前路由
const loaders = {};
const loadFailed = {};
export function registerLazy(name, loader) { loaders[name] = loader; }
async function ensureRoute(name) {
  if (routes[name] || !loaders[name]) return;
  try {
    await loaders[name]();
    delete loadFailed[name];
  } catch (e) {
    console.error("视图加载失败:", name, e);
    loadFailed[name] = e;
  }
}

// 路由 -> 顶栏标题/副标
export const ROUTE_META = {
  dashboard:   ["首页", "运行总览"],
  loop:        ["闭环健康", "Admission · resume · daemon"],
  alerts:      ["告警中心", "同步失败 · 磁盘 · CPU · 内存"],
  mirrors:     ["镜像管理", "元数据 · 同步 · 健康分"],
  sources:     ["系统/软件源", "os_mirrors · soft_mirrors"],
  browse:      ["文件浏览", "镜像目录结构"],
  sync:        ["同步状态", "实时同步进度"],
  synchistory: ["同步历史", "历史同步记录"],
  crontab:     ["定时任务", "同步调度机 crontab"],
  ansible:     ["Ansible", "批量执行与剧本管理"],
  nodes:       ["入站节点池", "bnode1–30 · Admission 共帽"],
  monitor:     ["主机监控", "CPU / 内存 / 网络"],
  traffic:     ["流量观测", "出站下载 · 入站同步"],
  storage:     ["存储管理", "/hpcfs/fpublic · 镜像容量"],
  firewall:    ["防火墙", "firewalld 端口放行"],
  upstreams:   ["上游源", "纳管同步上游 · 延迟探测"],
  logs:        ["日志中心", "登录 · 审计 · 访问 · 同步 · 运维日报"],
  settings:    ["系统设置", "账号 · 告警阈值 · 钉钉通知"],
};

export const LOG_TABS = {
  login:  ["登录日志", "后台登录 / 失败 / 退出"],
  audit:  ["操作审计", "管理端写操作审计（全量）"],
  access: ["访问日志", "mirrors.tibhpc.net · IP / 镜像源 / 下载"],
  sync:   ["同步日志", "镜像同步脚本输出"],
  daily:  ["运维日报", "定时汇总 · 按日回溯"],
};
export const LOG_TAB_IDS = Object.keys(LOG_TABS);

export const SETTINGS_SECTIONS = {
  account:   ["账号", "登录身份"],
  threshold: ["告警阈值", "磁盘 / CPU / 上游"],
  policy:    ["通知策略", "事件推送与冷却"],
  dingtalk:  ["钉钉通道", "Webhook 与关键词"],
};
export const SETTINGS_SECTION_IDS = Object.keys(SETTINGS_SECTIONS);

// 已下线路由 → 新入口（旧书签 / 外链兼容）
const ROUTE_REDIRECT = {
  health: "mirrors",
  users: "settings/account",
  agent: "dashboard",
};

let currentRoute = null;
let currentHash = null;

function parseRoute(raw) {
  let name = (raw || "").replace(/^#\/?/, "").replace(/^\//, "");
  if (ROUTE_REDIRECT[name]) {
    if (name === "health") window._mirrorInitFilter = "attention";
    name = ROUTE_REDIRECT[name];
  }
  // logs / logs/<tab>
  if (name === "logs" || name.startsWith("logs/")) {
    let tab = name === "logs" ? "" : name.slice(5);
    if (!LOG_TAB_IDS.includes(tab)) {
      tab = window._logsTab || localStorage.getItem("admin_logs_tab") || "login";
      if (!LOG_TAB_IDS.includes(tab)) tab = "login";
    }
    window._logsTab = tab;
    return { name: "logs", tab, section: null, hash: "logs/" + tab };
  }
  // settings / settings/<section>
  if (name === "settings" || name.startsWith("settings/")) {
    let section = name === "settings" ? "" : name.slice(9);
    if (section && !SETTINGS_SECTION_IDS.includes(section)) section = "";
    if (section) window._settingsSection = section;
    return {
      name: "settings",
      tab: null,
      section: section || null,
      hash: section ? "settings/" + section : "settings",
    };
  }
  return { name, tab: null, section: null, hash: name };
}

function syncNavActive(name, tab, section) {
  const hash = tab ? `${name}/${tab}` : (section ? `${name}/${section}` : name);
  document.querySelectorAll(".nav-item[data-route]").forEach(n => {
    const r = n.dataset.route;
    let on = false;
    if (r === hash) on = true;
    else if (r === name && !tab && !section && !r.includes("/")) on = true;
    else if (name === "settings" && r === "settings" && !section && hash === "settings") on = true;
    n.classList.toggle("active", on);
  });
  document.querySelectorAll(".nav-fly").forEach(fly => {
    const childOn = [...fly.querySelectorAll(".nav-item[data-route]")].some(el => el.classList.contains("active"));
    const toggle = fly.querySelector(".nav-fly-toggle");
    toggle?.classList.toggle("active", childOn);
    if (childOn) fly.classList.add("open");
    else fly.classList.remove("open");
  });
}

let _seq = 0;
export async function routeTo(name, force = false) {
  const parsed = parseRoute(name);
  name = parsed.name;
  const tab = parsed.tab;
  const hash = parsed.hash;

  const section = parsed.section;
  if (name === currentRoute && hash === currentHash && !force) return;
  const seq = ++_seq;
  await ensureRoute(name);
  if (seq !== _seq) return; // 期间又切了路由，丢弃本次
  currentRoute = name; state.currentRoute = name;
  currentHash = hash;
  syncNavActive(name, tab, section);
  // 销毁旧图表
  Object.values(state.charts).forEach(c => { try { c.destroy(); } catch {} });
  Object.keys(state.charts).forEach(k => delete state.charts[k]);
  const c = $("viewContent");
  // 清理上一页的定时器/监听（视图须在路由函数开头注册 c._cleanup）
  if (typeof c._cleanup === "function") { try { c._cleanup(); } catch {} c._cleanup = null; }
  // 顶栏标题
  let meta = ROUTE_META[name] || [name, ""];
  if (name === "logs" && tab && LOG_TABS[tab]) meta = LOG_TABS[tab];
  if (name === "settings" && section && SETTINGS_SECTIONS[section]) meta = SETTINGS_SECTIONS[section];
  $("topbarTitle").textContent = meta[0];
  $("topbarSub").textContent = meta[1] || "";
  // 渲染（try/catch 兜底：视图内部抛错时展示错误态 + 重试，避免半渲染页面卡死）
  const fn = routes[name];
  c.innerHTML = "";
  c.classList.remove("anim-in"); void c.offsetWidth; c.classList.add("anim-in");
  if (fn) {
    try {
      const ret = fn(c);
      // async 视图（mirrors/browse/health）await 后的异常不会被同步 try/catch 接住，避免 unhandled rejection
      if (ret && typeof ret.catch === "function") ret.catch(err => console.error("视图异步异常:", name, err));
    }
    catch (err) {
      console.error("视图渲染异常:", name, err);
      if (typeof c._cleanup === "function") { try { c._cleanup(); } catch {} c._cleanup = null; }
      c.innerHTML = empty("页面渲染异常", `${name} 视图出错：${err && err.message || err}`, "#i-warn",
        '<button class="btn primary" id="routeErrRetryBtn" style="margin-top:12px">重试</button>');
      c.querySelector("#routeErrRetryBtn")?.addEventListener("click", () => routeTo(hash || name, true));
    }
  }
  else if (loadFailed[name]) {
    // 模块加载失败（区别于路由不存在）：提供重试入口
    c.innerHTML = empty("页面加载失败", `视图 ${name} 模块加载出错，请检查网络后重试`, "#i-warn",
      '<button class="btn primary" id="routeRetryBtn" style="margin-top:12px">重新加载</button>');
    c.querySelector("#routeRetryBtn")?.addEventListener("click", () => routeTo(hash || name, true));
  }
  else c.innerHTML = empty("未找到页面", `路由 ${name} 不存在`, "#i-warn");
  $("viewScroll").scrollTop = 0;
  location.hash = "#/" + hash;
}

// 导航点击（sidebar data-route + 日志中心下拉）
export function initNav() {
  $("sidebarNav").addEventListener("click", (e) => {
    const toggle = e.target.closest("[data-nav-toggle], .nav-fly-toggle");
    if (toggle && toggle.closest(".nav-fly")) {
      e.preventDefault();
      const fly = toggle.closest(".nav-fly");
      const willOpen = !fly.classList.contains("open");
      document.querySelectorAll(".nav-fly.open").forEach(el => {
        if (el !== fly) el.classList.remove("open");
      });
      fly.classList.toggle("open", willOpen);
      return;
    }
    const item = e.target.closest(".nav-item[data-route]");
    if (item) {
      if (item.classList.contains("nav-fly-item")) {
        item.closest(".nav-fly")?.classList.add("open");
      }
      routeTo(item.dataset.route);
    }
  });
  // 折叠侧栏时：点外侧收起下拉
  document.addEventListener("click", (e) => {
    if (!document.body.classList.contains("nav-collapsed")) return;
    if (e.target.closest(".nav-fly")) return;
    document.querySelectorAll(".nav-fly.open").forEach(el => el.classList.remove("open"));
  });
  window.addEventListener("hashchange", () => {
    const raw = (location.hash || "").replace(/^#\//, "");
    if (raw && raw !== currentHash) routeTo(raw);
  });
}

// 事件委托：data-action="name" 触发 action("name", fn)，替代 inline onclick 全局污染
window._actions = {};
export function action(name, fn) { window._actions[name] = fn; }
export function initDelegation() {
  const c = $("viewContent");
  if (!c || c._delegated) return;
  c._delegated = true;
  c.addEventListener("click", (e) => {
    const el = e.target.closest("[data-action]");
    if (!el) return;
    const h = window._actions[el.dataset.action];
    if (h) h(el, e);
  });
}
