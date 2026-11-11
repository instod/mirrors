// 视图：同步历史 —— 7 天成功率/趋势图 + 各镜像最近同步 + 可筛选分页历史记录。
import { register } from "../core/router.js";
import { api } from "../core/api.js";
import { $, esc, empty, skel } from "../core/dom.js";
import { Icon, Pager, Table, Badge, Toolbar } from "../core/components.js";
import { fmtDur } from "../core/format.js";
import { poller } from "../core/poller.js";
import { state } from "../core/state.js";
import { ensureChart } from "../core/chart.js";

const SH_LABEL = { success: "成功", fail: "失败", skip: "跳过", syncing: "同步中", pending: "待同步", timeout: "超时" };
const SH_TONE = { success: "ok", fail: "danger", skip: "", syncing: "info", pending: "warn", timeout: "warn" };
const pill = (s) => Badge(SH_LABEL[s] || s, SH_TONE[s] ?? "", { dot: true, pulse: s === "syncing" });

register("synchistory", function (c) {
  c._cleanup = () => { if (c._stop) c._stop(); };
  let curPage = 1;

  c.innerHTML = `
    ${Toolbar('<span class="dim small">最近 7 天同步成功率与趋势 · 历史可按 Key / 状态筛选 · 统计每分钟刷新（记录表需手动筛选）</span>',
      `<button class="btn sm" id="shRefresh">${Icon("refresh")}刷新</button>`)}
    <div class="stats-grid mb" id="shStats" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">${'<div class="skel" style="height:66px"></div>'.repeat(4)}</div>
    <div class="card mb"><div class="card-head"><h3>近 7 天同步趋势</h3></div><div class="card-body"><canvas id="shTrend" height="80"></canvas></div></div>
    <div class="card mb"><div class="card-head"><h3>各镜像最近一次同步</h3></div><div class="card-body tight" id="shLatest">${skel(4)}</div></div>
    <div class="card">
      <div class="card-head wrap" style="flex-wrap:wrap">
        <h3>同步记录</h3>
        <div class="right row wrap">
          <input id="shKey" class="input" placeholder="按 Key 筛选" style="width:150px;padding:5px 10px;font-size:12.5px" />
          <select id="shStatus" class="select" style="width:120px;padding:5px 10px;font-size:12.5px">
            <option value="">全部状态</option><option value="success">成功</option><option value="fail">失败</option><option value="timeout">超时</option><option value="skip">跳过</option><option value="syncing">同步中</option>
          </select>
          <button class="btn sm primary" id="shFilter">${Icon("search")}筛选</button>
        </div>
      </div>
      <div class="card-body tight" id="shWrap">${Table(
        [{ t: "开始时间" }, { t: "镜像" }, { t: "状态" }, { t: "大小", r: 1 }, { t: "耗时", r: 1 }, { t: "节点" }, { t: "输出" }],
        "", { emptyHtml: skel(5) })}</div>
      <div class="row" style="padding:0 16px"><span id="shPage" style="flex:1"></span></div>
    </div>`;

  const tbody = c.querySelector("#shWrap table.tbl tbody"); tbody.id = "shTable";
  const css = getComputedStyle(document.documentElement), mk = (n) => css.getPropertyValue(n).trim();

  async function loadStats() {
    try {
      const d = await (await api("/sync/stats")).json();
      let succ = 0, fail = 0, skip = 0;
      (d.trend || []).forEach(t => { succ += t.success || 0; fail += t.fail || 0; skip += t.skip || 0; });
      const tot = succ + fail + skip, rate = tot ? Math.round(succ / tot * 100) : 0;
      const card = (label, val, tone, icon) => `<div class="stat"><div class="stat-ico ${tone}">${Icon(icon)}</div><div><div class="stat-val" style="color:var(--${tone === "" ? "text-dim" : tone})">${val}</div><div class="stat-label">${label}</div></div></div>`;
      $("shStats").innerHTML =
        card("同步成功", succ, "ok", "check") + card("同步失败", fail, "danger", "warn") +
        card("跳过", skip, "", "stop") +
        card("7 天成功率", rate + "%", rate >= 95 ? "ok" : rate >= 80 ? "warn" : "danger", "act");
      // 趋势图（独立降级：Chart 缺失/异常不影响统计卡与最近同步表）
      const trend = d.trend || [];
      try {
        if (!(await ensureChart())) throw new Error("Chart.js 未加载");
        if (state.charts.shTrend) { try { state.charts.shTrend.destroy(); } catch {} state.charts.shTrend = null; }
        state.charts.shTrend = new Chart($("shTrend"), {
          type: "bar",
          data: { labels: trend.map(t => (t.date || "").slice(5)), datasets: [
            { label: "成功", data: trend.map(t => t.success || 0), backgroundColor: mk("--ok"), borderRadius: 3, stack: "s" },
            { label: "失败", data: trend.map(t => t.fail || 0), backgroundColor: mk("--danger"), borderRadius: 3, stack: "s" },
            { label: "跳过", data: trend.map(t => t.skip || 0), backgroundColor: mk("--text-mute"), borderRadius: 3, stack: "s" }] },
          options: { responsive: true, plugins: { legend: { labels: { color: mk("--text-dim"), font: { size: 12 }, boxWidth: 12 } } },
            scales: { x: { stacked: true, ticks: { color: mk("--text-mute") }, grid: { display: false } }, y: { stacked: true, beginAtZero: true, ticks: { color: mk("--text-mute"), precision: 0 }, grid: { color: mk("--border-soft") } } } }
        });
      } catch { const cv = $("shTrend"); if (cv) cv.outerHTML = `<div class="empty" style="padding:20px">${empty("图表不可用", "Chart.js 未加载或渲染失败", "#i-warn")}</div>`; }
      // 最近一次同步
      const latest = d.latest || [];
      $("shLatest").innerHTML = latest.length
        ? Table([{ t: "Key" }, { t: "名称" }, { t: "状态" }, { t: "上次同步", r: 1 }, { t: "耗时", r: 1 }],
            latest.map(it => `<tr><td class="mono">${esc(it.key)}</td><td style="font-weight:600">${esc(it.name || it.key)}</td><td>${pill(it.status)}</td><td class="num">${esc(it.finished_at || it.started_at || "-")}</td><td class="num">${it.duration_sec != null ? fmtDur(it.duration_sec) : "-"}</td></tr>`).join(""))
        : empty("尚无同步记录", "", "#i-clock");
    } catch (e) { $("shStats").innerHTML = ""; $("shLatest").innerHTML = empty("统计加载失败", e.message, "#i-warn"); }
  }

  async function loadHistory(page) {
    curPage = page || 1;
    tbody.innerHTML = `<tr><td colspan="7" style="padding:14px">${skel(4)}</td></tr>`;
    try {
      const p = new URLSearchParams();
      const k = $("shKey").value.trim(); if (k) p.set("key", k);
      const st = $("shStatus").value; if (st) p.set("status", st);
      p.set("page", curPage);
      const d = await (await api("/sync/history?" + p.toString())).json();
      const items = d.items || [];
      tbody.innerHTML = items.length ? items.map(it => `<tr>
        <td class="num" style="white-space:nowrap">${esc(it.started_at || "-")}</td>
        <td><b>${esc(it.name || it.key)}</b><div class="mono small muted">${esc(it.key)}</div></td>
        <td>${pill(it.status)}</td>
        <td class="num">${esc(it.size || "-")}</td>
        <td class="num">${it.duration_sec != null ? fmtDur(it.duration_sec) : "-"}</td>
        <td class="mono small">${esc(it.node || "-")}</td>
        <td>${it.output ? `<details><summary style="cursor:pointer;color:var(--brand-text);font-size:12px">查看</summary><div class="codebox" style="margin-top:6px;max-height:200px">${esc(it.output)}</div></details>` : "-"}</td>
      </tr>`).join("") : `<tr><td colspan="7">${empty("没有匹配的记录", "", "#i-clock")}</td></tr>`;
      $("shPage").innerHTML = Pager(d.page || curPage, d.total_pages || 1, d.total || 0, 20);
    } catch (e) { tbody.innerHTML = `<tr><td colspan="7">${empty("加载失败", e.message, "#i-warn")}</td></tr>`; }
  }

  $("shRefresh").addEventListener("click", async () => { await loadStats(); await loadHistory(curPage); });
  $("shFilter").addEventListener("click", () => loadHistory(1));
  $("shKey").addEventListener("keydown", e => { if (e.key === "Enter") loadHistory(1); });
  $("shStatus").addEventListener("change", () => loadHistory(1));
  $("shPage").addEventListener("click", e => {
    const b = e.target.closest("[data-page]");
    if (b && !b.disabled) loadHistory(parseInt(b.dataset.page) || 1);
  });

  const initKey = window._syncHistoryKey;
  window._syncHistoryKey = null;
  if (initKey) $("shKey").value = initKey;

  loadStats();
  loadHistory(1);
  c._stop = poller(() => { if (state.currentRoute === "synchistory") loadStats(); }, 60000);
});
