// 视图：告警中心 —— 同步失败 / 磁盘 / CPU / 内存。
import { register } from "../core/router.js";
import { api } from "../core/api.js";
import { $, esc, empty, skel } from "../core/dom.js";
import { Icon, Badge, Toolbar, Callout, statCard } from "../core/components.js";
import { fetchAlertsCached } from "../core/state.js";
import { poller } from "../core/poller.js";

const TYPE_LABEL = {
  sync_fail: "同步失败",
  sync_resume: "续传中",
  disk_full: "磁盘",
  cpu_high: "CPU",
  mem_high: "内存",
};

register("alerts", function (c) {
  c.innerHTML = `
    ${Toolbar(
      '<span class="dim small">只读视图 · 阈值在系统设置中调整</span>',
      `<button class="btn sm" id="alRefresh">${Icon("refresh")}刷新</button>`
    )}
    <div id="alTip" class="mb"></div>
    <div class="stats-grid mb" id="alStats" style="grid-template-columns:repeat(auto-fit,minmax(130px,1fr))">${skel(4)}</div>
    <div class="card">
      <div class="card-head"><h3>活动告警</h3><span class="sub" id="alSub">加载中</span></div>
      <div class="card-body" id="alList">${skel(5)}</div>
    </div>`;

  $("alRefresh").onclick = () => load(true);

  async function load(force = false) {
    try {
      const d = force
        ? await (await api("/alerts")).json()
        : await fetchAlertsCached(api);
      render(d || { alerts: [], count: 0 });
    } catch (e) {
      $("alTip").innerHTML = Callout(e.message || String(e), { tone: "danger", title: "告警拉取失败" });
    }
  }

  function render(d) {
    const items = d.alerts || [];
    const n = d.count ?? items.length;
    const errN = items.filter(a => a.level === "error").length;
    const warnN = items.filter(a => a.level === "warning").length;
    const infoN = items.filter(a => a.level === "info").length;

    $("alTip").innerHTML = n
      ? Callout(`共 ${n} 条活动告警（错误 ${errN} · 警告 ${warnN} · 信息 ${infoN}）`, {
          tone: errN ? "danger" : warnN ? "warn" : "info",
          title: "告警中心 · /api/admin/alerts",
        })
      : Callout("当前无活动告警。", { tone: "ok", title: "告警中心" });

    $("alStats").innerHTML = [
      statCard("合计", String(n), { icon: "bell", tone: n ? "warn" : "ok" }),
      statCard("错误", String(errN), { icon: "warn", tone: errN ? "danger" : "" }),
      statCard("警告", String(warnN), { icon: "act", tone: warnN ? "warn" : "" }),
      statCard("信息", String(infoN), { icon: "info" }),
    ].join("");

    $("alSub").textContent = `${n} 条`;
    if (!items.length) {
      $("alList").innerHTML = empty("一切正常", "没有需要处理的告警", "#i-check");
      return;
    }

    $("alList").innerHTML = `<div class="al-list">${items.map(a => {
      const lvl = a.level || "info";
      const tone = lvl === "error" ? "danger" : lvl === "warning" ? "warn" : "info";
      const type = TYPE_LABEL[a.type] || a.type || "-";
      const go = a.type === "sync_fail" || a.type === "sync_resume"
        ? `data-action="al-go-sync" data-key="${esc(a.target || "")}"`
        : a.type === "disk_full"
          ? `data-action="al-go-storage"`
          : a.type === "cpu_high" || a.type === "mem_high"
            ? `data-action="al-go-mon"`
            : "";
      return `<div class="al-item ${tone}" ${go} style="cursor:${go ? "pointer" : "default"}">
        <div class="al-item-top">
          ${Badge(type, tone, { dot: true })}
          <strong class="al-item-title">${esc(a.title || "-")}</strong>
          <span class="small muted mono" style="margin-left:auto">${esc(a.time || "-")}</span>
        </div>
        <div class="small muted">${esc(a.reason || "")}${a.target ? ` · <code>${esc(a.target)}</code>` : ""}</div>
      </div>`;
    }).join("")}</div>`;
  }

  // 简易跳转
  window._actions["al-go-sync"] = (el) => {
    const key = el?.dataset?.key;
    if (key) window._mirrorInitFilter = key;
    routeTo("sync");
  };
  window._actions["al-go-storage"] = () => routeTo("storage");
  window._actions["al-go-mon"] = () => routeTo("monitor");

  load(true);
  c._cleanup = poller(() => load(false), 60000);
});
