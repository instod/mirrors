// 视图：系统/软件源子目录（os_mirrors / soft_mirrors）。
import { register } from "../core/router.js";
import { api, postJSON } from "../core/api.js";
import { $, esc, empty, skel, toast } from "../core/dom.js";
import { Icon, Badge, Toolbar, Callout } from "../core/components.js";

register("sources", function (c) {
  let pollTimer = null;
  c._cleanup = () => { if (pollTimer) clearInterval(pollTimer); pollTimer = null; };

  c.innerHTML = `
    ${Toolbar(
      '<span class="dim small">os_mirrors / soft_mirrors 子目录 · 大小异步 du</span>',
      `<button class="btn sm primary" id="srcRefresh">${Icon("refresh")}刷新</button>`
    )}
    <div id="srcTip" class="mb"></div>
    <div class="dash-grid" id="srcGrid">${skel(2)}</div>`;

  $("srcRefresh").onclick = () => load(true);

  function cardGroup(label, group, items) {
    const rows = (items || []).map(it => {
      const st = it.size_status || it.status || "";
      const size = it.size || "—";
      const tone = st === "done" ? "ok" : st === "pending" ? "info" : st === "fail" ? "danger" : "muted";
      return `<tr>
        <td class="mono">${esc(it.name)}</td>
        <td class="mono">${esc(size)}</td>
        <td>${Badge(st || "—", tone)}</td>
        <td><button class="btn sm" data-du="${esc(group)}" data-name="${esc(it.name)}">${Icon("refresh")}测大小</button></td>
      </tr>`;
    }).join("");
    return `<div class="card">
      <div class="card-head"><h3>${esc(label)}</h3><span class="sub">${(items || []).length} 项</span></div>
      <div class="card-body tight">
        ${(items || []).length
          ? `<table class="tbl dense"><thead><tr><th>名称</th><th>大小</th><th>状态</th><th></th></tr></thead><tbody>${rows}</tbody></table>`
          : empty("空目录", "", "#i-folder")}
      </div>
    </div>`;
  }

  async function load(manual) {
    try {
      const d = await (await api("/system-sources")).json();
      if (d.error) throw new Error(d.error);
      const os = d.os || d.groups?.os || {};
      const soft = d.software || d.groups?.software || {};
      const osItems = os.items || os || [];
      const softItems = soft.items || soft || [];
      $("srcTip").innerHTML = Callout(
        `操作系统 ${(Array.isArray(osItems) ? osItems : []).length} · 软件 ${(Array.isArray(softItems) ? softItems : []).length} · 大小走 8.74 异步 du`,
        { tone: "ok", title: "系统 / 软件源" }
      );
      $("srcGrid").innerHTML =
        cardGroup(os.label || "操作系统镜像", "os", Array.isArray(osItems) ? osItems : []) +
        cardGroup(soft.label || "软件应用镜像", "software", Array.isArray(softItems) ? softItems : []);
      c.querySelectorAll("[data-du]").forEach(btn => {
        btn.onclick = () => scanSize(btn.dataset.du, btn.dataset.name);
      });
      if (manual) toast("已刷新", "ok");
    } catch (e) {
      $("srcTip").innerHTML = Callout(e.message || String(e), { tone: "danger", title: "加载失败" });
    }
  }

  async function scanSize(group, name) {
    try {
      const r = await postJSON("/system-source-size", { targets: [[group, name]] });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "提交失败");
      toast(d.message || `已提交采集 (${d.kicked ?? 0})`, "ok");
      if (pollTimer) clearInterval(pollTimer);
      let n = 0;
      pollTimer = setInterval(() => {
        n += 1;
        load(false);
        if (n >= 20) { clearInterval(pollTimer); pollTimer = null; }
      }, 2000);
    } catch (e) {
      toast(e.message || String(e), "error");
    }
  }

  load();
});
