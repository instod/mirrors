// 视图：同步任务 —— 进行中任务 + 全部镜像同步记录表 + 手动触发。
// 成败只看镜像 status，不按等待超时判定。
import { register } from "../core/router.js";
import { api } from "../core/api.js";
import { $, esc, toast, confirmBox, debounce, empty, skel } from "../core/dom.js";
import { Icon, Pager, SearchBox, Table, statusPill, Badge, Toolbar } from "../core/components.js";
import { state } from "../core/state.js";

function cmdPreview(cmd) {
  if (!cmd || !cmd.length) return '<span class="muted">-</span>';
  return `<span class="mono small dim" style="max-width:210px;display:inline-block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(JSON.stringify(cmd))}">${esc(cmd.join(" "))}</span>`;
}

register("sync", function (c) {
  const _sv = { q: "", page: 1, pageSize: 12 };
  let _all = [];
  let _activeCount = 0;
  let _alive = true;
  c._cleanup = () => { _alive = false; };

  c.innerHTML = `
    ${Toolbar('<span class="dim small">以镜像 status 为准 · 有进行中任务时 5 秒刷新，否则 30 秒</span>',
      `<button class="btn sm" id="syncRefresh">${Icon("refresh")}刷新</button>`)}
    <div class="card mb"><div class="card-head"><h3>进行中的任务</h3><span class="sub" id="syncActiveCnt"></span></div><div class="card-body tight" id="syncActive">${skel(2)}</div></div>
    <div class="card">
      <div class="card-head">
        <h3>同步配置与手动触发</h3><span class="sub" id="syncCnt"></span>
        <div class="right">${SearchBox("syncSearch", "", "搜索 Key / 名称…")}</div>
      </div>
      <div class="card-body tight" id="syncWrap">${Table(
        [{ t: "Key" }, { t: "名称" }, { t: "状态" }, { t: "大小", r: 1 }, { t: "上次同步", r: 1 }, { t: "同步命令" }, { t: "操作", w: "120px" }],
        "", { emptyHtml: skel(5) })}</div>
      <div id="syncPager"></div>
    </div>`;

  const tbody = c.querySelector("#syncWrap table.tbl tbody"); tbody.id = "syncTable";
  $("syncRefresh").addEventListener("click", () => load());

  function renderList() {
    const tb = $("syncTable"); if (!tb) return;
    let items = _all;
    if (_sv.q) items = items.filter(m => (m.key || "").toLowerCase().includes(_sv.q) || (m.name || "").toLowerCase().includes(_sv.q));
    const total = items.length, totalPages = Math.max(1, Math.ceil(total / _sv.pageSize));
    if (_sv.page > totalPages) _sv.page = totalPages;
    const start = (_sv.page - 1) * _sv.pageSize, page = items.slice(start, start + _sv.pageSize);
    $("syncCnt").textContent = total ? `共 ${total} 个` : "";
    $("syncPager").innerHTML = Pager(_sv.page, totalPages, total, _sv.pageSize);
    if (!total) { tb.innerHTML = `<tr><td colspan="7">${empty(_sv.q ? "没有匹配的镜像" : "暂无镜像", "", "#i-db")}</td></tr>`; return; }
    tb.innerHTML = page.map(m => `<tr>
      <td class="mono">${esc(m.key)}</td>
      <td style="font-weight:600">${esc(m.name)}</td>
      <td>${statusPill(m.status)}</td>
      <td class="num">${esc(m.total_size_gb || "-")}</td>
      <td class="num">${esc(m.last_sync_date || "-")}</td>
      <td>${m.sync_script
        ? `<span class="mono small" style="color:var(--brand-text)" title="${esc(m.sync_script)}">脚本 ${esc(m.sync_script.split("/").pop())}</span>`
        : cmdPreview(m.sync_cmd)}</td>
      <td>${(m.sync_script || (m.sync_cmd && m.sync_cmd.length))
        ? `<button class="btn sm primary" data-act="run" data-key="${esc(m.key)}">${Icon("play")}同步</button>`
        : Badge("未配置")}</td>
    </tr>`).join("");
  }

  async function load() {
    try {
      const [ra, rm] = await Promise.all([api("/sync/status"), api("/mirrors")]);
      const active = await ra.json(); _all = await rm.json();
      state.mirrorsCache = _all;
      _activeCount = Array.isArray(active) ? active.length : 0;
      $("syncActiveCnt").textContent = _activeCount ? `${_activeCount} 个任务` : "";
      $("syncActive").innerHTML = _activeCount
        ? Table([{ t: "Key" }, { t: "名称" }, { t: "状态" }, { t: "上次同步", r: 1 }],
            active.map(t => `<tr><td class="mono">${esc(t.key)}</td><td>${esc(t.name)}</td><td>${t.is_scanning ? Badge("扫描中", "info", { dot: true, pulse: true }) : statusPill(t.status)}</td><td class="num">${esc(t.last_sync_date || "-")}</td></tr>`).join(""))
        : empty("当前无进行中的同步任务", "所有任务均已完成", "#i-check");
      renderList();
    } catch (e) {
      $("syncActive").innerHTML = empty("加载失败", e.message, "#i-warn");
    }
  }

  async function syncNow(key) {
    if (!(await confirmBox(`确认立即同步「${key}」？将在同步调度机后台执行。`, "手动同步", "开始同步"))) return;
    try {
      const r = await api(`/mirrors/${encodeURIComponent(key)}/sync`, { method: "POST" });
      if (r.ok) {
        toast("已提交同步，请看上方「进行中」与镜像状态", "success");
        await load();
      } else {
        const d = await r.json().catch(() => ({}));
        toast(d.error || "提交失败", "error");
      }
    } catch (e) { toast("提交失败：" + e.message, "error"); }
  }

  const si = c.querySelector('input[name="syncSearch"]');
  si.addEventListener("input", debounce(() => { _sv.q = si.value.trim().toLowerCase(); _sv.page = 1; renderList(); }, 220));
  $("syncPager").addEventListener("click", e => {
    const b = e.target.closest("[data-page]");
    if (b && !b.disabled) { _sv.page = parseInt(b.dataset.page) || 1; renderList(); }
  });
  tbody.addEventListener("click", e => {
    const b = e.target.closest('button[data-act="run"]');
    if (b) syncNow(b.dataset.key);
  });

  load();
  (async () => {
    while (_alive) {
      const wait = _activeCount > 0 ? 5000 : 30000;
      await new Promise(r => setTimeout(r, wait));
      if (!_alive || state.currentRoute !== "sync") continue;
      if (document.visibilityState === "visible") {
        try { await load(); } catch {}
      }
    }
  })();
});
