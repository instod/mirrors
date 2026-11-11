// 视图：流量观测 —— 出站下载 / 入站同步网卡速率（/api/admin/traffic）。
import { register } from "../core/router.js";
import { api } from "../core/api.js";
import { $, esc, skel, toast } from "../core/dom.js";
import { Icon, Toolbar, Callout, statCard, Badge } from "../core/components.js";
import { fmtBytes } from "../core/format.js";

register("traffic", function (c) {
  let timer = null;
  c._cleanup = () => { if (timer) clearInterval(timer); timer = null; };

  c.innerHTML = `
    ${Toolbar(
      '<span class="dim small">出站=用户下载 TX · 入站=同步拉取 RX · 采样自网卡</span>',
      `<button class="btn sm primary" id="tfRefresh">${Icon("refresh")}刷新</button>`
    )}
    <div id="tfTip" class="mb"></div>
    <div class="stats-grid mb" id="tfStats" style="grid-template-columns:repeat(auto-fit,minmax(160px,1fr))">${skel(4)}</div>
    <div class="card">
      <div class="card-head"><h3>nginx 下载窗口</h3><span class="sub" id="tfWinSub">近期</span></div>
      <div class="card-body" id="tfNginx">${skel(3)}</div>
    </div>
    <div class="card mt">
      <div class="card-head"><h3>Top 下载</h3><span class="sub">按路径</span></div>
      <div class="card-body tight" id="tfTop">${skel(5)}</div>
    </div>`;

  $("tfRefresh").onclick = () => load(true);

  function fmtWin(sec) {
    if (!sec) return "-";
    if (sec < 3600) return `${Math.round(sec / 60)} 分钟`;
    return `${(sec / 3600).toFixed(1)} 小时`;
  }

  async function load(manual) {
    try {
      const d = await (await api("/traffic")).json();
      const dl = d.tx_bps ?? d.download_bps ?? 0;
      const inn = d.rx_bps ?? d.inbound_bps ?? 0;
      const nginx = d.nginx || d.download || {};
      const nginxBps = nginx.bps ?? nginx.avg_bps ?? 0;
      const winSec = nginx.window_sec ?? d.window_sec;
      const hot = dl > 50 * 1024 * 1024;

      $("tfTip").innerHTML = Callout(
        `${esc(d.hostname || "本机")} · ${esc(d.source || "traffic")} · 间隔 ${esc(String(d.sample_interval_sec || 2))}s`,
        { tone: hot ? "warn" : "ok", title: "实时流量" }
      );
      $("tfStats").innerHTML = [
        statCard("实时下载 TX", `${fmtBytes(dl)}/s`, { tone: hot ? "warn" : "ok", icon: "arrow-up" }),
        statCard("同步入站 RX", `${fmtBytes(inn)}/s`, { icon: "inbox" }),
        statCard("镜像均速", `${fmtBytes(nginxBps)}/s`, { icon: "globe" }),
        statCard("窗口流量", fmtBytes(nginx.total_bytes || 0), { icon: "act", sub: fmtWin(winSec) }),
      ].join("");

      $("tfWinSub").textContent = fmtWin(winSec);
      $("tfNginx").innerHTML = `
        <div class="row" style="gap:10px;flex-wrap:wrap">
          ${Badge(`请求 ${(nginx.requests ?? nginx.hits ?? "—")}`, "info")}
          ${Badge(`字节 ${fmtBytes(nginx.total_bytes || 0)}`, "muted")}
          ${Badge(`均速 ${fmtBytes(nginxBps)}/s`, "ok")}
        </div>
        <div class="small muted mt">出站走 8.13 nginx；本页采样在控制面网卡，与首页流量卡同源。</div>`;

      const tops = nginx.top || nginx.items || d.top || [];
      $("tfTop").innerHTML = tops.length
        ? `<table class="tbl dense"><thead><tr><th>路径</th><th>次数</th><th>流量</th></tr></thead><tbody>
            ${tops.slice(0, 20).map(it => `<tr>
              <td class="mono small">${esc(it.path || it.url || it.key || "-")}</td>
              <td>${esc(String(it.count ?? it.hits ?? "-"))}</td>
              <td class="mono">${fmtBytes(it.bytes || 0)}</td>
            </tr>`).join("")}
          </tbody></table>`
        : `<div class="dim small">暂无 Top 明细（窗口内无采样）</div>`;
      if (manual) toast("已刷新", "ok");
    } catch (e) {
      $("tfTip").innerHTML = Callout(e.message || String(e), { tone: "danger", title: "流量读取失败" });
    }
  }

  load();
  timer = setInterval(() => load(false), 3000);
});
