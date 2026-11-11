// 组件工厂层 v3：全部输出 HTML 字符串，视图拼模板用。
// —— 与 v2 API 完全兼容（Icon/Badge/statusPill/Card/statCard/Table/Field/Input/Select/Textarea/Switch/SearchBox/Tabs/Pager/PBar/Toolbar 签名不变）；
// —— 新增：Tip/Callout/Collapse/Dropdown/MenuItem/Segmented/Kv/Ring/Steps/Avatar/CopyBtn；
// —— 增强：Badge(title)、Card(icon/foot)、Table(dense/striped/col.cls)、Field(req)、Input(id/max/step/maxlength)、Switch(disabled)、Tabs(icon/disabled)、statCard(act)、SearchBox(w)。
import { esc, toast } from "./dom.js";
import { STATUS_LABEL } from "./format.js";

// ---------- 图标 ----------
export function Icon(name, cls = "") {
  return `<svg class="${cls}" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><use href="#i-${name}"/></svg>`;
}

// ---------- 徽章 ----------
// tone: brand | ok | warn | danger | info | purple | muted(默认)
export function Badge(text, tone = "", { dot = false, pulse = false, title = "" } = {}) {
  return `<span class="badge ${tone} ${pulse ? "pulse" : ""}"${title ? ` title="${esc(title)}"` : ""}>${dot ? '<i class="bdot"></i>' : ""}${esc(text)}</span>`;
}

// 同步状态胶囊（badge 实现）
const STATUS_TONE = { success: "ok", syncing: "info", pending: "warn", fail: "danger", timeout: "info", none: "" };
export function statusPill(s) {
  const t = STATUS_TONE[s] ?? "";
  return Badge(STATUS_LABEL[s] || s || "未知", t, { dot: true, pulse: s === "syncing" });
}

// ---------- 卡片 ----------
export function Card({ title = "", sub = "", icon = "", right = "", body = "", foot = "", tight = false, cls = "", id = "" } = {}) {
  const head = (title || right) ? `<div class="card-head">${icon ? `<span class="card-ico">${Icon(icon)}</span>` : ""}${title ? `<h3>${esc(title)}</h3>` : ""}${sub ? `<span class="sub">${esc(sub)}</span>` : ""}${right ? `<div class="right">${right}</div>` : ""}</div>` : "";
  return `<div class="card ${cls}"${id ? ` id="${esc(id)}"` : ""}>${head}<div class="card-body ${tight ? "tight" : ""}">${body}</div>${foot ? `<div class="card-foot">${foot}</div>` : ""}</div>`;
}

// 统计卡：tone 决定图标底色（ok/warn/danger/info/accent/purple/brand）；act 可选，挂 data-action 供事件委托
export function statCard(label, value, { sub = "", tone = "", icon = "dash", act = "" } = {}) {
  return `<div class="stat${act ? " clickable" : ""}"${act ? ` data-action="${esc(act)}"` : ""}>
    <div class="stat-ico ${tone}">${Icon(icon)}</div>
    <div><div class="stat-val">${value ?? "--"}</div><div class="stat-label">${esc(label)}${sub ? ` · <span class="stat-sub">${esc(sub)}</span>` : ""}</div></div>
  </div>`;
}

// ---------- 表格 ----------
// cols: [{t, w, r(右对齐), cls}]；opts: emptyHtml / dense / striped / cls
export function Table(cols, rowsHtml, { emptyHtml = "", dense = false, striped = false, cls = "" } = {}) {
  const head = cols.map(c => `<th${c.w ? ` style="width:${c.w}"` : ""} class="${c.r ? "num" : ""} ${c.cls || ""}">${esc(c.t)}</th>`).join("");
  const tcls = `tbl ${dense ? "dense" : ""} ${striped ? "striped" : ""} ${cls}`.trim();
  return `<div class="tbl-wrap"><table class="${tcls}"><thead><tr>${head}</tr></thead><tbody>${rowsHtml || `<tr><td colspan="${cols.length}">${emptyHtml}</td></tr>`}</tbody></table></div>`;
}

// ---------- 表单 ----------
// req: label 后加红星；hint 支持 html:true 传富文本（自行保证安全）
export function Field(label, inputHtml, hint = "", { req = false, html = false } = {}) {
  return `<div class="field"><label>${esc(label)}${req ? '<i class="req">*</i>' : ""}</label>${inputHtml}${hint ? `<div class="hint">${html ? hint : esc(hint)}</div>` : ""}</div>`;
}
export function Input(name, val, opts = {}) {
  return `<input class="input ${opts.mono ? "mono" : ""}" name="${esc(name)}"${opts.id ? ` id="${esc(opts.id)}"` : ""} value="${esc(val == null ? "" : val)}" placeholder="${esc(opts.placeholder || "")}"${opts.type ? ` type="${opts.type}"` : ""}${opts.required ? " required" : ""}${opts.disabled ? " disabled" : ""}${opts.min != null ? ` min="${opts.min}"` : ""}${opts.max != null ? ` max="${opts.max}"` : ""}${opts.step != null ? ` step="${opts.step}"` : ""}${opts.maxlength != null ? ` maxlength="${opts.maxlength}"` : ""}${opts.autofocus ? " autofocus" : ""}>`;
}
export function Select(name, options, val, opts = {}) {
  return `<select class="select" name="${esc(name)}"${opts.id ? ` id="${esc(opts.id)}"` : ""}${opts.disabled ? " disabled" : ""}>${options.map(o => `<option value="${esc(o.v)}"${String(o.v) === String(val) ? " selected" : ""}${o.disabled ? " disabled" : ""}>${esc(o.t)}</option>`).join("")}</select>`;
}
export function Textarea(name, val, opts = {}) {
  return `<textarea class="textarea ${opts.mono ? "mono" : ""}" name="${esc(name)}"${opts.id ? ` id="${esc(opts.id)}"` : ""} placeholder="${esc(opts.placeholder || "")}" rows="${opts.rows || 3}"${opts.disabled ? " disabled" : ""} spellcheck="false">${esc(val == null ? "" : val)}</textarea>`;
}
export function Switch(name, checked, label = "", { disabled = false } = {}) {
  return `<label class="switch${disabled ? " disabled" : ""}" title="${esc(label)}"><input type="checkbox" name="${esc(name)}"${checked ? " checked" : ""}${disabled ? " disabled" : ""}><span class="track"></span></label>`;
}
export function SearchBox(name = "q", val = "", ph = "搜索…", { w = "" } = {}) {
  // 同时输出 id 与 name：既有视图用 querySelector('[name=...]')，也有用 $(id) 取元素
  return `<div class="search-box">${Icon("search")}<input class="input" id="${name}" name="${name}" value="${esc(val || "")}" placeholder="${esc(ph)}"${w ? ` style="width:${w}"` : ""}></div>`;
}

// 分段选择器（radio 组）：options [{v, t}]
export function Segmented(name, options, val) {
  return `<div class="segmented">${options.map(o => `<label><input type="radio" name="${esc(name)}" value="${esc(o.v)}"${String(o.v) === String(val) ? " checked" : ""}><span>${esc(o.t)}</span></label>`).join("")}</div>`;
}

// ---------- 标签页 ----------
// tabs: [{id, label, count, icon, disabled}]
export function Tabs(tabs, active) {
  return `<div class="tabs">${tabs.map(t => `<button class="tab ${t.id === active ? "active" : ""}" data-tab="${esc(t.id)}"${t.disabled ? " disabled" : ""}>${t.icon ? Icon(t.icon) : ""}${esc(t.label)}${t.count != null ? ` (${t.count})` : ""}</button>`).join("")}</div>`;
}

// ---------- 分页 ----------
export function Pager(page, totalPages, total, perPage) {
  if (totalPages <= 1 && total <= perPage) return total != null ? `<div class="pager"><span class="pg-info">共 ${total} 条</span></div>` : "";
  const btns = [];
  const win = 2;
  let lo = Math.max(1, page - win), hi = Math.min(totalPages, page + win);
  if (lo > 1) { btns.push(pgBtn(1, page)); if (lo > 2) btns.push(`<span class="muted">…</span>`); }
  for (let i = lo; i <= hi; i++) btns.push(pgBtn(i, page));
  if (hi < totalPages) { if (hi < totalPages - 1) btns.push(`<span class="muted">…</span>`); btns.push(pgBtn(totalPages, page)); }
  return `<div class="pager">
    <span class="pg-info">共 ${total ?? ""} 条 · 第 ${page}/${totalPages} 页</span>
    <button class="pg-btn" data-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>‹</button>
    ${btns.join("")}
    <button class="pg-btn" data-page="${page + 1}" ${page >= totalPages ? "disabled" : ""}>›</button>
  </div>`;
}
function pgBtn(i, cur) {
  return `<button class="pg-btn ${i === cur ? "cur" : ""}" data-page="${i}">${i}</button>`;
}

// ---------- 进度 ----------
export function PBar(pct, tone = "") {
  const p = Math.max(0, Math.min(100, Number(pct) || 0));
  return `<div class="pbar ${tone}"><i style="width:${p}%"></i></div>`;
}

// 环形进度（SVG）：label 缺省显示百分比
export function Ring(pct, { tone = "", size = 62, label = null } = {}) {
  const p = Math.max(0, Math.min(100, Number(pct) || 0));
  const C = 2 * Math.PI * 26;
  return `<div class="ring ${tone}" style="--ring-size:${size}px" role="img" aria-label="${p}%">
    <svg viewBox="0 0 64 64"><circle class="ring-bg" cx="32" cy="32" r="26"/><circle class="ring-fg" cx="32" cy="32" r="26" stroke-dasharray="${C.toFixed(2)}" stroke-dashoffset="${(C * (1 - p / 100)).toFixed(2)}"/></svg>
    <span class="ring-val">${label != null ? esc(label) : Math.round(p) + "%"}</span></div>`;
}

// ---------- 工具条 ----------
export function Toolbar(leftHtml = "", rightHtml = "") {
  return `<div class="toolbar">${leftHtml ? `<div class="row wrap">${leftHtml}</div>` : "<span></span>"}${rightHtml ? `<div class="row wrap">${rightHtml}</div>` : ""}</div>`;
}

// ---------- 提示 / 告示 ----------
// 纯 CSS 悬浮提示：inner 为触发元素 HTML
export function Tip(inner, tip, { pos = "top" } = {}) {
  return `<span class="tip ${pos}" data-tip="${esc(tip)}">${inner}</span>`;
}

// 行内告示条：msg 默认转义，html:true 时原样输出（调用方保证安全）
const CALLOUT_ICON = { info: "info", warn: "warn", danger: "warn", ok: "check" };
export function Callout(msg, { tone = "info", title = "", html = false } = {}) {
  return `<div class="callout ${tone}">${Icon(CALLOUT_ICON[tone] || "info")}<div class="callout-bd">${title ? `<b>${esc(title)}</b>` : ""}${html ? msg : esc(msg)}</div></div>`;
}

// ---------- 折叠面板 ----------
export function Collapse(title, bodyHtml, { open = false, right = "", cls = "" } = {}) {
  return `<details class="collapse ${cls}"${open ? " open" : ""}>
    <summary><span class="collapse-t">${title}</span>${right ? `<span class="collapse-r">${right}</span>` : ""}${Icon("chev-d", "collapse-chev")}</summary>
    <div class="collapse-bd">${bodyHtml}</div>
  </details>`;
}

// ---------- 下拉菜单 ----------
// 纯 CSS（hover / focus-within 展开）；菜单项用 MenuItem 生成，事件走 data-action 委托
export function Dropdown(btnHtml, menuHtml, { align = "right", cls = "" } = {}) {
  return `<div class="dd ${align} ${cls}"><button type="button" class="dd-btn">${btnHtml}</button><div class="dd-menu" role="menu">${menuHtml}</div></div>`;
}
export function MenuItem(text, { icon = "", danger = false, action = "", disabled = false } = {}) {
  return `<button type="button" class="dd-item${danger ? " danger" : ""}"${action ? ` data-action="${esc(action)}"` : ""}${disabled ? " disabled" : ""}>${icon ? Icon(icon) : ""}${esc(text)}</button>`;
}

// ---------- 键值描述 ----------
// pairs: [{k, v}]；v 需富文本时用 {k, html}
export function Kv(pairs, { cols = 2 } = {}) {
  return `<div class="kv" style="--kv-cols:${cols}">${(pairs || []).map(p =>
    `<div class="kv-k">${esc(p.k)}</div><div class="kv-v">${p.html != null ? p.html : esc(p.v ?? "-")}</div>`).join("")}</div>`;
}

// ---------- 步骤条 ----------
export function Steps(items, active = 0) {
  return `<div class="steps">${items.map((s, i) =>
    `<div class="step ${i < active ? "done" : i === active ? "cur" : ""}"><i>${i < active ? "✓" : i + 1}</i><span>${esc(s)}</span></div>`).join("")}</div>`;
}

// ---------- 头像 ----------
export function Avatar(text, tone = "") {
  const t = String(text || "?").trim();
  return `<span class="avatar ${tone}" title="${esc(t)}">${esc(t.slice(0, 2).toUpperCase())}</span>`;
}

// ---------- 复制按钮 ----------
// 依赖 data-copy 全局委托（模块加载时绑定一次，见文件尾）
export function CopyBtn(text, label = "复制", { cls = "btn sm" } = {}) {
  return `<button type="button" class="${cls}" data-copy="${esc(text)}">${Icon("copy")}${esc(label)}</button>`;
}
if (typeof document !== "undefined" && !document._copyDelegated) {
  document._copyDelegated = true;
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-copy]");
    if (!b) return;
    navigator.clipboard.writeText(b.getAttribute("data-copy") || "")
      .then(() => toast("已复制"))
      .catch(() => toast("复制失败", "error"));
  });
}
