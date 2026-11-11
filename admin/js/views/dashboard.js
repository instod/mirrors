// 视图：首页 —— 指标 / 健康度 / 实时下载速度 / 进行中。
import { register, routeTo } from "../core/router.js";
import { api } from "../core/api.js";
import { $, esc, empty } from "../core/dom.js";
import { fmtBytes } from "../core/format.js";
import { Icon, Badge, statusPill, PBar, statCard } from "../core/components.js";
import { poller } from "../core/poller.js";
import { state, fetchAlertsCached } from "../core/state.js";
import { ensureChart } from "../core/chart.js";

register("dashboard", function (c) {
  let trafficTimer = null;
  c._cleanup = () => {
    if (c._stop) c._stop();
    if (trafficTimer) { clearInterval(trafficTimer); trafficTimer = null; }
    if (state.charts.dashTraffic) {
      try { state.charts.dashTraffic.destroy(); } catch {}
      delete state.charts.dashTraffic;
    }
  };

  c.innerHTML = `
    <div class="dash">
      <div class="spread mb">
        <div class="dim small">运行总览 · 30 秒刷新 · 流量 2 秒实时</div>
        <div class="row">
          <button class="btn sm" id="dashRefresh">${Icon("refresh")}刷新</button>
        </div>
      </div>

      <div class="stats-grid dash-stats" id="dashStats">${'<div class="skel" style="height:78px"></div>'.repeat(4)}</div>

      <div id="dashAlerts" class="mt" style="display:none"></div>
      <div class="card mt dash-loop-card" id="dashLoop"><div class="card-body" id="dashLoopBody"><div class="skel" style="height:48px"></div></div></div>
      <div class="card mt" id="dashHealth" style="display:none"><div class="card-body" id="dashHealthBody"></div></div>

      <div class="dash-grid mt">
        <div class="card dash-traffic-card">
          <div class="card-head">
            <h3>全站下载速度</h3>
            <span class="sub mono" id="dashTrafficTs"></span>
          </div>
          <div class="card-body">
            <div class="dash-traffic-stats" id="dashTrafficStats">
              <div class="skel" style="height:52px"></div>
              <div class="skel" style="height:52px"></div>
            </div>
            <canvas id="dashTrafficChart" height="120"></canvas>
            <div class="dash-dl-head">
              <span>近 1 小时镜像下载</span>
              <span class="mono small muted" id="dashDlSum"></span>
            </div>
            <div id="dashDlList" class="dash-dl-list"></div>
            <div class="dash-iface-row" id="dashIfaces"></div>
            <div class="small muted mt" id="dashTrafficHint">采样自 /api/admin/traffic</div>
          </div>
        </div>
        <div class="card">
          <div class="card-head"><h3>进行中</h3></div>
          <div class="card-body" id="dashTasks"></div>
        </div>
      </div>
    </div>`;

  $("dashRefresh").addEventListener("click", load);

  const css = getComputedStyle(document.documentElement);
  const col = (v) => css.getPropertyValue(v).trim();

  function fmtWin(sec) {
    const s = Number(sec) || 0;
    if (s >= 3600) return `${Math.round(s / 3600)} 小时`;
    if (s >= 60) return `${Math.round(s / 60)} 分钟`;
    return `${s} 秒`;
  }

  function ensureTrafficChart() {
    if (!window.Chart) return null;
    if (state.charts.dashTraffic) return state.charts.dashTraffic;
    const canvas = $("dashTrafficChart");
    if (!canvas) return null;
    try {
      state.charts.dashTraffic = new Chart(canvas, {
        type: "line",
        data: {
          labels: [],
          datasets: [
            {
              label: "下载 (出站)",
              data: [],
              borderColor: col("--brand-strong") || col("--brand") || "#1677d2",
              backgroundColor: col("--brand-bg") || "rgba(22,119,210,.12)",
              borderWidth: 2, fill: true, tension: 0.35, pointRadius: 0,
            },
            {
              label: "同步入站",
              data: [],
              borderColor: col("--ok") || "#2f9e44",
              backgroundColor: "transparent",
              borderWidth: 2, fill: false, tension: 0.35, pointRadius: 0,
            },
          ],
        },
        options: {
          responsive: true,
          animation: false,
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: { labels: { color: col("--text-dim"), font: { size: 11.5 }, boxWidth: 10 } },
          },
          scales: {
            x: {
              ticks: { color: col("--text-mute"), maxTicksLimit: 6, font: { size: 10 } },
              grid: { display: false },
            },
            y: {
              beginAtZero: true,
              ticks: {
                color: col("--text-mute"),
                font: { size: 10 },
                callback: (v) => fmtBytes(v) + "/s",
              },
              grid: { color: col("--border-soft") },
            },
          },
        },
      });
      return state.charts.dashTraffic;
    } catch {
      canvas.outerHTML = empty("图表不可用", "Chart.js 未加载", "#i-warn");
      return null;
    }
  }

  function renderTraffic(d) {
    if (!d || typeof d !== "object") return;
    const dl = d.download_bytes_per_sec ?? d.tx_bytes_per_sec ?? 0;
    const inn = d.sync_in_bytes_per_sec ?? d.rx_bytes_per_sec ?? 0;
    const dlInfo = d.downloads || {};
    const items = Array.isArray(dlInfo.items) ? dlInfo.items : [];
    const winSec = dlInfo.window_sec || 3600;
    const nginxBps = dlInfo.total_bps || 0;

    const ts = $("dashTrafficTs");
    if (ts) ts.textContent = d.timestamp || "";
    const hint = $("dashTrafficHint");
    if (hint) {
      hint.textContent = `${d.hostname || "本机"} · ${d.source || "traffic"} · ${d.sample_interval_sec || 2}s 刷新`;
    }

    const box = $("dashTrafficStats");
    if (box) {
      const hot = dl > 1024 * 100; // >100KB/s
      box.innerHTML = `
        <div class="dash-traffic-kpi${hot ? " hot" : ""}">
          <div class="dash-traffic-kpi-label">实时下载</div>
          <div class="dash-traffic-kpi-val">${fmtBytes(dl)}/s</div>
          <div class="dash-traffic-kpi-sub">网卡出站 TX</div>
        </div>
        <div class="dash-traffic-kpi alt">
          <div class="dash-traffic-kpi-label">同步入站</div>
          <div class="dash-traffic-kpi-val">${fmtBytes(inn)}/s</div>
          <div class="dash-traffic-kpi-sub">网卡入站 RX</div>
        </div>
        <div class="dash-traffic-kpi">
          <div class="dash-traffic-kpi-label">镜像均速</div>
          <div class="dash-traffic-kpi-val">${fmtBytes(nginxBps)}/s</div>
          <div class="dash-traffic-kpi-sub">近 ${fmtWin(winSec)} · ${fmtBytes(dlInfo.total_bytes || 0)}</div>
        </div>`;
    }

    const sum = $("dashDlSum");
    if (sum) {
      sum.textContent = items.length
        ? `${fmtBytes(nginxBps)}/s · ${dlInfo.total_reqs || 0} 次 · ${fmtBytes(dlInfo.total_bytes || 0)}`
        : `近 ${fmtWin(winSec)} 暂无`;
    }
    const list = $("dashDlList");
    if (list) {
      if (!items.length) {
        list.innerHTML = `<div class="small muted" style="padding:6px 0">近 ${fmtWin(winSec)} 无镜像文件下载（网卡速度仍实时）</div>`;
      } else {
        const maxB = Math.max(...items.map(i => i.bps || 0), 1);
        list.innerHTML = items.slice(0, 8).map(i => {
          const pct = Math.max(4, Math.round(((i.bps || 0) / maxB) * 100));
          return `<button type="button" class="dash-dl-row" data-mkey="${esc(i.key)}" title="${esc(i.name || i.key)}">
            <span class="mono dash-dl-key">${esc(i.key)}</span>
            <div class="dash-dl-bar"><i style="width:${pct}%"></i></div>
            <span class="dash-dl-rate">${fmtBytes(i.bps || 0)}/s</span>
            <span class="dash-dl-req muted">${i.reqs || 0}</span>
          </button>`;
        }).join("");
        list.querySelectorAll("[data-mkey]").forEach(btn => {
          btn.addEventListener("click", () => {
            window._mirrorInitQ = btn.dataset.mkey || "";
            routeTo("mirrors");
          });
        });
      }
    }

    const ifBox = $("dashIfaces");
    if (ifBox) {
      const ifaces = (d.ifaces || []).filter(x => (x.tx_bps || 0) + (x.rx_bps || 0) > 0).slice(0, 4);
      ifBox.innerHTML = ifaces.length
        ? ifaces.map(x => `<span class="dash-iface-chip"><b>${esc(x.name)}</b> ↓${fmtBytes(x.rx_bps || 0)}/s ↑${fmtBytes(x.tx_bps || 0)}/s</span>`).join("")
        : "";
    }

    const chart = ensureTrafficChart();
    if (!chart) return;
    const hist = Array.isArray(d.history) ? d.history : [];
    chart.data.labels = hist.map(p => p.t || "");
    chart.data.datasets[0].data = hist.map(p => p.tx ?? 0);
    chart.data.datasets[1].data = hist.map(p => p.rx ?? 0);
    chart.update("none");
  }

  async function loadTraffic() {
    try {
      await ensureChart();
      const d = await (await api("/traffic")).json();
      renderTraffic(d);
    } catch (e) {
      const box = $("dashTrafficStats");
      if (box) box.innerHTML = `<div class="small muted">流量加载失败：${esc(e.message || String(e))}</div>`;
    }
  }

  function renderStats(d) {
    const syncing = (d.status_distribution || {}).syncing || 0;
    $("dashStats").innerHTML =
      statCard("镜像总数", d.mirror_count ?? "--", {
        sub: `软件 ${d.db_mirror_count ?? 0} · 系统 ${d.system_mirror_count ?? 0}`,
        tone: "brand", icon: "db",
      }) +
      statCard("总容量", fmtBytes((d.total_size_gb || 0) * 1024 ** 3), {
        sub: "全部镜像合计", tone: "accent", icon: "hdd",
      }) +
      statCard("同步中", syncing, {
        sub: syncing ? "有任务在跑" : "当前空闲",
        tone: syncing ? "info" : "ok", icon: "sync",
      }) +
      (d.fail_count > 0
        ? `<div class="stat dash-fail" id="dashFailCard" role="button" tabindex="0">
             <div class="stat-ico danger">${Icon("warn")}</div>
             <div><div class="stat-val" style="color:var(--danger)">${d.fail_count}</div>
             <div class="stat-label">同步失败 · 点此查看</div></div></div>`
        : statCard("同步失败", 0, { sub: "全部正常", tone: "ok", icon: "check" }));
    $("dashFailCard")?.addEventListener("click", () => {
      window._mirrorInitFilter = "fail";
      routeTo("mirrors");
    });
  }

  function renderAlerts(d) {
    const box = $("dashAlerts"); if (!box) return;
    const alerts = (d && d.alerts) || [];
    if (!alerts.length) { box.style.display = "none"; box.innerHTML = ""; return; }
    box.style.display = "";
    const top = alerts.slice(0, 3);
    const more = alerts.length - top.length;
    box.innerHTML = `
      <div class="card dash-alert-card">
        <div class="card-body" style="padding:12px 16px">
          <div class="spread mb" style="margin-bottom:8px">
            <div class="row" style="gap:8px">${Badge(`${alerts.length} 条活动告警`, "danger", { dot: true, pulse: true })}
              <span class="small dim">优先处理</span></div>
          </div>
          ${top.map(a => `<div class="row" style="gap:8px;padding:4px 0;font-size:13px">
            ${Badge(a.level === "error" ? "严重" : "警告", a.level === "error" ? "danger" : "warn")}
            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(a.title || "")}</span>
          </div>`).join("")}
          ${more > 0 ? `<div class="small muted mt">还有 ${more} 条…</div>` : ""}
        </div>
      </div>`;
  }

  function ageLabel(sec) {
    if (sec == null) return "无心跳";
    const s = Number(sec);
    if (!Number.isFinite(s) || s < 0) return "无心跳";
    if (s < 60) return `${Math.round(s)} 秒前`;
    if (s < 3600) return `${Math.round(s / 60)} 分钟前`;
    return `${Math.round(s / 3600)} 小时前`;
  }

  function renderLoop(loop) {
    const card = $("dashLoop");
    const box = $("dashLoopBody");
    if (!card || !box) return;
    if (!loop || typeof loop !== "object") {
      card.classList.remove("is-ok", "is-bad");
      box.innerHTML = empty("闭环探针不可用", "dashboard 未返回 loop 字段", "#i-warn");
      return;
    }
    const ok = loop.status === "ok";
    card.classList.toggle("is-ok", ok);
    card.classList.toggle("is-bad", !ok);
    const daemons = loop.daemons || {};
    const daemonNames = {
      traffic: "流量采样",
      auto_resume: "自动续跑",
      crontab_cache: "定时缓存",
      size_scan: "容量扫描",
      agent_inspect: "巡检任务",
      daily_report: "运维日报",
    };
    const daemonHtml = Object.keys(daemonNames).map(k => {
      const d = daemons[k] || {};
      const dOk = !!d.ok;
      return `<div class="dash-loop-daemon ${dOk ? "ok" : "bad"}" title="${esc(ageLabel(d.age_sec))}">
        <span class="dash-loop-dot"></span>
        <span class="dash-loop-daemon-name">${esc(daemonNames[k])}</span>
        <span class="dash-loop-daemon-age">${esc(ageLabel(d.age_sec))}</span>
      </div>`;
    }).join("");
    const issues = (loop.issues || []).slice(0, 5);
    const queueN = loop.resume_queue_count || 0;
    const stuckN = (loop.resume_stuck || []).length;
    const report = loop.last_report || {};
    const cron = loop.crontab_cache || {};
    box.innerHTML = `
      <div class="dash-loop">
        <div class="dash-loop-head">
          <div>
            <div class="dash-loop-title">同步闭环探针</div>
            <div class="small muted">trigger → 执行 → report → 续跑</div>
          </div>
          ${Badge(ok ? "正常" : "降级", ok ? "ok" : "warn", { dot: true, pulse: !ok })}
        </div>
        <div class="dash-loop-metrics">
          <div class="dash-loop-metric"><span class="dash-loop-metric-val">${loop.syncing_count || 0}</span><span class="dash-loop-metric-lab">同步中</span></div>
          <div class="dash-loop-metric"><span class="dash-loop-metric-val">${queueN}</span><span class="dash-loop-metric-lab">续跑队列</span></div>
          <div class="dash-loop-metric ${stuckN ? "hot" : ""}"><span class="dash-loop-metric-val">${stuckN}</span><span class="dash-loop-metric-lab">卡住</span></div>
          <div class="dash-loop-metric"><span class="dash-loop-metric-val">${cron.entries ?? 0}</span><span class="dash-loop-metric-lab">定时条目</span></div>
          <div class="dash-loop-metric"><span class="dash-loop-metric-val">${loop.pending_with_script || 0}</span><span class="dash-loop-metric-lab">待脚本</span></div>
        </div>
        <div class="dash-loop-daemons">${daemonHtml || '<span class="small muted">无 daemon 数据</span>'}</div>
        <div class="dash-loop-meta">
          <span>最近上报 ${report.key ? `<code>${esc(report.key)}</code> · ${esc(ageLabel(report.age_sec))}` : "暂无"}</span>
          <span>crontab ${esc(ageLabel(cron.age_sec))}</span>
        </div>
        ${issues.length
          ? `<div class="dash-loop-issues">${issues.map(i => `<span>${esc(i)}</span>`).join("")}</div>`
          : ""}
      </div>`;
  }

  function renderHealth(hd, sd) {
    const st = hd.stats || {}, total = st.total || 0, healthy = st.healthy || 0;
    const score = total ? Math.round(healthy / total * 100) : 0;
    const tone = score >= 85 ? "ok" : score >= 70 ? "warn" : "danger";
    const attention = (st.fail || 0) + (st.stale || 0);
    const dist = sd || {};
    $("dashHealth").style.display = "";
    $("dashHealthBody").innerHTML = `
      <div class="row wrap" style="gap:16px;align-items:center">
        <div class="row" style="gap:13px;min-width:210px">
          <div class="dash-score ${tone}">${score}</div>
          <div>
            <div style="font-weight:700;font-size:13.5px">整体健康度</div>
            <div class="small muted">${healthy}/${total} 镜像状态正常</div>
          </div>
        </div>
        <div style="flex:1;min-width:220px"><div class="row wrap">
          ${Badge("正常 " + (dist.success ?? healthy), "ok", { dot: true })}
          ${Badge("失败 " + (dist.fail ?? st.fail ?? 0), "danger", { dot: true })}
          ${Badge("待同步 " + (dist.pending || 0), "warn", { dot: true })}
          ${Badge("同步中 " + (dist.syncing ?? st.syncing ?? 0), "info", { dot: true })}
        </div></div>
        <div style="flex:1;min-width:140px">${PBar(score, tone)}</div>
        <button class="btn sm" id="dashGoHealth">${attention ? `需关注 ${attention}` : "查看需关注"}</button>
      </div>`;
    $("dashGoHealth")?.addEventListener("click", () => {
      window._mirrorInitFilter = "attention";
      routeTo("mirrors");
    });
  }

  function renderTasks(ts) {
    const box = $("dashTasks"); if (!box) return;
    if (!ts.length) {
      box.innerHTML = empty("当前无进行中任务", "全部空闲", "#i-check");
      return;
    }
    const show = ts.slice(0, 5);
    box.innerHTML = `
      <div class="small dim mb">共 ${ts.length} 个进行中</div>
      ${show.map(t => `<div class="row dash-task-row">
        <span class="mono small" style="min-width:88px">${esc(t.key)}</span>
        <span class="dash-task-name">${esc(t.name)}</span>
        ${t.is_scanning ? Badge("扫描中", "info", { dot: true, pulse: true }) : statusPill(t.status)}
      </div>`).join("")}
      ${ts.length > 5 ? `<div class="small muted mt">另有 ${ts.length - 5} 个…</div>` : ""}`;
  }

  async function load() {
    try {
      const [d, hd, ts, al] = await Promise.all([
        (await api("/dashboard")).json(),
        (await api("/mirrors/health")).json().catch(() => null),
        (await api("/sync/status")).json().catch(() => null),
        fetchAlertsCached(api),
      ]);
      if (!d || typeof d !== "object") throw new Error("响应格式异常");
      renderStats(d);
      renderAlerts(al);
      renderLoop(d.loop);
      if (hd && hd.stats) renderHealth(hd, d.status_distribution);
      if (Array.isArray(ts)) renderTasks(ts);
      else $("dashTasks").innerHTML = empty("任务状态加载失败", "", "#i-warn");
    } catch (e) {
      console.error("dashboard load 失败:", e);
      const loopBody = $("dashLoopBody");
      if (loopBody && loopBody.querySelector(".skel")) {
        loopBody.innerHTML = empty("闭环探针加载失败", e.message || String(e), "#i-warn");
      }
      if (!$("dashStats").children.length) {
        c.innerHTML = empty("首页加载失败", e.message || String(e), "#i-warn",
          '<button class="btn sm primary" id="dashRetry">重试</button>');
        $("dashRetry")?.addEventListener("click", () => routeTo("dashboard", true));
        if (c._stop) c._stop();
        if (trafficTimer) { clearInterval(trafficTimer); trafficTimer = null; }
      }
    }
  }

  load();
  loadTraffic();
  c._stop = poller(() => { if (state.currentRoute === "dashboard") load(); }, 30000);
  trafficTimer = setInterval(() => {
    if (state.currentRoute === "dashboard" && document.visibilityState === "visible") loadTraffic();
  }, 2000);
});
