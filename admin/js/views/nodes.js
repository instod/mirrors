// 视图：入站节点池 —— Ansible inventory + running 标记 + 闭环并发。
import { register } from "../core/router.js";
import { api } from "../core/api.js";
import { $, esc, empty, skel } from "../core/dom.js";
import { Icon, Badge, Toolbar, Table, Callout, statCard } from "../core/components.js";

register("nodes", function (c) {
  c.innerHTML = `
    ${Toolbar(
      '<span class="dim small">入站下载器池 bnode1–30 · 与 Admission 共帽 · 出站仍走 8.13</span>',
      `<button class="btn sm primary" id="ndRefresh">${Icon("refresh")}刷新</button>`
    )}
    <div id="ndTip" class="mb"></div>
    <div class="stats-grid mb" id="ndStats" style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr))">${skel(4)}</div>
    <div class="dash-grid">
      <div class="card">
        <div class="card-head"><h3>Inventory 分组</h3><span class="sub">ansible-inventory</span></div>
        <div class="card-body tight" id="ndGroups">${skel(4)}</div>
      </div>
      <div class="card">
        <div class="card-head"><h3>Admission 帽</h3><span class="sub">与 wrapper 统一</span></div>
        <div class="card-body" id="ndCaps">${skel(3)}</div>
      </div>
    </div>
    <div class="card mt">
      <div class="card-head"><h3>bnode 池</h3><span class="sub" id="ndPoolSub">1–30</span></div>
      <div class="card-body tight" id="ndPool">${skel(6)}</div>
    </div>`;

  $("ndRefresh").onclick = load;

  async function load() {
    try {
      const [invR, loopR] = await Promise.all([api("/ansible/inventory"), api("/loop")]);
      const inv = await invR.json();
      const loop = await loopR.json();
      if (!invR.ok) throw new Error(inv.error || "inventory 失败");
      render(inv, loop);
    } catch (e) {
      $("ndTip").innerHTML = Callout(e.message || String(e), { tone: "danger", title: "节点池加载失败" });
    }
  }

  function render(inv, loop) {
    const groupList = Array.isArray(inv.groups) ? inv.groups : [];
    const bnodes = [];
    for (const g of groupList) {
      if ((g.name || "") === "bnode" && Array.isArray(g.hosts)) {
        bnodes.push(...g.hosts);
      }
    }
    // 若 children 展开不全，从所有 hosts 扫 bnodeN
    if (!bnodes.length) {
      for (const g of groupList) {
        for (const h of (g.hosts || [])) {
          if (/^bnode([1-9]|[12][0-9]|30)$/.test(h)) bnodes.push(h);
        }
      }
    }
    const uniq = [...new Set(bnodes)];
    uniq.sort((a, b) => {
      const na = parseInt(String(a).replace(/\D/g, ""), 10) || 0;
      const nb = parseInt(String(b).replace(/\D/g, ""), 10) || 0;
      return na - nb;
    });

    const caps = (loop && loop.caps) || {};
    const tiers = caps.tier_counts || {};
    const syncing = loop.syncing_count ?? 0;
    const q = loop.resume_queue_count ?? 0;
    const st = loop.status || "unknown";
    const tone = st === "ok" ? "ok" : st === "degraded" ? "warn" : "danger";

    $("ndTip").innerHTML = Callout(
      `闭环 ${st} · 同步中 ${syncing} · 续跑队列 ${q} · bnode ${uniq.length} 台登记 · inventory 主机合计 ${inv.hosts_total ?? "—"}`,
      { tone, title: "入站节点 · 控制面 8.74 调度" }
    );
    $("ndStats").innerHTML = [
      statCard("闭环", esc(st), { tone, icon: "shield" }),
      statCard("同步中", String(syncing), { icon: "sync" }),
      statCard("bnode 登记", String(uniq.length), { icon: "cpu" }),
      statCard("续跑队列", String(q), { icon: "inbox" }),
    ].join("");

    $("ndGroups").innerHTML = groupList.length
      ? Table(
          [{ t: "分组" }, { t: "主机数" }, { t: "子组" }],
          groupList.map(g => `<tr>
            <td class="mono">${esc(g.name)}</td>
            <td class="num">${(g.hosts || []).length}</td>
            <td class="small muted">${esc((g.children || []).join(", ") || "—")}</td>
          </tr>`).join("")
        )
      : empty("无分组", "inventory 为空或解析失败", "#i-warn");

    const maxP = caps.max_parallel ?? "6";
    const maxH = caps.max_huge ?? "1";
    const maxL = caps.max_large ?? "2";
    $("ndCaps").innerHTML = `
      <div class="mono small mb">total ≤ ${esc(String(maxP))}
        · huge ≤ ${esc(String(maxH))}
        · large ≤ ${esc(String(maxL))}</div>
      <div class="row" style="gap:8px;flex-wrap:wrap">
        ${["huge", "large", "medium", "small"].map(t =>
          Badge(`${t} ${tiers[t] ?? 0}`, t === "huge" || t === "large" ? "warn" : "muted")
        ).join("")}
      </div>
      <div class="small muted mt">SSH 触发不变 · 单镜像单写者 · 禁止 -l bnode 打满</div>`;

    $("ndPoolSub").textContent = `${uniq.length} 台`;
    $("ndPool").innerHTML = uniq.length
      ? `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:8px">
          ${uniq.map(n => `
            <div class="card" style="padding:10px 12px;margin:0">
              <div class="mono" style="font-weight:600">${esc(n)}</div>
              <div class="small muted">入站执行节点</div>
            </div>`).join("")}
        </div>`
      : empty("未解析到 bnode", "请检查 Ansible inventory", "#i-cpu");
  }

  load();
});
