// DOM/工具层：$、esc、md2html、toast、confirmBox、modal、skel、debounce、empty。
export const $ = (id) => document.getElementById(id);

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

// 极简 markdown -> HTML（先转义再替换语法，安全）：h1-h4/列表/粗体/斜体/行内code/引用/段落
export function md2html(md) {
  if (!md) return "";
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');
  const lines = String(md).replace(/\r/g, "").split("\n");
  let html = "", inUl = false, inOl = false;
  const closeList = () => { if (inUl) { html += "</ul>"; inUl = false; } if (inOl) { html += "</ol>"; inOl = false; } };
  for (const line of lines) {
    if (/^\s*$/.test(line)) { closeList(); continue; }
    let m;
    if ((m = line.match(/^####\s+(.*)$/))) { closeList(); html += `<h4>${inline(m[1])}</h4>`; }
    else if ((m = line.match(/^###\s+(.*)$/))) { closeList(); html += `<h4>${inline(m[1])}</h4>`; }
    else if ((m = line.match(/^#{1,2}\s+(.*)$/))) { closeList(); html += `<h3>${inline(m[1])}</h3>`; }
    else if ((m = line.match(/^>\s?(.*)$/))) { closeList(); html += `<blockquote>${inline(m[1])}</blockquote>`; }
    else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) { if (!inUl) { closeList(); html += "<ul>"; inUl = true; } html += `<li>${inline(m[1])}</li>`; }
    else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { if (!inOl) { closeList(); html += "<ol>"; inOl = true; } html += `<li>${inline(m[1])}</li>`; }
    else { closeList(); html += `<p>${inline(line)}</p>`; }
  }
  closeList();
  return html;
}

// Toast：tone = success | error | warn | info（默认 success）
const TOAST_ICON = {
  success: "#i-check", error: "#i-warn", warn: "#i-warn", info: "#i-info",
};
export function toast(msg, tone = "success") {
  if (tone === true) tone = "success";       // 兼容旧调用 toast(msg, true/false)
  if (tone === false) tone = "error";
  const box = $("toasts");
  const el = document.createElement("div");
  el.className = "toast " + tone;
  el.innerHTML = `<svg fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="${TOAST_ICON[tone] || "#i-info"}"/></svg><span>${esc(msg)}</span>`;
  box.appendChild(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 300); }, 2800);
  while (box.children.length > 5) box.firstChild.remove();
}

// 通用确认弹窗（返回 Promise<boolean>）
let _confirmResolve = null;
export function confirmBox(msg, title = "确认操作", okText = "确认") {
  return new Promise(resolve => {
    _confirmResolve = resolve;
    $("confirmMsg").textContent = msg;
    $("confirmTitle").textContent = title;
    $("confirmOk").textContent = okText;
    $("confirmBox").classList.add("show");
    setTimeout(() => $("confirmOk").focus(), 30);
  });
}
function _confirmClose(val) {
  $("confirmBox").classList.remove("show");
  const r = _confirmResolve; _confirmResolve = null;
  if (r) r(val);
}
// main.js 启动时绑定一次
export function initConfirm() {
  $("confirmOk").addEventListener("click", () => _confirmClose(true));
  $("confirmCancel").addEventListener("click", () => _confirmClose(false));
  $("confirmBox").addEventListener("click", (e) => { if (e.target === $("confirmBox")) _confirmClose(false); });
}

// ---------- 模态 ----------
export function openModal(title, bodyHtml, { wide = false, foot = null } = {}) {
  $("modalTitle").textContent = title;
  $("modalBody").innerHTML = bodyHtml;
  $("modalBox").classList.toggle("wide", wide);
  const footEl = $("modalFoot");
  footEl.innerHTML = "";
  footEl.style.display = foot ? "" : "none";
  if (foot) footEl.appendChild(foot);
  $("modal").classList.add("show");
  return $("modalBody");
}
export function closeModal() { $("modal").classList.remove("show"); $("modalBody").innerHTML = ""; }
export function initModal() {
  $("modalClose").addEventListener("click", closeModal);
  $("modal").addEventListener("click", (e) => { if (e.target === $("modal")) closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeModal(); _confirmClose(false); } });
}
// 常用底部：取消 + 确认（确认回调异步，自动 loading）
export function modalFoot(okLabel, onOk) {
  const frag = document.createElement("div");
  frag.className = "row right"; frag.style.display = "flex"; frag.style.gap = "9px";
  frag.innerHTML = `<button class="btn" data-mcancel>取消</button><button class="btn primary" data-mok>${esc(okLabel)}</button>`;
  frag.querySelector("[data-mcancel]").addEventListener("click", closeModal);
  frag.querySelector("[data-mok]").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true; btn.innerHTML = '<span class="spin"></span>处理中';
    try { await onOk(); }
    catch (err) { btn.disabled = false; btn.textContent = okLabel; if (err && err.silent) return; if (err && err.message !== "CSRF") toast(err.message || "操作失败", "error"); }
  });
  return frag;
}

// ---------- 骨架 / 空态 / 防抖 ----------
export function skel(n = 3, h = 54) {
  return Array.from({ length: n }, () => `<div class="skel" style="height:${h}px;margin-bottom:10px"></div>`).join("");
}

export function debounce(fn, ms = 300) {
  let t = null;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

export function empty(title, desc = "", icon = "#i-inbox", actionHtml = "") {
  return `<div class="empty"><svg fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><use href="${icon}"/></svg>
    <div class="empty-title">${esc(title)}</div>${desc ? `<div class="empty-sub">${esc(desc)}</div>` : ""}${actionHtml}</div>`;
}
