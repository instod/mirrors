// 视图：日志中心 —— 登录 / 访问 / 同步 / 运维日报（由侧栏下拉切入）。
import { register, LOG_TAB_IDS } from "../core/router.js";
import { api, postJSON } from "../core/api.js";
import { $, esc, empty, skel, debounce, md2html, toast } from "../core/dom.js";
import { Icon, Badge, Toolbar, Table, Pager, SearchBox, Callout, statCard } from "../core/components.js";
import { fmtBytes } from "../core/format.js";

const TAB_KEY = "admin_logs_tab";
const LOGIN_ACTIONS = "login,login_failed,logout";

const ACTION_OPTS = [
  { v: "", t: "全部登录相关" },
  { v: "login", t: "登录成功" },
  { v: "login_failed", t: "登录失败" },
  { v: "logout", t: "退出" },
];

const ACTION_LABEL = {
  login: "登录成功", login_failed: "登录失败", logout: "退出",
};

function todayStr() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function shiftDay(ymd, delta) {
  const [y, m, d] = String(ymd).split("-").map(Number);
  const dt = new Date(y, m - 1, d);
  dt.setDate(dt.getDate() + delta);
  return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
}

function fmtDur(sec) {
  const s = Number(sec) || 0;
  if (s <= 0) return "-";
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}

register("logs", function (c) {
  let tab = window._logsTab || localStorage.getItem(TAB_KEY) || "login";
  if (!LOG_TAB_IDS.includes(tab)) tab = "login";
  try { localStorage.setItem(TAB_KEY, tab); } catch {}
  window._logsTab = tab;

  const login = { page: 1, action: "" };
  const audit = { page: 1, action: "", target: "" };

  const tips = {
    login: Callout("仅含后台登录 / 失败 / 退出记录，不含镜像操作审计与前台访问流量。", { tone: "info", title: "登录日志 · SQLite 审计表" }),
    audit: Callout("管理端写操作全量审计（同步触发、crontab、Ansible、镜像 CRUD、设置变更等）。", { tone: "info", title: "操作审计 · audit_log" }),
    access: Callout("默认「按 IP 汇总」：某个 IP 访问了哪些镜像源、下载了哪些文件及流量。可切换「明细列表」。数据来自 https://mirrors.tibhpc.net 的 nginx 访问日志。", { tone: "info", title: "访问日志 · https://mirrors.tibhpc.net" }),
    sync: Callout("各镜像同步脚本输出。「新目录」= 现行路径，「旧目录」= 迁移前路径。", { tone: "ok", title: "同步日志 · 镜像 log 文件" }),
    daily: Callout("按日汇总同步与登录事实；可用日期选择回溯历史，也可手动「重新生成」。", { tone: "info", title: "运维日报 · 定时汇总 · 可回溯" }),
  };

  c.innerHTML = `
    ${Toolbar('<span class="dim small" id="lgHint"></span>',
      `<button class="btn sm" id="lgRefresh">${Icon("refresh")}刷新</button>`)}
    <div id="lgTip" class="mb">${tips[tab]}</div>

    <div id="lgLogin" class="log-panel" data-kind="login" style="${tab === "login" ? "" : "display:none"}">
      <div class="card">
        <div class="card-head wrap" style="flex-wrap:wrap">
          <h3>登录记录</h3>
          <span class="sub">登录 / 失败 / 退出</span>
          <div class="right row wrap">
            <select id="lgAction" class="select" style="width:150px;padding:5px 10px;font-size:12.5px">
              ${ACTION_OPTS.map(o => `<option value="${o.v}">${o.t}</option>`).join("")}
            </select>
            <button class="btn sm primary" id="lgFilter">${Icon("search")}筛选</button>
          </div>
        </div>
        <div class="card-body tight" id="lgLoginWrap">${Table(
          [{ t: "时间" }, { t: "操作人" }, { t: "动作" }, { t: "详情" }],
          "", { emptyHtml: skel(5) })}</div>
        <div class="row" style="padding:0 16px"><span id="lgPage" style="flex:1"></span></div>
      </div>
    </div>

    <div id="lgAudit" class="log-panel" data-kind="audit" style="${tab === "audit" ? "" : "display:none"}">
      <div class="card">
        <div class="card-head wrap" style="flex-wrap:wrap">
          <h3>操作审计</h3>
          <span class="sub">全量管理动作</span>
          <div class="right row wrap">
            <input class="input" id="lgAuditAction" placeholder="action 过滤" style="width:140px;padding:5px 10px;font-size:12.5px" />
            <input class="input" id="lgAuditTarget" placeholder="target 过滤" style="width:140px;padding:5px 10px;font-size:12.5px" />
            <button class="btn sm primary" id="lgAuditFilter">${Icon("search")}筛选</button>
          </div>
        </div>
        <div class="card-body tight" id="lgAuditWrap">${Table(
          [{ t: "时间" }, { t: "操作人" }, { t: "动作" }, { t: "目标" }, { t: "详情" }],
          "", { emptyHtml: skel(5) })}</div>
        <div class="row" style="padding:0 16px"><span id="lgAuditPage" style="flex:1"></span></div>
      </div>
    </div>

    <div id="lgAccess" class="log-panel" data-kind="access" style="${tab === "access" ? "" : "display:none"}">
      <div class="stats-grid mb" id="lgAccessStats" style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr))">${'<div class="skel" style="height:66px"></div>'.repeat(4)}</div>
      <div class="row wrap mb" id="lgAccessTops" style="gap:12px;align-items:stretch"></div>
      <div class="card">
        <div class="card-head wrap" style="flex-wrap:wrap">
          <h3>访问记录</h3>
          <span class="sub">https://mirrors.tibhpc.net</span>
          <div class="right row wrap">
            <div class="tabs" id="lgAccessView" style="margin:0">
              <button type="button" class="tab active" data-aview="ip">按 IP 汇总</button>
              <button type="button" class="tab" data-aview="detail">明细列表</button>
            </div>
            ${SearchBox("lgAccessQ", "", "IP / 镜像 / 路径 / UA…")}
            <label class="small dim row" style="gap:6px;cursor:pointer" title="关闭后包含首页与 /api 等非镜像请求">
              <input type="checkbox" id="lgAccessDl" checked> 仅镜像下载
            </label>
            <select id="lgAccessN" class="select" style="width:140px;padding:5px 10px;font-size:12.5px">
              <option value="100">最近 100 条</option>
              <option value="300" selected>最近 300 条</option>
              <option value="500">最近 500 条</option>
              <option value="1000">最近 1000 条</option>
            </select>
          </div>
        </div>
        <div class="card-body" id="lgAccessBody">${skel(5, 80)}</div>
      </div>
    </div>

    <div id="lgSync" class="log-panel" data-kind="sync" style="${tab === "sync" ? "" : "display:none"}">
      <div class="card">
        <div class="card-head wrap" style="flex-wrap:wrap">
          <h3>同步脚本日志</h3>
          <span class="sub">按镜像分文件</span>
          <div class="right row wrap">
            <label class="small dim row" style="gap:6px;cursor:pointer"><input type="checkbox" id="lgSyncErr"> 只看含错误</label>
            <select id="lgSyncN" class="select" style="width:140px;padding:5px 10px;font-size:12.5px">
              <option value="100">每镜像 100 行</option><option value="200" selected>每镜像 200 行</option>
            </select>
          </div>
        </div>
        <div class="card-body" id="lgSyncBody">${skel(3, 80)}</div>
      </div>
    </div>

    <div id="lgDaily" class="log-panel" data-kind="daily" style="${tab === "daily" ? "" : "display:none"}">
      <div class="card mb">
        <div class="card-body" style="padding:12px 16px">
          <div class="row wrap" style="gap:10px;align-items:center">
            <button type="button" class="btn sm" id="lgDailyPrev" title="前一天">${Icon("chev-l")}前一天</button>
            <input type="date" class="input" id="lgDailyDate" style="width:160px;padding:5px 10px;font-size:13px" />
            <button type="button" class="btn sm" id="lgDailyNext" title="后一天">后一天${Icon("chev-r")}</button>
            <button type="button" class="btn sm" id="lgDailyToday">今天</button>
            <span class="small muted mono" id="lgDailyMeta" style="margin-left:auto"></span>
            <button type="button" class="btn sm primary" id="lgDailyRegen">${Icon("refresh")}重新生成</button>
          </div>
        </div>
      </div>
      <div class="stats-grid mb" id="lgDailyStats" style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr))">${'<div class="skel" style="height:66px"></div>'.repeat(4)}</div>
      <div class="card mb">
        <div class="card-head"><h3>同步明细</h3><span class="sub" id="lgDailySyncSub">当日同步过的镜像</span></div>
        <div class="card-body tight" id="lgDailySync">${skel(3)}</div>
      </div>
      <div class="card">
        <div class="card-head"><h3>日报正文</h3><span class="sub" id="lgDailyBy"></span></div>
        <div class="card-body" id="lgDailyBody">${skel(4, 80)}</div>
      </div>
    </div>`;

  const hint = $("lgHint");
  if (hint) {
    const labels = { login: "登录日志", audit: "操作审计", access: "访问日志", sync: "同步日志", daily: "运维日报" };
    hint.textContent = `当前：${labels[tab] || tab}`;
  }

  const loginTbody = c.querySelector("#lgLoginWrap table.tbl tbody");
  const auditTbody = c.querySelector("#lgAuditWrap table.tbl tbody");

  function actTone(a) {
    if (a === "login_failed") return "danger";
    if (a === "logout") return "info";
    return "ok";
  }

  async function loadLogin(page) {
    if (!loginTbody) return;
    login.page = page || 1;
    login.action = ($("lgAction")?.value || "").trim();
    loginTbody.innerHTML = `<tr><td colspan="4" style="padding:14px">${skel(4)}</td></tr>`;
    try {
      const p = new URLSearchParams({ page: login.page, page_size: 50 });
      if (login.action) p.set("action", login.action);
      else p.set("actions", LOGIN_ACTIONS);
      const d = await (await api("/audit?" + p.toString())).json();
      const items = d.items || [];
      loginTbody.innerHTML = items.length ? items.map(it => `<tr>
        <td class="num" style="white-space:nowrap">${esc(it.created_at || "-")}</td>
        <td class="mono">${esc(it.operator || "admin")}</td>
        <td>${Badge(ACTION_LABEL[it.action] || it.action || "-", actTone(it.action), { dot: true, title: it.action || "" })}</td>
        <td>${it.detail ? `<span class="small">${esc(it.detail)}</span>` : "-"}</td>
      </tr>`).join("") : `<tr><td colspan="4">${empty("没有匹配的登录记录", "尚无登录 / 失败 / 退出事件", "#i-key")}</td></tr>`;
      $("lgPage").innerHTML = Pager(d.page || login.page, d.total_pages || 1, d.total || 0, 50);
    } catch (e) { loginTbody.innerHTML = `<tr><td colspan="4">${empty("加载失败", e.message, "#i-warn")}</td></tr>`; }
  }

  async function loadAudit(page) {
    if (!auditTbody) return;
    audit.page = page || 1;
    audit.action = ($("lgAuditAction")?.value || "").trim();
    audit.target = ($("lgAuditTarget")?.value || "").trim();
    auditTbody.innerHTML = `<tr><td colspan="5" style="padding:14px">${skel(4)}</td></tr>`;
    try {
      const p = new URLSearchParams({ page: audit.page, page_size: 50 });
      if (audit.action) p.set("action", audit.action);
      if (audit.target) p.set("target", audit.target);
      const d = await (await api("/audit?" + p.toString())).json();
      const items = d.items || [];
      auditTbody.innerHTML = items.length ? items.map(it => `<tr>
        <td class="num" style="white-space:nowrap">${esc(it.created_at || "-")}</td>
        <td class="mono">${esc(it.operator || "-")}</td>
        <td><code>${esc(it.action || "-")}</code></td>
        <td class="mono">${esc(it.target || "-")}</td>
        <td class="small">${esc((it.detail || "").slice(0, 200) || "-")}</td>
      </tr>`).join("") : `<tr><td colspan="5">${empty("没有匹配的审计记录", "调整筛选条件试试", "#i-inbox")}</td></tr>`;
      $("lgAuditPage").innerHTML = Pager(d.page || audit.page, d.total_pages || 1, d.total || 0, 50);
    } catch (e) {
      auditTbody.innerHTML = `<tr><td colspan="5">${empty("加载失败", e.message, "#i-warn")}</td></tr>`;
    }
  }

  async function loadAccess() {
    const body = $("lgAccessBody");
    const statsEl = $("lgAccessStats");
    const topsEl = $("lgAccessTops");
    if (!body) return;
    body.innerHTML = skel(5, 80);
    if (statsEl) statsEl.innerHTML = '<div class="skel" style="height:66px"></div>'.repeat(4);
    if (topsEl) topsEl.innerHTML = "";
    const view = c._accessView || "ip";
    try {
      const p = new URLSearchParams({ n: $("lgAccessN")?.value || "300" });
      const q = $("lgAccessQ")?.value.trim(); if (q) p.set("q", q);
      p.set("downloads", $("lgAccessDl")?.checked ? "1" : "0");
      const d = await (await api("/logs/access?" + p.toString())).json();
      if (d.error) {
        body.innerHTML = empty("读取失败", d.error, "#i-warn");
        return;
      }
      const host = d.host || "https://mirrors.tibhpc.net";
      const sum = d.summary || {};
      if (statsEl) {
        statsEl.innerHTML =
          statCard("请求数", sum.reqs || 0, { icon: "globe", tone: "info" }) +
          statCard("下载量", fmtBytes(sum.bytes || 0), { icon: "arrow-up", tone: "ok" }) +
          statCard("独立 IP", sum.ips || 0, { icon: "user", tone: "" }) +
          statCard("涉及镜像", sum.mirrors || 0, { icon: "db", tone: "brand" });
      }
      if (topsEl) {
        const mir = (d.top_mirrors || []).slice(0, 5);
        const ips = (d.top_ips || []).slice(0, 5);
        const chip = (title, rows, kind) => `
          <div class="card" style="flex:1;min-width:220px;margin:0">
            <div class="card-head"><h3 style="font-size:13px">${title}</h3><span class="sub">点击筛选</span></div>
            <div class="card-body" style="padding:10px 14px">
              ${rows.length ? rows.map(r => kind === "m"
                ? `<button type="button" class="spread lg-acc-filter" data-q="${esc(r.key)}" style="gap:8px;padding:4px 0;font-size:12.5px;width:100%;background:none;border:0;cursor:pointer;text-align:left;color:inherit">
                    <span style="font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.key)}">${esc(r.name || r.key)}</span>
                    <span class="mono muted">${fmtBytes(r.bytes)} · ${r.reqs}次</span>
                  </button>`
                : `<button type="button" class="spread lg-acc-filter" data-q="${esc(r.ip)}" style="gap:8px;padding:4px 0;font-size:12.5px;width:100%;background:none;border:0;cursor:pointer;text-align:left;color:inherit">
                    <span class="mono" style="font-weight:600">${esc(r.ip)}</span>
                    <span class="mono muted">${fmtBytes(r.bytes)} · ${r.reqs}次</span>
                  </button>`).join("") : `<div class="small muted">暂无</div>`}
            </div>
          </div>`;
        topsEl.innerHTML = chip("热门镜像源", mir, "m") + chip("活跃客户端 IP", ips, "i");
        topsEl.querySelectorAll(".lg-acc-filter").forEach(b => b.addEventListener("click", () => {
          const inp = $("lgAccessQ");
          if (inp) { inp.value = b.dataset.q || ""; loadAccess(); }
        }));
      }

      const shortUa = (ua) => {
        const s = String(ua || "").trim();
        if (!s) return "-";
        if (s.length <= 48) return s;
        return s.slice(0, 46) + "…";
      };
      const statusTone = (st) => {
        if (st === 200 || st === 206) return "ok";
        if (st >= 400) return "danger";
        if (st >= 300) return "warn";
        return "";
      };

      if (view === "ip") {
        body.classList.remove("tight");
        const groups = d.by_ip || [];
        if (!groups.length) {
          body.innerHTML = empty(
            q ? "无匹配访问" : "暂无镜像访问记录",
            q ? "换个关键字试试，或关闭「仅镜像下载」" : "等待客户端访问 https://mirrors.tibhpc.net 后出现",
            "#i-globe");
          return;
        }
        body.innerHTML = `<div class="small muted mb">按客户端 IP 汇总：访问了哪些镜像源、下载了哪些数据 · 扫描 ${d.scanned || 0} 行日志</div>` +
          groups.map(g => {
            const mirHtml = (g.mirrors || []).map(m =>
              `${Badge(m.name || m.key, "info")} <span class="mono small muted">${fmtBytes(m.bytes)} / ${m.reqs}次</span>`
            ).join(" · ") || '<span class="muted">-</span>';
            const files = (g.files || []).map(f => `
              <tr>
                <td class="num" style="white-space:nowrap">${esc(f.time || "-")}</td>
                <td>${esc(f.mirror_name || f.mirror_key || "-")}</td>
                <td><a class="mono small" href="${esc(host + (f.path || ""))}" target="_blank" rel="noopener" title="${esc(f.path || "")}">${esc(f.file || f.path || "-")}</a></td>
                <td class="num">${fmtBytes(f.bytes || 0)}</td>
              </tr>`).join("");
            return `<details class="log-sync-item" style="margin-bottom:8px;border:1px solid var(--border);border-radius:9px;overflow:hidden;border-left:3px solid var(--info)" open>
              <summary style="cursor:pointer;padding:10px 13px;background:var(--panel-2);display:flex;gap:10px;align-items:center;flex-wrap:wrap">
                <strong class="mono" style="font-size:13.5px">${esc(g.ip)}</strong>
                ${Badge(`${g.reqs} 次请求`, "info", { dot: true })}
                ${Badge(fmtBytes(g.bytes), "ok")}
                <span class="small muted" style="flex:1;min-width:160px">访问源：${mirHtml}</span>
                <span class="small muted" title="${esc(g.ua || "")}">${esc(shortUa(g.ua))}</span>
              </summary>
              <div style="padding:8px 10px">
                ${Table([{ t: "时间" }, { t: "镜像源" }, { t: "下载内容" }, { t: "大小", r: 1 }], files)}
              </div>
            </details>`;
          }).join("");
      } else {
        const items = d.items || [];
        body.classList.add("tight");
        body.innerHTML = items.length ? Table(
          [{ t: "时间" }, { t: "客户端 IP" }, { t: "镜像源" }, { t: "下载内容" }, { t: "大小", r: 1 }, { t: "状态" }, { t: "客户端" }],
          items.map(it => {
            const mirror = it.mirror_name || it.mirror_key
              ? `<div style="font-weight:600">${esc(it.mirror_name || it.mirror_key)}</div>
                 <div class="mono small muted">${esc(it.mirror_key || "")}</div>`
              : `<span class="muted">-</span>`;
            const file = it.file || it.path || "-";
            const href = it.url || (host + (it.path || ""));
            return `<tr>
              <td class="num" style="white-space:nowrap">${esc(it.time || "-")}</td>
              <td class="mono" style="font-weight:600"><button type="button" class="lg-acc-filter" data-q="${esc(it.ip || "")}" style="background:none;border:0;padding:0;cursor:pointer;color:inherit;font:inherit">${esc(it.ip || "-")}</button></td>
              <td>${mirror}</td>
              <td><a class="mono small" style="word-break:break-all" href="${esc(href)}" target="_blank" rel="noopener" title="${esc(it.path || "")}">${esc(file)}</a></td>
              <td class="num">${fmtBytes(it.bytes || 0)}</td>
              <td>${Badge(String(it.status ?? "-"), statusTone(it.status), { dot: true })}</td>
              <td class="small muted" title="${esc(it.ua || "")}">${esc(shortUa(it.ua))}</td>
            </tr>`;
          }).join("")
        ) : empty(
          q ? "无匹配访问" : "暂无镜像访问记录",
          q ? "换个关键字试试，或关闭「仅镜像下载」" : "等待客户端访问 https://mirrors.tibhpc.net 后出现",
          "#i-globe");
        body.querySelectorAll(".lg-acc-filter").forEach(b => b.addEventListener("click", () => {
          const inp = $("lgAccessQ");
          if (inp) { inp.value = b.dataset.q || ""; loadAccess(); }
        }));
      }
    } catch (e) {
      body.innerHTML = empty("读取失败", e.message, "#i-warn");
    }
  }

  async function loadSync() {
    const body = $("lgSyncBody"); if (!body) return;
    body.innerHTML = skel(3, 80);
    try {
      const p = new URLSearchParams({ n: $("lgSyncN").value });
      if ($("lgSyncErr").checked) p.set("error", "1");
      const d = await (await api("/logs/sync?" + p.toString())).json();
      const items = d.items || [];
      if (!items.length) {
        body.innerHTML = empty("没有同步脚本日志", $("lgSyncErr").checked ? "当前无含错误的同步日志" : "同步日志按镜像分文件存放", "#i-sync");
        return;
      }
      body.innerHTML = `<div class="small muted mb"><span class="badge ok">同步日志</span> 共 ${d.total} 个镜像 · 各取最新日志尾部</div>` + items.map(it => `
        <details class="log-sync-item" style="margin-bottom:8px;border:1px solid var(--border);border-radius:9px;overflow:hidden;border-left:3px solid var(--ok)">
          <summary style="cursor:pointer;padding:9px 13px;background:var(--panel-2);display:flex;gap:9px;align-items:center;flex-wrap:wrap">
            <strong style="font-size:13px">${esc(it.name)}</strong>
            <span class="mono small muted">${esc(it.file)}</span>
            ${it.has_error ? Badge("含错误", "danger", { dot: true }) : Badge("正常", "ok", { dot: true })}
            ${Badge(it.source === "new" ? "新目录" : "旧目录", "", { title: it.source === "new" ? "现行日志路径" : "迁移前旧路径" })}
            <span class="small muted" style="margin-left:auto">${esc(it.mtime)} · ${it.count} 行</span>
          </summary>
          <div style="padding:10px">${it.error ? empty("读取失败", it.error, "#i-warn")
            : `<pre class="codebox" style="max-height:44vh">${(it.lines || []).map(l => esc(l).replace(/(\[ERROR\]|失败|❌)/g, '<span class="err">$1</span>')).join("\n") || "(空日志)"}</pre>`}</div>
        </details>`).join("");
    } catch (e) { body.innerHTML = empty("读取失败", e.message, "#i-warn"); }
  }

  let dailyDay = todayStr();

  function syncDailyDateInput() {
    const el = $("lgDailyDate");
    if (el) el.value = dailyDay;
    const next = $("lgDailyNext");
    if (next) next.disabled = dailyDay >= todayStr();
  }

  function renderDailySync(items) {
    const list = items || [];
    if (!list.length) {
      return empty("该日无同步记录", "调度或手动触发后会出现在此", "#i-sync");
    }
    const tone = (st) => {
      if (st === "success") return "ok";
      if (st === "fail") return "danger";
      if (st === "timeout") return "warn";
      return "info";
    };
    return Table(
      [{ t: "时间" }, { t: "镜像" }, { t: "状态" }, { t: "耗时", r: 1 }, { t: "触发" }, { t: "节点" }],
      list.map(it => `<tr>
        <td class="num small" style="white-space:nowrap">${esc((it.started_at || "").slice(11, 19) || "-")}</td>
        <td><div style="font-weight:600">${esc(it.name || it.key || "-")}</div>
            <div class="mono small muted">${esc(it.key || "")}</div></td>
        <td>${Badge(it.status || "-", tone(it.status), { dot: true })}</td>
        <td class="num small">${esc(fmtDur(it.duration_sec))}</td>
        <td class="small">${esc(it.trigger_type || "-")}${it.trigger_by ? ` / ${esc(it.trigger_by)}` : ""}</td>
        <td class="mono small muted">${esc(it.node || "-")}</td>
      </tr>`).join(""),
      { dense: true }
    );
  }

  async function loadDaily(day) {
    if (day) dailyDay = day;
    syncDailyDateInput();
    $("lgDailyStats").innerHTML = '<div class="skel" style="height:66px"></div>'.repeat(4);
    $("lgDailySync").innerHTML = skel(3);
    $("lgDailyBody").innerHTML = skel(4, 80);
    const meta = $("lgDailyMeta");
    if (meta) meta.textContent = "加载中…";
    const by = $("lgDailyBy");
    if (by) by.textContent = "";
    try {
      const r = await api("/daily-report?date=" + encodeURIComponent(dailyDay) + "&generate=1");
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "加载失败");
      const facts = d.facts || {};
      const counts = facts.counts || {};
      const login = facts.login_counts || {};
      const succ = counts.success || 0;
      const fail = counts.fail || 0;
      const timeout = counts.timeout || 0;
      const tot = facts.sync_total || 0;
      $("lgDailyStats").innerHTML =
        statCard("同步次数", tot, { icon: "sync", tone: "brand" }) +
        statCard("成功", succ, { icon: "check", tone: "ok" }) +
        statCard("失败 / 超时", `${fail} / ${timeout}`, { icon: "warn", tone: (fail || timeout) ? "danger" : "" }) +
        statCard("登录成功", login.login || 0, { icon: "key", tone: "info" });
      const sub = $("lgDailySyncSub");
      if (sub) sub.textContent = tot ? `共 ${tot} 次 · ${dailyDay}` : dailyDay;
      $("lgDailySync").innerHTML = renderDailySync(facts.sync_items || []);
      const src = d.generated_by === "ai" ? "自动撰写" : d.generated_by === "fallback" ? "结构化底稿" : (d.generated_by || "");
      if (by) by.textContent = [src, d.generated_at ? `生成于 ${d.generated_at}` : ""].filter(Boolean).join(" · ");
      if (meta) {
        meta.textContent = d.status === "empty" ? "当日事实较少" : (d.status === "fallback" ? "已用结构化底稿" : "已就绪");
      }
      const body = $("lgDailyBody");
      if (d.content) body.innerHTML = `<div class="md daily-report-md">${md2html(d.content)}</div>`;
      else body.innerHTML = empty("暂无日报正文", "点击「重新生成」汇总当日事实", "#i-file");
    } catch (e) {
      $("lgDailyStats").innerHTML = "";
      $("lgDailySync").innerHTML = empty("加载失败", e.message, "#i-warn");
      $("lgDailyBody").innerHTML = empty("日报加载失败", e.message, "#i-warn");
      if (meta) meta.textContent = "";
    }
  }

  async function regenDaily() {
    const btn = $("lgDailyRegen");
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span>生成中'; }
    try {
      const r = await postJSON("/daily-report", { date: dailyDay, use_ai: false });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "生成失败");
      toast(d.message || "日报已更新");
      await loadDaily(dailyDay);
    } catch (e) {
      if (e.message !== "CSRF") toast(e.message || "生成失败", "error");
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = Icon("refresh") + "重新生成"; }
    }
  }

  function loadCurrent() {
    if (tab === "login") loadLogin();
    else if (tab === "audit") loadAudit();
    else if (tab === "access") loadAccess();
    else if (tab === "sync") loadSync();
    else loadDaily();
  }

  $("lgRefresh").addEventListener("click", loadCurrent);

  if (tab === "login") {
    $("lgFilter")?.addEventListener("click", () => loadLogin(1));
    $("lgAction")?.addEventListener("change", () => loadLogin(1));
    $("lgPage")?.addEventListener("click", e => {
      const b = e.target.closest("[data-page]");
      if (b && !b.disabled) loadLogin(parseInt(b.dataset.page) || 1);
    });
  }
  if (tab === "audit") {
    $("lgAuditFilter")?.addEventListener("click", () => loadAudit(1));
    $("lgAuditAction")?.addEventListener("keydown", e => { if (e.key === "Enter") loadAudit(1); });
    $("lgAuditTarget")?.addEventListener("keydown", e => { if (e.key === "Enter") loadAudit(1); });
    $("lgAuditPage")?.addEventListener("click", e => {
      const b = e.target.closest("[data-page]");
      if (b && !b.disabled) loadAudit(parseInt(b.dataset.page) || 1);
    });
  }
  if (tab === "access") {
    c._accessView = "ip";
    const accessSearch = debounce(loadAccess, 350);
    $("lgAccessQ")?.addEventListener("input", accessSearch);
    $("lgAccessQ")?.addEventListener("keydown", e => { if (e.key === "Enter") loadAccess(); });
    $("lgAccessN")?.addEventListener("change", loadAccess);
    $("lgAccessDl")?.addEventListener("change", loadAccess);
    $("lgAccessView")?.addEventListener("click", e => {
      const b = e.target.closest("[data-aview]");
      if (!b) return;
      c._accessView = b.dataset.aview;
      $("lgAccessView").querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t === b));
      loadAccess();
    });
  }
  if (tab === "sync") {
    $("lgSyncErr")?.addEventListener("change", loadSync);
    $("lgSyncN")?.addEventListener("change", loadSync);
  }
  if (tab === "daily") {
    syncDailyDateInput();
    $("lgDailyDate")?.addEventListener("change", () => {
      const v = $("lgDailyDate")?.value;
      if (v) loadDaily(v);
    });
    $("lgDailyPrev")?.addEventListener("click", () => loadDaily(shiftDay(dailyDay, -1)));
    $("lgDailyNext")?.addEventListener("click", () => {
      const n = shiftDay(dailyDay, 1);
      if (n <= todayStr()) loadDaily(n);
    });
    $("lgDailyToday")?.addEventListener("click", () => loadDaily(todayStr()));
    $("lgDailyRegen")?.addEventListener("click", regenDaily);
  }

  loadCurrent();
});
