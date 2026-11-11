// 视图：存储管理 —— /hpcfs/fpublic 磁盘卡（对照告警阈值）+ 镜像容量占比。
import { register, routeTo } from "../core/router.js";
import { api } from "../core/api.js";
import { $, esc, empty, skel } from "../core/dom.js";
import { Icon, Badge, Toolbar, Card, PBar } from "../core/components.js";
import { parseSizeGB, fmtBytes } from "../core/format.js";
import { state } from "../core/state.js";
import { ensureChart } from "../core/chart.js";

const STORAGE_PATH = "/hpcfs/fpublic";

register("storage", function (c) {
  let diskThreshold = 85;

  c.innerHTML = `
    ${Toolbar(`<span class="dim small" id="stgHint">共享存储 ${STORAGE_PATH} · 镜像占用按登记大小汇总</span>`,
      `<button class="btn sm" id="stgRefresh">${Icon("refresh")}刷新</button>`)}
    <div class="stats-grid mb" id="stgGrid" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr))">${'<div class="skel" style="height:110px"></div>'.repeat(3)}</div>
    ${Card({ title: "各镜像容量占比", sub: "按 total_size_gb", body: `<canvas id="stgChart" height="110"></canvas>` })}`;

  const css = getComputedStyle(document.documentElement), mk = (n) => css.getPropertyValue(n).trim();

  function diskCard(d) {
    const pct = d.use_percent || 0;
    const tone = pct >= diskThreshold ? "danger" : pct >= Math.max(60, diskThreshold - 15) ? "warn" : "ok";
    const label = pct >= diskThreshold ? `超阈值(${diskThreshold}%)` : pct >= Math.max(60, diskThreshold - 15) ? "偏高" : "正常";
    const title = d.path || d.mount || STORAGE_PATH;
    const real = d.real_mount && d.real_mount !== title ? d.real_mount : "";
    return `<div class="card" style="padding:13px 15px;border-left:3px solid var(--${tone})">
      <div class="row" style="gap:8px;margin-bottom:8px">
        <span class="mono" style="flex:1;font-weight:600;font-size:13px" title="${esc(d.filesystem || "")}">${esc(title)}</span>
        ${Badge(label, tone, { dot: true })}
      </div>
      <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:8px">
        <span style="font-size:22px;font-weight:700;color:var(--${tone})">${pct}%</span>
        <span class="mono small muted">${esc(d.used)} / ${esc(d.size)}</span>
      </div>
      ${PBar(pct, tone)}
      <div class="mono small muted" style="margin-top:7px">可用 ${esc(d.avail)} · 告警阈值 ${diskThreshold}%${real ? ` · 挂载 ${esc(real)}` : ""}</div>
    </div>`;
  }

  async function load() {
    const grid = $("stgGrid"); if (!grid) return;
    grid.innerHTML = '<div class="skel" style="height:110px"></div>'.repeat(3);
    try {
      const [rd, rm, rs] = await Promise.all([
        api("/disks?path=" + encodeURIComponent(STORAGE_PATH)),
        api("/mirrors"),
        api("/settings"),
      ]);
      const disks = await rd.json(); const mirrors = await rm.json();
      const settings = await rs.json().catch(() => ({}));
      diskThreshold = parseInt(settings.disk_alert_threshold) || 85;
      const hint = $("stgHint");
      if (hint) hint.textContent = `共享存储 ${STORAGE_PATH} · 磁盘告警阈值 ${diskThreshold}% · 镜像占用按登记大小汇总`;
      if (!rd.ok) {
        grid.innerHTML = `<div class="card" style="grid-column:1/-1">${empty("磁盘读取失败", disks.error || "", "#i-warn")}</div>`;
        return;
      }
      const list = Array.isArray(disks) ? disks : [];
      grid.innerHTML = list.length
        ? list.map(diskCard).join("")
        : `<div class="card" style="grid-column:1/-1">${empty(`未检测到 ${STORAGE_PATH}`, "请检查存储挂载与权限", "#i-hdd")}</div>`;
      const sized = (Array.isArray(mirrors) ? mirrors : [])
        .map(m => ({ n: m.name || m.key, gb: parseSizeGB(m.total_size_gb) }))
        .filter(m => m.gb > 0).sort((a, b) => b.gb - a.gb);
      try {
        if (!(await ensureChart())) throw new Error("Chart.js 未加载");
        if (state.charts.storage) { try { state.charts.storage.destroy(); } catch {} state.charts.storage = null; }
        state.charts.storage = new Chart($("stgChart"), {
          type: "bar",
          data: { labels: sized.map(x => x.n), datasets: [{ data: sized.map(x => x.gb), backgroundColor: mk("--brand-strong"), borderRadius: 4 }] },
          options: { responsive: true,
            plugins: { legend: { display: false }, tooltip: { callbacks: { label: (t) => fmtBytes(t.raw * 1024 * 1024 * 1024) } } },
            scales: { x: { ticks: { color: mk("--text-mute"), maxRotation: 45, minRotation: 45, font: { size: 10 } }, grid: { display: false } },
                      y: { beginAtZero: true, ticks: { color: mk("--text-mute"), callback: v => fmtBytes(v * 1024 * 1024 * 1024) }, grid: { color: mk("--border-soft") } } } }
        });
      } catch { const cv = $("stgChart"); if (cv) cv.outerHTML = `<div class="empty" style="padding:20px">${empty("图表不可用", "Chart.js 未加载或渲染失败", "#i-warn")}</div>`; }
    } catch (e) { grid.innerHTML = `<div class="card" style="grid-column:1/-1">${empty("加载失败", e.message, "#i-warn")}</div>`; }
  }

  $("stgRefresh").addEventListener("click", () => routeTo("storage", true));
  load();
});
