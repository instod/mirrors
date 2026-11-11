// 视图：上游源纳管 —— 实际在用的同步上游目录 + 关联镜像 + 延迟探测 + 增删改。
import { register } from "../core/router.js";
import { api, postJSON, putJSON, delReq } from "../core/api.js";
import { $, esc, empty, skel, toast, confirmBox, openModal, closeModal, modalFoot } from "../core/dom.js";
import { Icon, Badge, Toolbar, Table, statCard, Field, Input, Select, Textarea } from "../core/components.js";
import { latencyTone } from "../core/format.js";
import { poller } from "../core/poller.js";
import { state } from "../core/state.js";

const CATS = ["国内镜像", "发行版官方", "生信数据库", "厂商软件源", "自动发现", "其他"];

register("upstreams", function (c) {
  c._cleanup = () => { if (c._stop) c._stop(); };
  let warn = 500, crit = 1500, paused = false, filter = "";

  c.innerHTML = `
    ${Toolbar('<span class="dim small" id="upHint">纳管实际同步上游 · 关联镜像自动反查</span>',
      `<button class="btn sm" id="upDiscover">${Icon("search")}从镜像发现</button>
       <button class="btn sm primary" id="upAdd">${Icon("plus")}添加上游</button>
       <button class="btn sm" id="upRefresh">${Icon("refresh")}刷新探测</button>
       <button class="btn sm" id="upAutoBtn">${Icon("stop")}暂停自动刷新</button>`)}
    <div class="stats-grid mb" id="upSummary" style="grid-template-columns:repeat(auto-fit,minmax(160px,1fr))">${'<div class="skel" style="height:66px"></div>'.repeat(4)}</div>
    <div class="card">
      <div class="card-head wrap" style="flex-wrap:wrap">
        <h3>已纳管上游源</h3>
        <span class="sub">谁在用 · 能不能连 · 延迟多少</span>
        <div class="right row wrap">
          <input id="upFilter" class="input" placeholder="筛选名称 / 主机 / 镜像…" style="width:200px;padding:5px 10px;font-size:12.5px" />
          <span class="small muted mono" id="upTs"></span>
        </div>
      </div>
      <div class="card-body tight" id="upWrap">${Table(
        [{ t: "上游源" }, { t: "协议" }, { t: "探测地址" }, { t: "关联镜像" }, { t: "延迟", r: 1 }, { t: "状态" }, { t: "操作" }],
        "", { emptyHtml: skel(5) })}</div>
    </div>`;

  const tbody = c.querySelector("#upWrap table.tbl tbody");
  const TONE_VAR = { ok: "var(--ok)", warn: "var(--warn)", danger: "var(--danger)" };
  let cache = [];

  function mirrorChips(mirrors) {
    const list = mirrors || [];
    if (!list.length) return `<span class="muted small">未关联</span>`;
    const top = list.slice(0, 4);
    const more = list.length - top.length;
    return top.map(m =>
      `<span class="badge info" style="margin:1px 2px" title="${esc(m.key)}">${esc(m.name || m.key)}</span>`
    ).join("") + (more > 0 ? `<span class="small muted">+${more}</span>` : "");
  }

  function renderRows(ups) {
    const q = filter.trim().toLowerCase();
    const rows = !q ? ups : ups.filter(u => {
      const blob = [
        u.name, u.host, u.protocol, u.url, u.category, u.note,
        ...(u.mirrors || []).map(m => `${m.key} ${m.name}`),
      ].join(" ").toLowerCase();
      return blob.includes(q);
    });
    tbody.innerHTML = rows.length ? rows.map(u => {
      const tone = latencyTone(u.latency_ms, warn, crit);
      let st;
      if (u.probe_skipped) st = Badge("未探测", "", { dot: true });
      else if (u.ok) st = Badge("正常", "ok", { dot: true });
      else if (u.timeout) st = Badge("超时", "warn", { dot: true });
      else st = Badge("不可达", "danger", { dot: true });
      const enabled = u.enabled !== false;
      return `<tr data-id="${esc(u.id)}" style="${enabled ? "" : "opacity:.55"}">
        <td>
          <div style="font-weight:600">${esc(u.name)}</div>
          <div class="mono small muted">${esc(u.host)}</div>
          ${u.category ? `<div class="small muted">${esc(u.category)}</div>` : ""}
        </td>
        <td>${Badge(u.protocol || "-", "info")}</td>
        <td class="mono small" style="max-width:240px">
          <a href="${esc(u.url || "#")}" target="_blank" rel="noopener" title="${esc(u.source || u.url || "")}"
             style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block">${esc(u.url || "-")}</a>
        </td>
        <td style="max-width:280px">${mirrorChips(u.mirrors)}
          ${(u.mirrors || []).length ? `<div class="small muted mt">${u.mirrors.length} 个镜像</div>` : ""}</td>
        <td class="num" style="font-weight:600;color:${TONE_VAR[tone] || "var(--text-mute)"}">${u.latency_ms != null ? u.latency_ms + " ms" : "-"}</td>
        <td>${st}${u.http_code > 0 ? `<div class="mono small muted">${u.http_code}</div>` : ""}</td>
        <td>
          <div class="row" style="gap:4px">
            <button class="btn sm" data-act="edit" title="编辑">${Icon("edit")}</button>
            <button class="btn sm" data-act="del" title="删除">${Icon("trash")}</button>
          </div>
        </td>
      </tr>`;
    }).join("") : `<tr><td colspan="7">${empty(q ? "无匹配上游" : "尚未纳管上游源", q ? "换个关键词" : "点击「从镜像发现」或「添加上游」", "#i-globe")}</td></tr>`;
  }

  async function tick() {
    if (paused) return;
    try {
      const [ru, rs] = await Promise.all([api("/upstreams"), api("/settings")]);
      const d = await ru.json();
      const s = await rs.json().catch(() => ({}));
      warn = parseInt(s.upstream_latency_warn) || 500;
      crit = parseInt(s.upstream_latency_crit) || 1500;
      const hint = $("upHint");
      if (hint) hint.textContent = `已纳管 ${d.total || 0} 个上游 · 慢 ≥${warn}ms · 差 ≥${crit}ms · 每 60 秒探测`;
      cache = d.upstreams || [];
      $("upTs").textContent = "更新于 " + (d.timestamp || "-");
      const ok = cache.filter(u => u.ok).length;
      const linked = cache.filter(u => (u.mirrors || []).length).length;
      const cats = new Set(cache.map(u => u.category).filter(Boolean));
      const reach = cache.filter(u => u.latency_ms != null);
      const avg = reach.length ? Math.round(reach.reduce((a, u) => a + u.latency_ms, 0) / reach.length) : null;
      $("upSummary").innerHTML =
        statCard("已纳管", cache.length, { sub: `${cats.size} 类`, tone: "info", icon: "globe" }) +
        statCard("可达", ok + " / " + cache.length, { sub: "HTTP 探测成功", tone: ok === cache.length ? "ok" : "warn", icon: "check" }) +
        statCard("已关联镜像", linked, { sub: "至少被 1 个镜像使用", tone: "ok", icon: "db" }) +
        statCard("平均延迟", avg != null ? avg + " ms" : "--", { sub: `慢阈值 ${warn} ms`, tone: avg != null && avg < warn ? "ok" : "warn", icon: "act" });
      renderRows(cache);
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="7">${empty("加载失败", e.message, "#i-warn")}</td></tr>`;
    }
  }

  function openEditor(item) {
    const isNew = !item;
    const it = item || { name: "", host: "", protocol: "https", url: "", source: "", category: "其他", note: "", enabled: true };
    openModal(isNew ? "添加上游源" : "编辑上游源", `
      <div class="form-grid" style="display:grid;gap:12px">
        ${Field("显示名称", Input("upName", it.name || "", { id: "upName", placeholder: "如：南京大学 NJU" }))}
        ${Field("主机 host", Input("upHost", it.host || "", { id: "upHost", placeholder: "如：mirror.nju.edu.cn" }))}
        ${Field("协议", Select("upProto", [
          { v: "https", t: "https" }, { v: "http", t: "http" }, { v: "rsync", t: "rsync" },
          { v: "ftp", t: "ftp" }, { v: "rsync/https", t: "rsync/https" }, { v: "ftp/rsync", t: "ftp/rsync" },
        ], it.protocol || "https", { id: "upProto" }))}
        ${Field("探测地址 URL", Input("upUrl", it.url || "", { id: "upUrl", placeholder: "https://... 用于延迟探测" }))}
        ${Field("实际同步地址", Input("upSource", it.source || "", { id: "upSource", placeholder: "rsync://... 或 ftp://..." }))}
        ${Field("分类", Select("upCat", CATS.map(c => ({ v: c, t: c })), it.category || "其他", { id: "upCat" }))}
        ${Field("备注", Textarea("upNote", it.note || "", { id: "upNote", rows: 2 }))}
        <label class="row" style="gap:8px;cursor:pointer">
          <input type="checkbox" id="upEnabled" ${it.enabled !== false ? "checked" : ""}> 启用探测
        </label>
      </div>
    `, {
      foot: modalFoot(isNew ? "添加" : "保存", async () => {
        const body = {
          name: $("upName").value.trim(),
          host: $("upHost").value.trim(),
          protocol: $("upProto").value,
          url: $("upUrl").value.trim(),
          source: $("upSource").value.trim(),
          category: $("upCat").value,
          note: $("upNote").value.trim(),
          enabled: $("upEnabled").checked,
        };
        if (!body.name || !body.host) {
          toast("请填写名称和主机", "warn");
          const err = new Error("校验"); err.silent = true; throw err;
        }
        if (!body.url) body.url = "https://" + body.host.replace(/\/$/, "") + "/";
        let r;
        if (isNew) r = await postJSON("/upstreams", body);
        else r = await putJSON("/upstreams/" + encodeURIComponent(item.id), body);
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.error || "保存失败");
        toast(d.message || "已保存");
        closeModal();
        await tick();
      }),
    });
  }

  $("upWrap").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const tr = btn.closest("tr[data-id]");
    if (!tr) return;
    const id = tr.dataset.id;
    const item = cache.find(u => u.id === id);
    if (!item) return;
    if (btn.dataset.act === "edit") openEditor(item);
    else if (btn.dataset.act === "del") {
      if (!(await confirmBox(`确认删除上游源「${item.name}」(${item.host})？\n不会影响镜像同步配置，仅从纳管目录移除。`, "删除上游源", "删除"))) return;
      const r = await delReq("/upstreams/" + encodeURIComponent(id));
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { toast(d.error || "删除失败", "error"); return; }
      toast("已删除");
      tick();
    }
  });

  $("upAdd").addEventListener("click", () => openEditor(null));
  $("upDiscover").addEventListener("click", async () => {
    const btn = $("upDiscover");
    const old = btn.innerHTML;
    btn.disabled = true; btn.innerHTML = '<span class="spin"></span>扫描中';
    try {
      const r = await postJSON("/upstreams/discover", {});
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || "发现失败");
      toast(d.message || `新纳入 ${d.count || 0} 个`);
      await tick();
    } catch (e) {
      toast(e.message || "发现失败", "error");
    } finally {
      btn.disabled = false; btn.innerHTML = old;
    }
  });
  $("upRefresh").addEventListener("click", tick);
  $("upAutoBtn").addEventListener("click", () => {
    paused = !paused;
    $("upAutoBtn").innerHTML = paused ? Icon("play") + "继续自动刷新" : Icon("stop") + "暂停自动刷新";
  });
  $("upFilter").addEventListener("input", () => {
    filter = $("upFilter").value;
    renderRows(cache);
  });

  tick();
  c._stop = poller(() => { if (state.currentRoute === "upstreams") tick(); }, 60000);
});
