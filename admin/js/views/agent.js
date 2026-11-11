// 视图：AI 运维 —— 对话 UI 重写（流式 / 会话落库 / 执行后对话内日志监控）。
import { register } from "../core/router.js";
import { api } from "../core/api.js";
import { $, esc, toast, md2html, confirmBox } from "../core/dom.js";
import { Icon, Badge } from "../core/components.js";
import { state } from "../core/state.js";

const FALLBACK = [
  { id: "inspector", name: "巡检", desc: "失败分析 · 磁盘 · 上游", presets: ["最近有哪些镜像失败？分析根因", "检查磁盘和上游"] },
  { id: "scheduler", name: "调度", desc: "定时 · Ansible 分流", presets: ["大库改 Ansible，小库保持本机", "给 conda 写每天凌晨定时"] },
  { id: "ansible", name: "Ansible", desc: "Playbook · 同步执行", presets: ["列出 playbook", "用 Ansible 同步一次 conda"] },
  { id: "ops", name: "综合", desc: "端到端运维编排", presets: ["巡检并优化失败大库的定时"] },
];

const AGENT_META = {
  inspector: { icon: "act", desc: "失败分析 · 磁盘 · 上游" },
  scheduler: { icon: "timer", desc: "定时 · Ansible 分流" },
  ansible: { icon: "term", desc: "Playbook · 同步执行" },
  ops: { icon: "spark", desc: "端到端运维编排" },
};

const STATUS_LABEL = {
  running: "运行中", ok: "成功", fail: "失败", timeout: "超时",
  syncing: "同步中", success: "成功", pending: "等待", error: "错误",
};
const STATUS_TONE = {
  running: "info", ok: "ok", fail: "danger", timeout: "warn",
  syncing: "info", success: "ok", pending: "warn", error: "danger",
};

register("agent", function (c) {
  let agents = FALLBACK.slice();
  let agentId = localStorage.getItem("mirror.agentId") || "inspector";
  let sessionId = sessionStorage.getItem("mirror.agentSession") || "";
  let busy = false;
  let abort = null;
  let sessCache = [];
  let histFilter = "";
  const monitors = new Map();

  c._cleanup = () => {
    stopAllMonitors();
    if (abort) { try { abort.abort(); } catch {} abort = null; }
  };

  c.innerHTML = `
    <div class="ag-shell" id="agShell">
      <aside class="ag-rail" id="agRail">
        <div class="ag-rail-top">
          <div class="ag-rail-brand">
            <span class="ag-rail-ico">${Icon("spark")}</span>
            <div>
              <strong>会话</strong>
              <em>AI 运维</em>
            </div>
          </div>
          <button type="button" class="ag-icon-btn ag-rail-close" id="agRailClose" title="收起侧栏">${Icon("chev-l")}</button>
        </div>
        <button type="button" class="ag-new" id="agNew">${Icon("plus")}<span>新对话</span></button>
        <div class="ag-rail-search">
          ${Icon("search")}
          <input type="search" id="agHistQ" placeholder="搜索会话…" autocomplete="off" />
        </div>
        <div class="ag-rail-list" id="agHist"></div>
      </aside>

      <section class="ag-stage">
        <header class="ag-top">
          <button type="button" class="ag-icon-btn ag-rail-open" id="agRailOpen" title="会话列表">${Icon("menu")}</button>
          <div class="ag-agent" id="agAgent">
            <button type="button" class="ag-agent-btn" id="agAgentBtn" aria-haspopup="listbox" aria-expanded="false">
              <span class="ag-agent-ico" id="agAgentIco">${Icon("spark")}</span>
              <span class="ag-agent-meta">
                <strong id="agAgentName">巡检</strong>
                <em id="agAgentDesc">失败分析 · 磁盘 · 上游</em>
              </span>
              <span class="ag-agent-chev">${Icon("chev-d")}</span>
            </button>
            <div class="ag-agent-menu" id="agAgentMenu" role="listbox" hidden></div>
          </div>
          <div class="ag-top-spacer"></div>
          <span class="ag-top-pill" id="agTopPill">就绪</span>
          <button type="button" class="ag-icon-btn" id="agClear" title="删除当前会话">${Icon("trash")}</button>
        </header>

        <div class="ag-feed" id="agThread">
          <div class="ag-welcome" id="agEmpty"></div>
        </div>

        <button type="button" class="ag-jump" id="agJump" hidden title="回到底部">${Icon("chev-d")}</button>

        <footer class="ag-dock">
          <div class="ag-dock-inner">
            <textarea id="agAsk" class="ag-ask" rows="1" placeholder="描述你想做的运维操作…" spellcheck="false"></textarea>
            <div class="ag-dock-bar">
              <span class="ag-dock-hint"><kbd>Enter</kbd> 发送 · <kbd>⇧Enter</kbd> 换行</span>
              <button type="button" class="ag-go" id="agAskBtn" title="发送">${Icon("send")}<span>发送</span></button>
              <button type="button" class="ag-halt" id="agStopBtn" title="停止" hidden>${Icon("stop")}<span>停止</span></button>
            </div>
          </div>
        </footer>
      </section>
      <div class="ag-scrim" id="agScrim" hidden></div>
    </div>`;

  const cur = () => agents.find(a => a.id === agentId) || agents[0];

  function agentDesc(a) {
    return a.desc || AGENT_META[a.id]?.desc || "";
  }
  function agentIcon(a) {
    return AGENT_META[a.id]?.icon || "spark";
  }

  function stopAllMonitors() {
    for (const m of monitors.values()) {
      try { m.stop(); } catch {}
    }
    monitors.clear();
  }

  function setRail(open) {
    const shell = $("agShell");
    if (!shell) return;
    shell.classList.toggle("rail-open", open);
    const scrim = $("agScrim");
    if (scrim) scrim.hidden = !open || window.matchMedia("(min-width: 900px)").matches;
  }

  function renderAgentMenu() {
    const menu = $("agAgentMenu");
    if (!menu) return;
    menu.innerHTML = agents.map(a => `
      <button type="button" class="ag-agent-opt ${a.id === agentId ? "active" : ""}" data-agent="${esc(a.id)}" role="option" aria-selected="${a.id === agentId}">
        <span class="ag-agent-opt-ico">${Icon(agentIcon(a))}</span>
        <span>
          <strong>${esc(a.name)}</strong>
          <em>${esc(agentDesc(a))}</em>
        </span>
      </button>`).join("");
  }

  function syncAgentChrome() {
    const a = cur();
    const name = $("agAgentName");
    const desc = $("agAgentDesc");
    const ico = $("agAgentIco");
    if (name) name.textContent = a.name;
    if (desc) desc.textContent = agentDesc(a);
    if (ico) ico.innerHTML = Icon(agentIcon(a));
    const ta = $("agAsk");
    if (ta) ta.placeholder = `问「${a.name}」…`;
    renderAgentMenu();
    const welcome = $("agEmpty");
    if (welcome) paintWelcome(welcome);
  }

  function switchAgent(id) {
    if (!agents.some(a => a.id === id)) return;
    agentId = id;
    localStorage.setItem("mirror.agentId", id);
    syncAgentChrome();
    closeAgentMenu();
  }

  function closeAgentMenu() {
    const menu = $("agAgentMenu");
    const btn = $("agAgentBtn");
    if (menu) menu.hidden = true;
    if (btn) btn.setAttribute("aria-expanded", "false");
  }

  function toggleAgentMenu() {
    const menu = $("agAgentMenu");
    const btn = $("agAgentBtn");
    if (!menu || !btn) return;
    const open = menu.hidden;
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function paintWelcome(el) {
    const a = cur();
    el.innerHTML = `
      <div class="ag-welcome-hero">
        <div class="ag-welcome-mark">${Icon(agentIcon(a))}</div>
        <h2>${esc(a.name)} 助手</h2>
        <p>${esc(agentDesc(a) || "用自然语言巡检、调度与排查；触发同步后可在对话内跟日志。")}</p>
      </div>
      <div class="ag-suggest" id="agPresets">
        ${(a.presets || []).map(q => `
          <button type="button" class="ag-suggest-card" data-q="${esc(q)}">
            <span class="ag-suggest-ico">${Icon("zap")}</span>
            <span>${esc(q)}</span>
          </button>`).join("")}
      </div>
      <div class="ag-welcome-agents">
        ${agents.map(x => `
          <button type="button" class="ag-welcome-agent ${x.id === agentId ? "active" : ""}" data-agent="${esc(x.id)}">
            ${Icon(agentIcon(x))}
            <span>${esc(x.name)}</span>
          </button>`).join("")}
      </div>`;
  }

  function clearEmpty() {
    $("agEmpty")?.remove();
  }

  function resetThread(msg) {
    stopAllMonitors();
    const feed = $("agThread");
    feed.innerHTML = `<div class="ag-welcome" id="agEmpty"></div>`;
    paintWelcome($("agEmpty"));
    if (msg) {
      const p = $("agEmpty")?.querySelector(".ag-welcome-hero p");
      if (p) p.textContent = msg;
    }
    setPill("就绪");
    updateJump();
  }

  function feedInner() {
    let inner = $("agThread")?.querySelector(".ag-feed-inner");
    if (!inner) {
      clearEmpty();
      inner = document.createElement("div");
      inner.className = "ag-feed-inner";
      $("agThread").appendChild(inner);
    }
    return inner;
  }

  function appendMsg(role, html, cls = "") {
    const inner = feedInner();
    const el = document.createElement("article");
    el.className = `ag-row ${role} ${cls}`.trim();

    if (role === "user") {
      el.innerHTML = `<div class="ag-user">${html}</div>`;
    } else if (role === "bot") {
      el.innerHTML = `
        <div class="ag-bot-head">
          <span class="ag-bot-avatar">${Icon(agentIcon(cur()))}</span>
          <span class="ag-bot-label">${esc(cur().name)}</span>
          <button type="button" class="ag-copy" data-copy-msg title="复制回复" hidden>${Icon("copy")}</button>
        </div>
        <div class="ag-bot-body">${html}</div>`;
    } else if (role === "status") {
      el.innerHTML = `<div class="ag-status">${html}</div>`;
    } else {
      el.innerHTML = `<div class="ag-tools">${html}</div>`;
    }

    inner.appendChild(el);
    scrollThread(true);
    return el;
  }

  function botBody(el) {
    return el?.querySelector(".ag-bot-body") || el?.querySelector(".ag-tools") || el?.querySelector(".ag-status") || el;
  }

  function scrollThread(force) {
    const th = $("agThread");
    if (!th) return;
    const nearBottom = th.scrollHeight - th.scrollTop - th.clientHeight < 96;
    if (force || nearBottom) th.scrollTop = th.scrollHeight;
    updateJump();
  }

  function updateJump() {
    const th = $("agThread");
    const btn = $("agJump");
    if (!th || !btn) return;
    const nearBottom = th.scrollHeight - th.scrollTop - th.clientHeight < 120;
    btn.hidden = nearBottom || !th.querySelector(".ag-feed-inner");
  }

  function setBusy(on) {
    busy = on;
    const go = $("agAskBtn");
    const halt = $("agStopBtn");
    if (go) { go.disabled = on; go.hidden = !!on; }
    if (halt) halt.hidden = !on;
    setPill(on ? "生成中" : "就绪", on ? "busy" : "");
  }

  function setPill(text, cls = "") {
    const pill = $("agTopPill");
    if (!pill) return;
    pill.textContent = text;
    pill.className = `ag-top-pill ${cls}`.trim();
  }

  function skillArgsBrief(args) {
    if (!args || typeof args !== "object") return "";
    const parts = [];
    for (const [k, v] of Object.entries(args)) {
      if (v == null || v === "") continue;
      const s = String(v);
      parts.push(`${k}=${s.length > 28 ? s.slice(0, 26) + "…" : s}`);
      if (parts.length >= 3) break;
    }
    return parts.join(" ");
  }

  function resultBriefHtml(skill, result, ok) {
    if (!result || typeof result !== "object") return "";
    if (result.error) return `<span class="ag-err">${esc(result.error)}</span>`;
    const bits = [];
    if (skill === "run_playbook") {
      bits.push(`run_id=${result.run_id || "-"}`);
      bits.push(result.playbook || "");
      bits.push(`-l ${result.limit || ""}`);
      if (result.message) bits.push(result.message);
    } else if (skill === "trigger_sync") {
      bits.push(result.message || (ok ? "已触发" : "失败"));
      if (result.chosen_executor) bits.push(`via ${result.chosen_executor}`);
    } else if (skill === "list_playbooks") {
      const n = (result.playbooks || []).length || result.count || 0;
      bits.push(`${n} 个 playbook`);
    } else if (skill === "sync_logs") {
      const n = (result.app?.count || 0) + (result["app-02"]?.count || 0);
      bits.push(`${n} 行`);
    } else if (result.message) {
      bits.push(result.message);
    } else if (result.ok != null) {
      bits.push(result.ok ? "ok" : "fail");
    } else {
      const raw = JSON.stringify(result);
      bits.push(raw.length > 120 ? raw.slice(0, 118) + "…" : raw);
    }
    return esc(bits.filter(Boolean).join(" · "));
  }

  function mountSkillResult(hostEl, ev) {
    const card = document.createElement("div");
    card.className = `ag-step ${ev.ok === false ? "is-err" : "is-ok"}`;
    const args = skillArgsBrief(ev.args);
    card.innerHTML = `
      <div class="ag-step-rail" aria-hidden="true"></div>
      <div class="ag-step-main">
        <div class="ag-step-head">
          <code>${esc(ev.skill || "")}</code>
          ${Badge(ev.ok === false ? "失败" : "完成", ev.ok === false ? "danger" : "ok", { dot: true })}
          ${args ? `<span class="ag-step-args">${esc(args)}</span>` : ""}
        </div>
        <div class="ag-step-body">${resultBriefHtml(ev.skill, ev.result, ev.ok !== false)}</div>
      </div>`;
    hostEl.appendChild(card);
    if (ev.watch) mountMonitor(hostEl, ev.watch);
    scrollThread(true);
  }

  function statusBadge(st) {
    const s = st || "running";
    return Badge(STATUS_LABEL[s] || s, STATUS_TONE[s] || "muted", {
      dot: true, pulse: s === "running" || s === "syncing",
    });
  }

  function mountMonitor(hostEl, watch, opts = {}) {
    const kind = watch.kind || "";
    const mid = kind === "ansible" ? `ans-${watch.run_id}`
      : kind === "sync" ? `sync-${watch.key}`
      : kind === "logs" ? `logs-${watch.keyword || "all"}-${Date.now()}`
      : `w-${Date.now()}`;
    if (monitors.has(mid)) return;

    const resume = !!opts.resume;
    const el = document.createElement("div");
    el.className = "ag-mon";
    el.dataset.mid = mid;
    el.innerHTML = `
      <div class="ag-mon-head">
        <div class="ag-mon-title">
          <span class="ag-mon-ico">${Icon(kind === "ansible" ? "term" : "file")}</span>
          <strong>${esc(watch.title || "监控")}</strong>
          <span class="ag-mon-meta"></span>
        </div>
        <div class="ag-mon-actions">
          <span class="ag-mon-badge">${statusBadge("running")}</span>
          <button type="button" class="btn sm ghost ag-mon-pause">暂停</button>
          <button type="button" class="ag-icon-btn ag-mon-close" title="关闭">${Icon("x")}</button>
        </div>
      </div>
      <pre class="ag-mon-log">等待输出…</pre>
      <div class="ag-mon-foot"><span class="ag-mon-hint">每 3s 刷新</span></div>`;
    hostEl.appendChild(el);

    const logEl = el.querySelector(".ag-mon-log");
    const badgeEl = el.querySelector(".ag-mon-badge");
    const metaEl = el.querySelector(".ag-mon-meta");
    const hintEl = el.querySelector(".ag-mon-hint");
    const pauseBtn = el.querySelector(".ag-mon-pause");
    let timer = null;
    let paused = false;
    let stuckBottom = true;
    let lastText = "";
    let ticks = 0;

    logEl.addEventListener("scroll", () => {
      stuckBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 24;
    });

    function setLog(text) {
      const t = (text || "").trimEnd() || "(暂无输出)";
      if (t === lastText) return;
      lastText = t;
      logEl.textContent = t;
      if (stuckBottom) logEl.scrollTop = logEl.scrollHeight;
    }

    function setStatus(st, meta) {
      badgeEl.innerHTML = statusBadge(st);
      if (meta) metaEl.textContent = meta;
      el.classList.toggle("is-done", st && st !== "running" && st !== "syncing");
      el.classList.toggle("is-fail", st === "fail" || st === "timeout" || st === "error");
    }

    async function tick() {
      if (paused || !el.isConnected) return;
      ticks += 1;
      try {
        if (kind === "ansible") await pollAnsible();
        else if (kind === "sync") await pollSync();
        else if (kind === "logs") await pollLogs();
      } catch (e) {
        hintEl.textContent = `拉取失败：${e.message || e}`;
      }
    }

    async function pollAnsible() {
      const r = await api("/ansible/runs/" + encodeURIComponent(watch.run_id));
      const d = await r.json();
      if (d.error) { setStatus("fail", d.error); stop(); return; }
      const st = d.status || "running";
      setStatus(st, `${d.playbook || watch.playbook || ""} · -l ${d.lim || watch.limit || ""} · ${watch.run_id}`);
      let out = (d.output || "").trim();
      if (!out && st === "running") {
        try {
          const lr = await api("/ansible/log?lines=60");
          const ld = await lr.json();
          out = (ld.log || "").trim() || "执行中，等待 playbook 输出…";
        } catch {
          out = "执行中，等待 playbook 输出…";
        }
      }
      setLog(out);
      hintEl.textContent = st === "running"
        ? `实时日志 · 第 ${ticks} 次 · ${new Date().toLocaleTimeString()}`
        : `已结束 · rc=${d.rc ?? "-"} · ${d.finished_at || ""}`;
      if (st !== "running") stop(false);
    }

    async function pollSync() {
      const key = watch.key;
      let st = "syncing";
      let name = key;
      try {
        const mr = await api("/mirrors");
        const list = await mr.json();
        const m = (Array.isArray(list) ? list : []).find(x => x.key === key);
        if (m) {
          st = m.status || st;
          name = m.name || key;
        }
      } catch {}
      setStatus(st, `${name} · ${watch.executor || ""}`);
      const lr = await api(`/agent/logs?keyword=${encodeURIComponent(key)}&n=80`);
      const ld = await lr.json();
      const lines = []
        .concat(ld.app?.lines || [])
        .concat(ld["app-02"]?.lines || []);
      setLog(lines.length ? lines.join("\n") : `等待 [${key}] 相关日志…`);
      const done = st && st !== "syncing";
      hintEl.textContent = done
        ? `同步结束 · ${STATUS_LABEL[st] || st} · ${new Date().toLocaleTimeString()}`
        : `跟随镜像 status · 第 ${ticks} 次 · ${new Date().toLocaleTimeString()}`;
      if (done && ticks > 2) stop(false);
    }

    async function pollLogs() {
      const kw = watch.keyword || "";
      setStatus(resume ? "ok" : "running", kw ? `关键词 ${kw}` : "全量尾部");
      const lr = await api(`/agent/logs?keyword=${encodeURIComponent(kw)}&n=100`);
      const ld = await lr.json();
      const blocks = [];
      if (ld.app?.lines?.length) blocks.push("── app.log ──\n" + ld.app.lines.join("\n"));
      if (ld["app-02"]?.lines?.length) blocks.push("── app-02.log ──\n" + ld["app-02"].lines.join("\n"));
      setLog(blocks.join("\n\n") || "(空)");
      hintEl.textContent = resume
        ? `历史快照 · ${new Date().toLocaleTimeString()}`
        : `每 3s 刷新 · 第 ${ticks} 次 · ${new Date().toLocaleTimeString()}`;
      if (resume || ticks >= 40) stop(false);
    }

    function stop() {
      if (timer) { clearInterval(timer); timer = null; }
      pauseBtn.textContent = "已结束";
      pauseBtn.disabled = true;
    }

    pauseBtn.addEventListener("click", () => {
      if (pauseBtn.disabled) return;
      paused = !paused;
      pauseBtn.textContent = paused ? "继续" : "暂停";
      hintEl.textContent = paused ? "已暂停轮询" : "继续轮询…";
      if (!paused) tick();
    });
    el.querySelector(".ag-mon-close").addEventListener("click", () => {
      stop();
      monitors.delete(mid);
      el.remove();
    });

    timer = setInterval(tick, 3000);
    monitors.set(mid, { stop, el });
    tick();
    scrollThread(true);
  }

  function renderSessions() {
    const box = $("agHist");
    if (!box) return;
    const q = histFilter.trim().toLowerCase();
    const list = !q ? sessCache : sessCache.filter(s =>
      `${s.title || ""} ${s.agent || ""}`.toLowerCase().includes(q));
    if (!sessCache.length) {
      box.innerHTML = `<div class="ag-rail-empty">暂无历史会话</div>`;
      return;
    }
    if (!list.length) {
      box.innerHTML = `<div class="ag-rail-empty">无匹配会话</div>`;
      return;
    }
    box.innerHTML = list.map(s => `
      <button type="button" class="ag-sess ${s.id === sessionId ? "active" : ""}" data-sid="${esc(s.id)}">
        <strong>${esc(s.title || "对话")}</strong>
        <em>
          <span class="ag-sess-agent">${esc(s.agent || "")}</span>
          <span>${esc((s.updated_at || "").slice(5, 16))}</span>
          <span>${s.message_count || 0} 条</span>
        </em>
      </button>`).join("");
  }

  async function loadSessions() {
    try {
      const d = await (await api("/agent/sessions?limit=40")).json();
      sessCache = d.sessions || [];
      renderSessions();
    } catch {
      const box = $("agHist");
      if (box) box.innerHTML = `<div class="ag-rail-empty">历史加载失败</div>`;
    }
  }

  async function openSession(sid) {
    if (!sid || busy) return;
    sessionId = sid;
    sessionStorage.setItem("mirror.agentSession", sid);
    setRail(false);
    try {
      const d = await (await api("/agent/sessions/" + encodeURIComponent(sid))).json();
      if (d.error) { toast(d.error, "error"); return; }
      const sess = d.session || {};
      if (sess.agent) switchAgent(sess.agent);
      stopAllMonitors();
      const feed = $("agThread");
      feed.innerHTML = "";
      const msgs = d.messages || [];
      if (!msgs.length) {
        resetThread("空会话，可继续提问");
        loadSessions();
        return;
      }
      msgs.forEach(m => {
        if (m.role === "user") {
          appendMsg("user", esc(m.content || ""));
        } else if (m.role === "assistant") {
          const skills = (m.skills || []).map(s => `<code>${esc(s)}</code>`).join("");
          const bot = appendMsg("bot",
            (skills ? `<div class="ag-skills">${skills}</div>` : "") +
            `<div class="md">${md2html(m.content || "")}</div>`);
          enableCopy(bot, m.content || "");
          const watches = m.watches || [];
          if (watches.length) {
            const host = document.createElement("article");
            host.className = "ag-row tools";
            host.innerHTML = `<div class="ag-tools"></div>`;
            bot.before(host);
            watches.forEach(w => mountMonitor(host.querySelector(".ag-tools"), w, { resume: true }));
          }
        }
      });
      loadSessions();
      scrollThread(true);
    } catch (e) {
      toast(e.message || "加载失败", "error");
    }
  }

  function enableCopy(botEl, text) {
    const btn = botEl.querySelector("[data-copy-msg]");
    if (!btn) return;
    btn.hidden = !text;
    btn.onclick = async () => {
      try {
        await navigator.clipboard.writeText(text);
        toast("已复制");
      } catch {
        toast("复制失败", "error");
      }
    };
  }

  function newSession() {
    if (busy) return;
    sessionId = "";
    sessionStorage.removeItem("mirror.agentSession");
    resetThread();
    loadSessions();
    setRail(false);
    $("agAsk")?.focus();
  }

  async function ask(q) {
    q = (q || $("agAsk").value || "").trim();
    if (!q) { toast("请输入问题", "warn"); return; }
    if (busy) return;

    $("agAsk").value = "";
    autoGrow();
    appendMsg("user", esc(q));

    const statusEl = appendMsg("status",
      `<span class="ag-dots" aria-hidden="true"><i></i><i></i><i></i></span><span class="ag-status-text">连接中…</span>`);
    const resultsHost = appendMsg("tools", "", "ag-results");
    resultsHost.style.display = "none";
    const toolsBox = resultsHost.querySelector(".ag-tools");

    const bot = appendMsg("bot", `<span class="ag-stream"></span><span class="ag-caret"></span>`, "streaming");
    const streamEl = bot.querySelector(".ag-stream");
    const statusText = statusEl.querySelector(".ag-status-text");
    let raw = "";
    let skills = [];

    setBusy(true);
    abort = new AbortController();

    try {
      const headers = { "Content-Type": "application/json" };
      if (state.csrf) headers["X-CSRF-Token"] = state.csrf;
      const r = await fetch("/api/admin/agent/ask/stream", {
        method: "POST",
        headers,
        body: JSON.stringify({ question: q, agent: agentId, session_id: sessionId || undefined }),
        signal: abort.signal,
      });
      if (r.status === 401) { toast("未登录", "error"); return; }
      if (!r.ok || !r.body) {
        let err = "请求失败";
        try { err = (await r.json()).error || err; } catch {}
        statusEl.remove();
        resultsHost.remove();
        bot.classList.remove("streaming");
        bot.querySelector(".ag-caret")?.remove();
        streamEl.innerHTML = `<span class="ag-err">${esc(err)}</span>`;
        return;
      }

      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      let queue = "";
      let typing = false;
      let finished = false;

      const pump = () => {
        if (!queue) {
          typing = false;
          if (finished) finalize();
          return;
        }
        typing = true;
        const ch = queue[0];
        queue = queue.slice(1);
        raw += ch;
        streamEl.textContent = raw;
        scrollThread(false);
        setTimeout(pump, 18);
      };

      const finalize = () => {
        if (bot._finalized) return;
        bot._finalized = true;
        bot.classList.remove("streaming");
        bot.querySelector(".ag-caret")?.remove();
        statusEl.remove();
        const body = botBody(bot);
        const text = bot._answer || raw;
        streamEl.innerHTML = text
          ? `<div class="md">${md2html(text)}</div>`
          : `<span class="ag-err">（无回复）</span>`;
        if (bot._truncated) {
          const tip = document.createElement("div");
          tip.className = "ag-trunc";
          tip.textContent = "已达步数上限";
          body.prepend(tip);
        }
        if (skills.length) {
          const sl = document.createElement("div");
          sl.className = "ag-skills";
          sl.innerHTML = skills.map(s => `<code>${esc(s)}</code>`).join("");
          body.prepend(sl);
        }
        enableCopy(bot, text);
        if (toolsBox.childElementCount) {
          resultsHost.style.display = "";
          bot.before(resultsHost);
        } else {
          resultsHost.remove();
        }
        loadSessions();
        scrollThread(true);
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop() || "";
        for (const block of parts) {
          const line = block.split("\n").find(l => l.startsWith("data: "));
          if (!line) continue;
          let ev;
          try { ev = JSON.parse(line.slice(6)); } catch { continue; }
          const t = ev.type;
          if (t === "status") {
            statusText.textContent = ev.text || "…";
          } else if (t === "skill") {
            skills.push(ev.skill);
            const brief = skillArgsBrief(ev.args);
            statusText.textContent = brief
              ? `调用 ${ev.skill}（${brief}）…`
              : `调用 ${ev.skill}…`;
            const st = statusEl.querySelector(".ag-status");
            st?.querySelector(".ag-skills")?.remove();
            const sl = document.createElement("div");
            sl.className = "ag-skills";
            sl.innerHTML = skills.map(s => `<code>${esc(s)}</code>`).join("");
            st?.appendChild(sl);
          } else if (t === "skill_result") {
            resultsHost.style.display = "";
            mountSkillResult(toolsBox, ev);
            statusText.textContent = ev.watch
              ? `已挂载监控：${ev.watch.title || ev.skill}`
              : `${ev.skill} 完成`;
          } else if (t === "token") {
            if (statusEl.isConnected) statusEl.remove();
            queue += ev.t || "";
            if (!typing) pump();
          } else if (t === "done") {
            bot._answer = ev.answer || raw;
            bot._truncated = !!ev.truncated;
            if (ev.session_id) {
              sessionId = ev.session_id;
              sessionStorage.setItem("mirror.agentSession", sessionId);
            }
            finished = true;
            if (!typing && !queue) finalize();
          } else if (t === "error") {
            statusEl.remove();
            bot.classList.remove("streaming");
            bot.querySelector(".ag-caret")?.remove();
            streamEl.innerHTML = `<span class="ag-err">${esc(ev.error || "失败")}</span>`;
            bot._finalized = true;
            if (!toolsBox.childElementCount) resultsHost.remove();
            else { resultsHost.style.display = ""; bot.before(resultsHost); }
          }
        }
      }
      finished = true;
      if (!typing && !queue && !bot._finalized) finalize();
      if (typing || queue) {
        await new Promise(resolve => {
          const wait = () => {
            if (!typing && !queue) { finalize(); resolve(); }
            else setTimeout(wait, 40);
          };
          wait();
        });
      }
    } catch (e) {
      if (e.name === "AbortError") {
        statusEl.remove();
        bot.classList.remove("streaming");
        bot.querySelector(".ag-caret")?.remove();
        if (!bot._finalized) {
          bot._finalized = true;
          const text = bot._answer || raw;
          if (text) {
            streamEl.innerHTML = `<div class="md">${md2html(text)}</div>`;
            if (skills.length) {
              const sl = document.createElement("div");
              sl.className = "ag-skills";
              sl.innerHTML = skills.map(s => `<code>${esc(s)}</code>`).join("");
              botBody(bot).prepend(sl);
            }
            enableCopy(bot, text);
          } else {
            streamEl.innerHTML = `<span class="ag-err">已停止生成</span>`;
          }
          if (toolsBox.childElementCount) {
            resultsHost.style.display = "";
            bot.before(resultsHost);
          } else {
            resultsHost.remove();
          }
        }
        return;
      }
      statusEl.remove();
      bot.classList.remove("streaming");
      bot.querySelector(".ag-caret")?.remove();
      streamEl.innerHTML = `<span class="ag-err">${esc(e.message || "失败")}</span>`;
      if (!toolsBox.childElementCount) resultsHost.remove();
    } finally {
      setBusy(false);
      abort = null;
      scrollThread(true);
    }
  }

  function autoGrow() {
    const ta = $("agAsk");
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(160, Math.max(48, ta.scrollHeight)) + "px";
  }

  // —— 事件 ——
  $("agAgentBtn").addEventListener("click", e => {
    e.stopPropagation();
    if (!busy) toggleAgentMenu();
  });
  $("agAgentMenu").addEventListener("click", e => {
    const b = e.target.closest("[data-agent]");
    if (b && !busy) switchAgent(b.dataset.agent);
  });
  const onDocClick = e => {
    if (!e.target.closest("#agAgent")) closeAgentMenu();
  };
  document.addEventListener("click", onDocClick);
  const prevCleanup = c._cleanup;
  c._cleanup = () => {
    document.removeEventListener("click", onDocClick);
    if (typeof prevCleanup === "function") prevCleanup();
  };

  $("agThread").addEventListener("click", e => {
    const q = e.target.closest("[data-q]");
    if (q) { ask(q.dataset.q); return; }
    const a = e.target.closest(".ag-welcome-agent[data-agent]");
    if (a && !busy) switchAgent(a.dataset.agent);
  });
  $("agHist").addEventListener("click", e => {
    const b = e.target.closest("[data-sid]");
    if (b) openSession(b.dataset.sid);
  });
  $("agHistQ").addEventListener("input", e => {
    histFilter = e.target.value || "";
    renderSessions();
  });
  $("agNew").addEventListener("click", newSession);
  $("agAskBtn").addEventListener("click", () => ask());
  $("agStopBtn").addEventListener("click", () => {
    if (abort) {
      try { abort.abort(); } catch {}
      abort = null;
    }
    setBusy(false);
    toast("已停止", "warn");
  });
  $("agAsk").addEventListener("input", autoGrow);
  $("agAsk").addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      ask();
    }
  });
  $("agThread").addEventListener("scroll", updateJump, { passive: true });
  $("agJump").addEventListener("click", () => scrollThread(true));
  $("agRailOpen").addEventListener("click", () => setRail(true));
  $("agRailClose").addEventListener("click", () => setRail(false));
  $("agScrim").addEventListener("click", () => setRail(false));
  $("agClear").addEventListener("click", async () => {
    if (sessionId) {
      if (await confirmBox("删除当前会话及其全部消息？", "删除会话", "删除")) {
        try {
          await api("/agent/sessions/" + encodeURIComponent(sessionId), { method: "DELETE" });
        } catch {}
        newSession();
        toast("已删除");
      }
    } else {
      if (abort) abort.abort();
      setBusy(false);
      resetThread();
    }
  });

  api("/agent/skills").then(r => r.json()).then(d => {
    if (d.agents?.length) {
      agents = d.agents.map(a => ({
        id: a.id,
        name: a.name,
        desc: a.desc || AGENT_META[a.id]?.desc || "",
        presets: a.presets || (FALLBACK.find(f => f.id === a.id) || {}).presets || [],
      }));
    }
    if (!agents.some(a => a.id === agentId)) agentId = agents[0].id;
    switchAgent(agentId);
  }).catch(() => switchAgent(agentId));

  resetThread();
  loadSessions().then(async () => {
    const prefill = sessionStorage.getItem("mirror.agentPrefill");
    // 首页快捷提问：开新会话，避免与旧会话/openSession 竞态叠消息
    if (prefill) {
      sessionStorage.removeItem("mirror.agentPrefill");
      sessionStorage.removeItem("mirror.agentSession");
      sessionId = "";
      resetThread();
      setTimeout(() => ask(prefill), 120);
      return;
    }
    const sid = sessionStorage.getItem("mirror.agentSession");
    if (sid) await openSession(sid);
  });
});
