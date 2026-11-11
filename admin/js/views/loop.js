// 视图：同步闭环 —— Admission / resume_queue / daemon / 最近 report。
import { register } from "../core/router.js";
import { api } from "../core/api.js";
import { $, esc, empty, skel, toast } from "../core/dom.js";
import { Icon, Badge, Toolbar, Table, Callout, statCard } from "../core/components.js";
import { poller } from "../core/poller.js";

function fmtAge(sec) {
  if (sec == null || sec === "") return "-";
  const s = Number(sec);
  if (!Number.isFinite(s)) return "-";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`;
}

const DAEMON_LABEL = {
  traffic: "流量采样",
  auto_resume: "自动续跑",
  crontab_cache: "定时缓存",
  size_scan: "容量扫描",
  agent_inspect: "巡检任务",
  daily_report: "运维日报",
};

register("loop", function (c) {
  c.innerHTML = `
    ${Toolbar(
      '<span class="dim small">Admission 共帽 · resume 队列 · daemon 心跳 · SSH 触发不变</span>',
      `<button class="btn sm" id="lpRefresh">${Icon("refresh")}刷新</button>`
    )}
    <div id="lpTip" class="mb"></div>
    <div class="stats-grid mb" id="lpStats" style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr))">${skel(4)}</div>
    <div class="dash-grid">
      <div class="card">
        <div class="card-head"><h3>Daemon 心跳</h3><span class="sub">后台线程</span></div>
        <div class="card-body tight" id="lpDaemons">${skel(4)}</div>
      </div>
      <div class="card">
        <div class="card-head"><h3>并发分档</h3><span class="sub">与 wrapper 共帽</span></div>
        <div class="card-body" id="lpCaps">${skel(3)}</div>
      </div>
    </div>
    <div class="card mt">
      <div class="card-head"><h3>续跑队列</h3><span class="sub" id="lpQueueSub">resume_queue</span></div>
      <div class="card-body tight" id="lpQueue">${skel(5)}</div>
    </div>
    <div class="card mt">
      <div class="card-head"><h3>问题与建议</h3></div>
      <div class="card-body" id="lpIssues">${skel(2)}</div>
    </div>`;

  $("lpRefresh").onclick = load;

  async function load() {
    try {
      const d = await (await api("/loop")).json();
      render(d);
    } catch (e) {
      $("lpTip").innerHTML = Callout(e.message || String(e), { tone: "danger", title: "闭环探针失败" });
    }
  }

  function render(d) {
    const st = d.status || "unknown";
    const tone = st === "ok" ? "ok" : st === "degraded" ? "warn" : "danger";
    $("lpTip").innerHTML = Callout(
      `状态 ${st} · syncing ${d.syncing_count ?? "-"} · 队列 ${d.resume_queue_count ?? "-"} · 最近上报 ${esc((d.last_report && d.last_report.key) || "-")}（${fmtAge(d.last_report && d.last_report.age_sec)} 前）`,
      { tone, title: "同步闭环探针 · /api/admin/loop" }
    );

    const caps = d.caps || {};
    const tiers = caps.tier_counts || {};
    $("lpStats").innerHTML = [
      statCard("闭环状态", esc(st), { tone, icon: "shield" }),
      statCard("同步中", String(d.syncing_count ?? 0), { icon: "sync" }),
      statCard("续跑队列", String(d.resume_queue_count ?? 0), { icon: "inbox" }),
      statCard("上报空窗", fmtAge(d.last_report && d.last_report.age_sec), { icon: "clock" }),
    ].join("");

    const daemons = d.daemons || {};
    const dRows = Object.keys(daemons).sort().map(name => {
      const x = daemons[name] || {};
      const ok = !!x.ok;
      const label = DAEMON_LABEL[name] || name;
      return `<tr>
        <td>${esc(label)}<div class="mono small muted">${esc(name)}</div></td>
        <td>${Badge(ok ? "存活" : "异常", ok ? "ok" : "danger", { dot: true })}</td>
        <td class="num">${fmtAge(x.age_sec)}</td>
      </tr>`;
    });
    $("lpDaemons").innerHTML = dRows.length
      ? Table([{ t: "线程" }, { t: "状态" }, { t: "心跳" }], dRows.join(""))
      : empty("暂无 daemon 数据", "等待后台心跳", "#i-cpu");

    const maxP = caps.max_parallel ?? caps.total ?? "-";
    const maxH = caps.max_huge ?? "-";
    const maxL = caps.max_large ?? "-";
    $("lpCaps").innerHTML = `
      <div class="stats-grid" style="grid-template-columns:repeat(3,1fr);gap:10px">
        ${statCard("总并发帽", String(maxP), { icon: "cpu" })}
        ${statCard("巨库帽", String(maxH), { icon: "hdd" })}
        ${statCard("大库帽", String(maxL), { icon: "box" })}
      </div>
      <div class="mt small muted">当前持锁分档：
        huge=${tiers.huge ?? 0} · large=${tiers.large ?? 0} ·
        medium=${tiers.medium ?? 0} · small=${tiers.small ?? 0}
        ${(d.held_locks && d.held_locks.length) ? ` · 持锁 ${d.held_locks.length}` : ""}
      </div>`;

    const q = d.resume_queue || d.queue || [];
    $("lpQueueSub").textContent = `${q.length} 条展示 / 共 ${d.resume_queue_count ?? q.length}`;
    if (!q.length) {
      $("lpQueue").innerHTML = empty("队列为空", "暂无待续跑任务", "#i-check");
    } else {
      const rows = q.map(it => `<tr>
        <td class="mono"><a href="#/synchistory" data-action="noop">${esc(it.key || "-")}</a></td>
        <td>${Badge(it.reason || "-", "info")}</td>
        <td>${esc(it.tier || "-")}</td>
        <td class="num">${esc(it.attempts ?? "-")}</td>
        <td class="num" style="white-space:nowrap">${esc(it.not_before || it.due_at || "-")}</td>
        <td class="small">${esc((it.last_error || "").slice(0, 80) || "-")}</td>
      </tr>`).join("");
      $("lpQueue").innerHTML = Table(
        [{ t: "镜像" }, { t: "原因" }, { t: "档" }, { t: "重试" }, { t: "不早于" }, { t: "最近错误" }],
        rows
      );
    }

    const issues = d.issues || [];
    if (!issues.length) {
      $("lpIssues").innerHTML = empty("无异常项", "闭环健康", "#i-check");
    } else {
      $("lpIssues").innerHTML = `<ul style="margin:0;padding-left:18px;line-height:1.8">
        ${issues.map(i => `<li><code>${esc(i)}</code></li>`).join("")}
      </ul>`;
    }
  }

  load();
  c._cleanup = poller(load, 15000);
});
