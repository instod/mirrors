// 视图：主机监控 —— CPU/内存/负载/网络（/proc 实时采集）+ 折线图，10s 刷新（可暂停）。
import { register } from "../core/router.js";
import { api } from "../core/api.js";
import { $, esc, empty } from "../core/dom.js";
import { fmtBytes } from "../core/format.js";
import { statCard, Toolbar, Icon } from "../core/components.js";
import { state } from "../core/state.js";
import { ensureChart } from "../core/chart.js";
import { poller } from "../core/poller.js";

register("monitor", function (c) {
  let running = true;
  let stopPoll = null;
  let cpuThreshold = 80;
  let memThreshold = 85;
  c._cleanup = () => { if (stopPoll) { stopPoll(); stopPoll = null; } };

  c.innerHTML = `
    ${Toolbar('<span class="dim small" id="monHostHint">本机监控 · 每 10 秒刷新 · 阈值见「系统设置」</span>',
      `<button class="btn sm" id="monPause">${Icon("stop")}暂停刷新</button>`)}
    <div class="stats-grid" id="monGrid" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">${'<div class="skel" style="height:70px"></div>'.repeat(4)}</div>
    <div class="card mt">
      <div class="card-head"><h3>CPU 使用率</h3><span class="sub mono" id="monTs"></span></div>
      <div class="card-body"><canvas id="cpuChart" height="75"></canvas></div>
    </div>
    <div class="grid-2 mt">
      <div class="card"><div class="card-head"><h3>网络流量 (bytes/s)</h3></div><div class="card-body"><canvas id="netChart" height="100"></canvas></div></div>
      <div class="card"><div class="card-head"><h3>系统概况</h3></div><div class="card-body mono small" id="monSys" style="line-height:2"></div></div>
    </div>`;

  const css = getComputedStyle(document.documentElement);
  const mk = (n) => css.getPropertyValue(n).trim();
  const cpuHist = Array(30).fill(null);
  const rxHist = Array(30).fill(null), txHist = Array(30).fill(null);
  let chartsReady = false;

  async function initCharts() {
    const ok = await ensureChart();
    if (!ok || state.currentRoute !== "monitor") {
      for (const id of ["cpuChart", "netChart"]) {
        const cv = $(id); if (cv) cv.outerHTML = `<div class="empty" style="padding:20px">${empty("图表不可用", "Chart.js 未加载", "#i-warn")}</div>`;
      }
      return;
    }
    state.charts.cpu = new Chart($("cpuChart"), {
      type: "line",
      data: { labels: cpuHist.map((_, i) => i), datasets: [{ label: "CPU %", data: cpuHist, borderColor: mk("--brand"), backgroundColor: mk("--brand-bg"), borderWidth: 2, fill: true, tension: .35, pointRadius: 0 }] },
      options: { responsive: true, animation: false, plugins: { legend: { display: false } },
        scales: { x: { display: false }, y: { min: 0, max: 100, ticks: { color: mk("--text-mute"), callback: v => v + "%" }, grid: { color: mk("--border-soft") } } } }
    });
    state.charts.net = new Chart($("netChart"), {
      type: "line",
      data: { labels: rxHist.map((_, i) => i), datasets: [
        { label: "接收", data: rxHist, borderColor: mk("--ok"), backgroundColor: "transparent", borderWidth: 2, tension: .35, pointRadius: 0 },
        { label: "发送", data: txHist, borderColor: mk("--warn"), backgroundColor: "transparent", borderWidth: 2, tension: .35, pointRadius: 0 }] },
      options: { responsive: true, animation: false, plugins: { legend: { labels: { color: mk("--text-dim"), font: { size: 12 }, boxWidth: 12 } } },
        scales: { x: { display: false }, y: { ticks: { color: mk("--text-mute"), callback: v => fmtBytes(v) }, grid: { color: mk("--border-soft") } } } }
    });
    chartsReady = true;
  }

  async function loadThresholds() {
    try {
      const s = await (await api("/settings")).json();
      cpuThreshold = parseInt(s.cpu_alert_threshold) || 80;
      memThreshold = parseInt(s.mem_alert_threshold) || 85;
    } catch { /* 保持默认 */ }
  }

  async function tick() {
    if (!running) return;
    try {
      const d = await (await api("/monitor")).json();
      const host = d.hostname || "本机";
      const hint = $("monHostHint");
      if (hint) hint.textContent = `监控主机：${host}（Web/API 节点）· 每 10 秒刷新 · CPU≥${cpuThreshold}% / 内存≥${memThreshold}% 告警色`;
      $("monTs").textContent = d.timestamp || "";
      const cpu = d.cpu_percent ?? 0, mem = d.mem_percent ?? 0;
      $("monGrid").innerHTML =
        statCard("CPU 使用率", cpu + "%", { sub: "1 秒采样", tone: cpu >= cpuThreshold ? "danger" : "ok", icon: "cpu" }) +
        statCard("内存使用率", mem + "%", { sub: fmtBytes((d.mem_used_kb || 0) * 1024) + " / " + fmtBytes((d.mem_total_kb || 0) * 1024), tone: mem >= memThreshold ? "danger" : "info", icon: "box" }) +
        statCard("系统负载 1min", d.load1 ?? "-", { sub: `5min ${d.load5} · 15min ${d.load15}`, tone: "warn", icon: "act" }) +
        statCard("网络接收", fmtBytes(d.net_rx_bytes_per_sec || 0) + "/s", { sub: `发送 ${fmtBytes(d.net_tx_bytes_per_sec || 0)}/s`, tone: "accent", icon: "zap" });
      $("monSys").innerHTML =
        `主机：${esc(host)}<br>内存：${fmtBytes((d.mem_used_kb || 0) * 1024)} / ${fmtBytes((d.mem_total_kb || 0) * 1024)}（${mem}%）<br>负载 1/5/15：${d.load1} / ${d.load5} / ${d.load15}<br>网络：↓ ${fmtBytes(d.net_rx_bytes_per_sec || 0)}/s · ↑ ${fmtBytes(d.net_tx_bytes_per_sec || 0)}/s`;
      cpuHist.shift(); cpuHist.push(cpu);
      rxHist.shift(); rxHist.push(d.net_rx_bytes_per_sec); txHist.shift(); txHist.push(d.net_tx_bytes_per_sec);
      if (chartsReady && state.charts.cpu) { state.charts.cpu.data.datasets[0].data = cpuHist; state.charts.cpu.update("none"); }
      if (chartsReady && state.charts.net) { state.charts.net.data.datasets[0].data = rxHist; state.charts.net.data.datasets[1].data = txHist; state.charts.net.update("none"); }
    } catch {
      const g = $("monGrid");
      if (g && !g.children.length) g.innerHTML = `<div class="card" style="grid-column:1/-1"><div class="card-body">${empty("监控数据获取失败", "重试中…", "#i-warn")}</div></div>`;
    }
  }

  $("monPause").addEventListener("click", () => {
    running = !running;
    $("monPause").innerHTML = running ? Icon("stop") + "暂停刷新" : Icon("play") + "继续刷新";
  });

  Promise.all([initCharts(), loadThresholds()]).then(() => tick());
  stopPoll = poller(() => { if (running) tick(); }, 10000);
});
