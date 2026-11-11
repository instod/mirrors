// 视图：镜像仓库 —— 列表/筛选/搜索/CRUD 模态/手动同步。
import { register, routeTo } from "../core/router.js";
import { api } from "../core/api.js";
import { $, esc, toast, confirmBox, skel, debounce, openModal, closeModal, modalFoot, empty } from "../core/dom.js";
import { statusPill } from "../core/components.js";
import { Icon, Badge, Pager, SearchBox, Tabs, Table, Field, Input, Textarea, Switch, Toolbar } from "../core/components.js";
import { state } from "../core/state.js";

// ---- 模块私有状态 ----
let _alive = false, pendingFilter = null;
let _v = { q: "", filter: "", page: 1, pageSize: 12 };
let _healthMap = {}; // key -> { score, days }

function scoreTone(sc) {
  if (sc == null) return "";
  if (sc >= 85) return "ok";
  if (sc >= 70) return "warn";  // 与「需关注 <70」对齐：70–84 警告，≥85 正常
  return "danger";
}

async function loadHealthMap() {
  try {
    const d = await (await api("/mirrors/health")).json();
    const map = {};
    (d.items || []).forEach(it => { map[it.key] = { score: it.score, days: it.days }; });
    _healthMap = map;
  } catch { _healthMap = {}; }
}

// ---- 列表 ----
async function loadMirrors() {
  const tb = $("tableBody"); if (!tb) return;
  tb.innerHTML = `<tr><td colspan="9" style="padding:0">${skel(6)}</td></tr>`;
  try {
    const r = await api("/mirrors"); if (r.status === 401) return;
    state.mirrorsCache = await r.json();
    await loadHealthMap();
    renderList();
  } catch (e) { tb.innerHTML = `<tr><td colspan="9">${empty("加载失败", e.message, "#i-warn")}</td></tr>`; }
}

function renderList() {
  const tb = $("tableBody"); if (!tb) return;
  let items = state.mirrorsCache || [];
  if (_v.filter === "attention") {
    items = items.filter(m => {
      const h = _healthMap[m.key];
      return m.status === "fail" || m.status === "timeout" || (h && h.score < 70);
    });
    items = items.slice().sort((a, b) => (_healthMap[a.key]?.score ?? 999) - (_healthMap[b.key]?.score ?? 999));
  } else if (_v.filter) {
    items = items.filter(m => m.status === _v.filter);
  }
  if (_v.q) {
    const q = _v.q;
    items = items.filter(m => (m.key || "").toLowerCase().includes(q) || (m.name || "").toLowerCase().includes(q) || (m.description || "").toLowerCase().includes(q));
  }
  const total = items.length;
  const totalPages = Math.max(1, Math.ceil(total / _v.pageSize));
  if (_v.page > totalPages) _v.page = totalPages;
  const start = (_v.page - 1) * _v.pageSize;
  const pageItems = items.slice(start, start + _v.pageSize);
  const cnt = $("mCount"); if (cnt) cnt.textContent = total ? `共 ${total} 个` : "";
  const pg = $("mPager"); if (pg) pg.innerHTML = Pager(_v.page, totalPages, total, _v.pageSize);
  if (!total) { tb.innerHTML = `<tr><td colspan="9">${empty((_v.q || _v.filter) ? "没有匹配的镜像" : "暂无镜像", (_v.q || _v.filter) ? "调整搜索或筛选条件试试" : "点击右上角「新增镜像」创建", "#i-db")}</td></tr>`; return; }
  tb.innerHTML = pageItems.map(it => {
    const h = _healthMap[it.key];
    const scoreHtml = h
      ? Badge(h.score + " 分", scoreTone(h.score), { title: h.days == null ? "无同步时间" : (h.days <= 1 ? "新鲜" : h.days + " 天前") })
      : '<span class="muted small">—</span>';
    const lastSync = it.last_sync_date || "-";
    return `
    <tr data-key="${esc(it.key)}">
      <td class="mono">${esc(it.key)}</td>
      <td style="font-weight:600">${esc(it.name)}</td>
      <td class="dim small" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(it.description || "")}">${esc(it.description || "-")}</td>
      <td>${statusPill(it.status)}</td>
      <td class="num">${scoreHtml}</td>
      <td class="num small" title="${esc(lastSync)}">${esc(String(lastSync).slice(0, 16))}</td>
      <td class="num">${esc(it.total_size_gb || "-")}</td>
      <td>${it.is_system ? Badge("系统源", "purple") : Badge("登记", "brand")}</td>
      <td><div class="row-act">
        <button class="icon-btn" data-act="sync" title="手动同步"><svg fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><use href="#i-refresh"/></svg></button>
        <button class="icon-btn" data-act="browse" title="文件浏览"><svg fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><use href="#i-folder"/></svg></button>
        <button class="icon-btn" data-act="history" title="同步历史"><svg fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><use href="#i-clock"/></svg></button>
        <button class="icon-btn" data-act="edit" title="编辑"><svg fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><use href="#i-edit"/></svg></button>
        <button class="icon-btn" data-act="del" title="删除" ${(it.key === "os" || it.key === "software") ? "disabled" : ""} style="${(it.key === "os" || it.key === "software") ? "opacity:.35" : "color:var(--danger)"}"><svg fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><use href="#i-trash"/></svg></button>
      </div></td>
    </tr>`;
  }).join("");
}

// ---- 手动同步：只提交触发，成败以镜像 status 为准（不再用等待次数/时长判超时） ----
async function doSync(key) {
  const it = state.mirrorsCache.find(x => x.key === key);
  if (!it) return;
  if (!(await confirmBox(`确认立即同步「${it.name}」？将在同步调度机后台执行。`, "手动同步", "开始同步"))) return;
  const safeKey = (typeof CSS !== "undefined" && CSS.escape) ? CSS.escape(key) : String(key).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  const btn = document.querySelector(`tr[data-key="${safeKey}"] button[data-act="sync"]`);
  const orig = btn ? btn.innerHTML : "";
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin" style="width:13px;height:13px;border:2px solid var(--brand-bd);border-top-color:var(--brand);border-radius:50%;animation:spin .7s linear infinite"></span>'; }
  try {
    const r = await api(`/mirrors/${encodeURIComponent(key)}/sync`, { method: "POST" });
    if (r.ok) {
      toast("已提交同步，状态以镜像「同步中/成功/失败」为准", "success");
      // 立即刷一次，让行状态变成 syncing
      await loadMirrors();
      const poll = async () => {
        if (!_alive) return;
        await loadMirrors();
        const cur = (state.mirrorsCache || []).find(x => x.key === key);
        if (!cur) return;
        if (cur.status === "syncing") {
          setTimeout(poll, 5000); // 一直跟到终态，不设次数上限
          return;
        }
        const ok = cur.status === "success";
        toast(
          `「${cur.name}」同步${ok ? "完成" : cur.status === "fail" ? "失败" : "已结束"}`,
          ok ? "success" : "warn"
        );
      };
      setTimeout(poll, 2000);
    } else {
      const d = await r.json().catch(() => ({}));
      toast(d.error || "提交失败", "error");
    }
  } catch (e) { toast("提交失败：" + e.message, "error"); }
  finally { if (btn) { btn.disabled = false; btn.innerHTML = orig; } }
}

// ---- 新增/编辑模态 ----
function formHtml(it) {
  const s = it || {};
  // status 由表单隐藏字段携带；编辑时后端会强制保留原状态，新增默认 pending
  const statusVal = it ? (s.status || "pending") : "pending";
  return `<div class="form-grid">
    ${Field("标识 Key（新增后不可改）", Input("key", s.key, { placeholder: "英文/数字/_/-", required: true, disabled: !!it }), "")}
    ${Field("名称", Input("name", s.name, { placeholder: "显示名", required: true }))}
    <div class="span2">${Field("描述", Input("description", s.description))}</div>
    ${Field("相对路径 URL", Input("url", s.url, { placeholder: "例如 pdb-redo/" }), "须以 / 结尾")}
    ${it ? Field("当前状态", `<div class="row">${statusPill(statusVal)}<span class="small muted">由同步结果自动更新，不可手改</span></div>`) : ""}
    <input type="hidden" name="status" value="${esc(statusVal)}" />
    <div class="span2">${Field("同步命令 sync_cmd", Textarea("sync_cmd", s.sync_cmd != null ? JSON.stringify(s.sync_cmd) : "[]", { mono: true, rows: 2, placeholder: '["rsync","-avz","rsync://源/","/目标/"]' }), "JSON 数组，每元素一个参数")}</div>
    <div class="span2">${Field("同步脚本 sync_script", Input("sync_script", s.sync_script, { placeholder: "/hpcfs/fpublic/manage/baiwx/script/ncbi.py", mono: true }), "远程脚本绝对路径，优先于 sync_cmd（在同步调度机执行）")}</div>
    <div class="span2">${Field("定时调度", `<div class="small muted" style="line-height:1.7">调度以同步调度机 root crontab 为准。请到「定时任务」页编辑。</div>`)}</div>
    <div class="span2">${Field("系统源", `<div class="row">${Switch("is_system", !!s.is_system)}<span class="small muted">勾选表示 os/software 类系统源</span></div>`)}</div>
  </div><div class="login-err" id="formError" style="margin-top:4px"></div>`;
}

function readForm(body) {
  const g = (n) => body.querySelector(`[name="${n}"]`);
  return {
    key: g("key").value.trim(), name: g("name").value, description: g("description").value,
    url: g("url").value, status: (g("status") && g("status").value) || "pending", sync_cmd: g("sync_cmd").value,
    sync_script: g("sync_script").value.trim(),
    is_system: g("is_system").checked ? 1 : 0,
  };
}

function showFormError(body, msg) {
  const el = body.querySelector("#formError");
  if (el) { el.textContent = msg; el.style.display = msg ? "" : "none"; el.classList.toggle("show", !!msg); }
}

function openAdd() {
  state.editingKey = null;
  const body = openModal("新增镜像", formHtml(null), { wide: true, foot: modalFoot("保存", () => saveForm()) });
  body.querySelector('[name="key"]').readOnly = false;
}
function openEdit(key) {
  const it = state.mirrorsCache.find(x => x.key === key); if (!it) return;
  state.editingKey = key;
  const body = openModal("编辑镜像：" + it.name, formHtml(it), { wide: true, foot: modalFoot("保存", () => saveForm()) });
  const kEl = body.querySelector('[name="key"]'); if (kEl) kEl.disabled = true;
}

async function saveForm() {
  const body = $("modalBody");
  const payload = readForm(body);
  // 前端校验
  if (!payload.key) { showFormError(body, "Key 不能为空"); throw new Error("Key 不能为空"); }
  if (!payload.name.trim()) { showFormError(body, "名称不能为空"); throw new Error("名称不能为空"); }
  if (payload.url && !payload.url.endsWith("/")) { showFormError(body, "相对路径 URL 须以 / 结尾"); throw new Error("URL 格式错误"); }
  if (payload.sync_cmd.trim()) {
    try { const a = JSON.parse(payload.sync_cmd); if (!Array.isArray(a) || a.some(x => typeof x !== "string")) throw 0; }
    catch { showFormError(body, "同步命令必须是合法的 JSON 字符串数组"); throw new Error("sync_cmd 格式错误"); }
  }
  const url = state.editingKey ? `/mirrors/${encodeURIComponent(state.editingKey)}` : "/mirrors";
  const method = state.editingKey ? "PUT" : "POST";
  const r = await api(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  const d = await r.json();
  if (r.ok) { closeModal(); toast(state.editingKey ? "更新成功" : "新增成功"); await loadMirrors(); }
  else { showFormError(body, d.error || "保存失败"); throw new Error(d.error || "保存失败"); }
}

async function doDelete(key) {
  const it = state.mirrorsCache.find(x => x.key === key); if (!it) return;
  if (!(await confirmBox(`确认删除「${it.name}」(${key})？\n注意：删除后仍需手动移除 nginx 中对应的 location 配置。`, "删除镜像", "删除"))) return;
  try {
    const r = await api(`/mirrors/${encodeURIComponent(key)}`, { method: "DELETE" });
    const d = await r.json();
    if (r.ok) { toast("删除成功"); await loadMirrors(); } else toast(d.error || "删除失败", "error");
  } catch (e) { toast("删除失败：" + e.message, "error"); }
}

// ---- 路由 ----
register("mirrors", async function (c) {
  c._cleanup = () => { _alive = false; };
  const tabs = [
    { id: "", label: "全部" },
    { id: "attention", label: "需关注" },
    { id: "fail", label: "失败" },
    { id: "syncing", label: "同步中" },
    { id: "success", label: "成功" },
    { id: "pending", label: "待同步" },
  ];
  _v = { q: "", filter: "", page: 1, pageSize: 12 };
  _alive = true; // 须先于 await 置位：路由竞态时 cleanup 置 false 后不得被后续 await 覆盖回 true
  // dashboard 失败卡 / 健康度跳转带的初始筛选
  const initFilter = window._mirrorInitFilter || pendingFilter;
  window._mirrorInitFilter = pendingFilter = null;
  if (initFilter) _v.filter = initFilter;
  const initQ = window._mirrorInitQ;
  window._mirrorInitQ = null;
  if (initQ) _v.q = String(initQ).trim().toLowerCase();

  c.innerHTML = `
    ${Toolbar(
      SearchBox("mq", "", "按 Key / 名称 / 描述搜索…") + `<span class="muted small" id="mCount"></span>`,
      `<button class="btn primary" id="addBtn">${Icon("plus")}新增镜像</button>`
    )}
    <div class="small muted mb">需关注 = 同步失败或健康分 &lt;70；健康分 ≥85 正常 · 70–84 警告 · &lt;70 危险</div>
    <div class="card">
      <div class="card-body tight" style="padding:0 14px">${Tabs(tabs, _v.filter)}</div>
      <div class="card-body tight">
        ${Table(
          [{ t: "Key" }, { t: "名称" }, { t: "描述" }, { t: "状态" }, { t: "健康分", r: 1 }, { t: "上次同步", r: 1 }, { t: "大小", r: 1 }, { t: "类型" }, { t: "操作", w: "168px" }],
          "", { emptyHtml: skel(5) }
        )}
      </div>
      <div id="mPager"></div>
    </div>`;

  // 表格 tbody 引用（Table 输出后定位）
  const tbody = c.querySelector("table.tbl tbody"); tbody.id = "tableBody";
  // 筛选 tab
  c.querySelector(".tabs").addEventListener("click", e => {
    const t = e.target.closest(".tab"); if (!t) return;
    c.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    t.classList.add("active"); _v.filter = t.dataset.tab; _v.page = 1; renderList();
  });
  $("addBtn").addEventListener("click", openAdd);
  const si = c.querySelector('input[name="mq"]');
  if (_v.q) si.value = _v.q;
  si.addEventListener("input", debounce(() => { _v.q = si.value.trim().toLowerCase(); _v.page = 1; renderList(); }, 220));
  $("mPager").addEventListener("click", e => {
    const b = e.target.closest("[data-page]");
    if (b && !b.disabled) { _v.page = parseInt(b.dataset.page) || 1; renderList(); }
  });
  tbody.addEventListener("click", e => {
    const b = e.target.closest("button[data-act]"); if (!b) return;
    const key = b.closest("tr").dataset.key;
    if (b.dataset.act === "edit") openEdit(key);
    else if (b.dataset.act === "del") doDelete(key);
    else if (b.dataset.act === "sync") doSync(key);
    else if (b.dataset.act === "browse") { window._browseInitKey = key; routeTo("browse"); }
    else if (b.dataset.act === "history") { window._syncHistoryKey = key; routeTo("synchistory"); }
  });

  await loadMirrors();
});
