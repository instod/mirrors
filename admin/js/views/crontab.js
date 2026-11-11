// 视图：定时任务 —— 时间/镜像源/同步脚本列表 + 设置/删除（右键或按钮）。
import { register } from "../core/router.js";
import { api, putJSON } from "../core/api.js";
import { $, esc, toast, confirmBox, empty, openModal, closeModal, modalFoot } from "../core/dom.js";
import { Icon, Badge, Toolbar, Field, Input, Select } from "../core/components.js";
import { poller } from "../core/poller.js";
import { state } from "../core/state.js";

let _tasks = [];
let _mirrors = [];
let _ctxMenu = null;
let _onDocClick = null;
let _onDocScroll = null;
let _doRender = null;

function hideCtx() {
  if (_ctxMenu) { _ctxMenu.remove(); _ctxMenu = null; }
}

function executorBadge(ex) {
  if (ex === "ansible") return Badge("Ansible", "info", { title: "ai_dispatch → bnode 池" });
  if (ex === "wrapper") return Badge("本机", "brand", { title: "wrapper.py 本机执行" });
  return Badge("其他", "", { title: "非标准调度命令" });
}

function cronRow(t) {
  const expr = `${t.minute} ${t.hour} ${t.dom} ${t.month} ${t.dow}`;
  const mirrorLabel = t.mirror_name
    ? `${t.mirror_name}${t.mirror_key ? ` (${t.mirror_key})` : ""}`
    : (t.mirror_key || t.title || "—");
  const script = t.sync_script || "—";
  return `<div class="cron-row" data-id="${t.id}" title="右键或点击设置">
    <div class="cron-sched">
      <div class="cron-sched-main">${Badge(t.schedule || expr, "brand", { title: expr })}</div>
      <div class="cron-sched-expr mono small muted" title="${esc(expr)}">${esc(expr)}</div>
    </div>
    <div class="cron-mirror" title="${esc(mirrorLabel)}">
      <div class="cron-title">${esc(mirrorLabel)}</div>
      ${t.note ? `<div class="cron-note">${esc(t.note)}</div>` : ""}
    </div>
    <div class="cron-exec">${executorBadge(t.executor)}</div>
    <code class="cron-script" title="${esc(script)}">${esc(script)}</code>
    <div class="cron-acts">
      <button type="button" class="btn sm" data-act="settings" title="设置">${Icon("set")}设置</button>
    </div>
  </div>`;
}

function cronGroup(title, items, tone) {
  if (!items.length) return "";
  return `<div class="card cron-group">
    <div class="card-head">
      <span class="cron-dot" style="background:var(--${tone})"></span>
      <h3>${title}</h3>
      <span class="sub">${items.length} 个任务</span>
    </div>
    <div class="card-body tight">
      <div class="cron-list">
        <div class="cron-head">
          <div>时间</div>
          <div>镜像源</div>
          <div>执行方式</div>
          <div>同步脚本</div>
          <div>操作</div>
        </div>
        ${items.map(cronRow).join("")}
      </div>
    </div>
  </div>`;
}

async function loadMirrors() {
  try {
    const r = await api("/mirrors");
    if (!r.ok) return;
    const list = await r.json();
    _mirrors = Array.isArray(list) ? list : [];
  } catch { _mirrors = []; }
}

async function deleteTask(task) {
  if (!(await confirmBox(
    `确认删除任务「${task.mirror_name || task.title || task.sync_script || "#" + task.id}」？`,
    "删除任务",
    "删除"
  ))) return false;
  const r = await api("/crontab/task", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: task.id }),
  });
  const d = await r.json();
  if (!r.ok) { toast(d.error || "删除失败", "error"); return false; }
  toast(d.message || "已删除");
  return true;
}

const EXEC_OPTS = [
  { v: "wrapper", t: "本机 wrapper（单机跑脚本）" },
  { v: "ansible", t: "Ansible 分发（ai_dispatch → bnode）" },
];

function openSettings(task) {
  hideCtx();
  const opts = [{ v: "", t: "（不绑定镜像）" }].concat(
    _mirrors.map(m => ({
      v: m.key,
      t: `${m.name || m.key} (${m.key})${m.sync_script ? "" : " · 无脚本"}`,
    }))
  );
  const curExec = (task.executor === "ansible" || task.executor === "wrapper")
    ? task.executor : "wrapper";
  const body = openModal("任务设置", `
    <div class="callout mb">修改后写回同步调度机 crontab。执行方式切换会改写为 wrapper 或 ai_dispatch。</div>
    <div class="form-grid">
      ${Field("分", Input("minute", task.minute, { mono: true, required: true }), "0–59 或 * / */N")}
      ${Field("时", Input("hour", task.hour, { mono: true, required: true }), "0–23 或 * / */N")}
      ${Field("日", Input("dom", task.dom, { mono: true, required: true }))}
      ${Field("月", Input("month", task.month, { mono: true, required: true }))}
      <div class="span2">${Field("周", Input("dow", task.dow, { mono: true, required: true }), "0–7 或 *（0/7=周日）")}</div>
      <div class="span2">${Field("镜像源", Select("mirror_key", opts, task.mirror_key || ""))}</div>
      <div class="span2">${Field("执行方式", Select("executor", EXEC_OPTS, curExec), "Ansible = 经 ai_dispatch 分发到 bnode 池")}</div>
      <div class="span2">${Field("同步脚本", Input("sync_script", task.sync_script || "", { mono: true, placeholder: "/hpcfs/fpublic/.../xxx.py" }), "选择镜像后可自动带出；须为 /hpcfs/fpublic/ 下绝对路径")}</div>
      <div class="span2">${Field("当前命令", `<code class="cron-script" style="white-space:normal;word-break:break-all;display:block">${esc(task.command || "")}</code>`, "保存后若脚本或执行方式变化将重建")}</div>
    </div>
    <div class="spread mt">
      <button type="button" class="btn danger" id="cronDelBtn">${Icon("trash")}删除任务</button>
      <span id="cronSetMsg" class="small muted"></span>
    </div>`, {
    wide: true,
    foot: modalFoot("保存", async () => {
      const root = $("modalBody");
      const g = (n) => root.querySelector(`[name="${n}"]`);
      const payload = {
        id: task.id,
        minute: g("minute").value.trim(),
        hour: g("hour").value.trim(),
        dom: g("dom").value.trim(),
        month: g("month").value.trim(),
        dow: g("dow").value.trim(),
        mirror_key: g("mirror_key").value.trim(),
        sync_script: g("sync_script").value.trim(),
        executor: g("executor").value.trim(),
      };
      const r = await api("/crontab/task", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const d = await r.json();
      if (!r.ok) {
        const msg = d.error || "保存失败";
        $("cronSetMsg").textContent = msg;
        throw new Error(msg);
      }
      closeModal();
      toast(d.message || "已保存");
      if (_doRender) _doRender();
    }),
  });

  const mirSel = body.querySelector('[name="mirror_key"]');
  const scriptIn = body.querySelector('[name="sync_script"]');
  mirSel.addEventListener("change", () => {
    const m = _mirrors.find(x => x.key === mirSel.value);
    if (m && m.sync_script) scriptIn.value = m.sync_script;
  });

  body.querySelector("#cronDelBtn").addEventListener("click", async () => {
    try {
      if (await deleteTask(task)) {
        closeModal();
        if (_doRender) _doRender();
      }
    } catch (e) { toast("删除失败：" + e.message, "error"); }
  });
}

function openCreate(preset = {}) {
  hideCtx();
  const opts = [{ v: "", t: "请选择镜像…" }].concat(
    _mirrors.filter(m => m.sync_script).map(m => ({
      v: m.key,
      t: `${m.name || m.key} (${m.key})`,
    }))
  );
  const body = openModal("新建定时任务", `
    <div class="callout mb">新建一行写入同步调度机 crontab。默认定时每天 02:00。</div>
    <div class="form-grid">
      ${Field("分", Input("minute", preset.minute || "0", { mono: true, required: true }))}
      ${Field("时", Input("hour", preset.hour || "2", { mono: true, required: true }))}
      ${Field("日", Input("dom", preset.dom || "*", { mono: true, required: true }))}
      ${Field("月", Input("month", preset.month || "*", { mono: true, required: true }))}
      <div class="span2">${Field("周", Input("dow", preset.dow || "*", { mono: true, required: true }))}</div>
      <div class="span2">${Field("镜像源", Select("mirror_key", opts, preset.mirror_key || ""))}</div>
      <div class="span2">${Field("执行方式", Select("executor", EXEC_OPTS, preset.executor || "wrapper"))}</div>
      <div class="span2">${Field("同步脚本", Input("sync_script", preset.sync_script || "", { mono: true, placeholder: "/hpcfs/fpublic/.../xxx.py" }))}</div>
    </div>
    <div class="mt"><span id="cronSetMsg" class="small muted"></span></div>`, {
    wide: true,
    foot: modalFoot("创建", async () => {
      const root = $("modalBody");
      const g = (n) => root.querySelector(`[name="${n}"]`);
      const payload = {
        minute: g("minute").value.trim(),
        hour: g("hour").value.trim(),
        dom: g("dom").value.trim(),
        month: g("month").value.trim(),
        dow: g("dow").value.trim(),
        mirror_key: g("mirror_key").value.trim(),
        sync_script: g("sync_script").value.trim(),
        executor: g("executor").value.trim(),
      };
      const r = await api("/crontab/task", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const d = await r.json();
      if (!r.ok) {
        const msg = d.error || "创建失败";
        $("cronSetMsg").textContent = msg;
        throw new Error(msg);
      }
      closeModal();
      toast(d.message || "已创建");
      if (_doRender) _doRender();
    }),
  });
  const mirSel = body.querySelector('[name="mirror_key"]');
  const scriptIn = body.querySelector('[name="sync_script"]');
  mirSel.addEventListener("change", () => {
    const m = _mirrors.find(x => x.key === mirSel.value);
    if (m && m.sync_script) scriptIn.value = m.sync_script;
  });
}

async function switchExecutor(task, executor) {
  if (!task.sync_script) {
    toast("该任务无同步脚本，请先在设置中绑定", "error");
    return false;
  }
  const label = executor === "ansible" ? "Ansible 分发" : "本机 wrapper";
  if (!(await confirmBox(
    `将「${task.mirror_name || task.title || task.sync_script}」切换为 ${label}？`,
    "切换执行方式",
    "切换"
  ))) return false;
  const r = await api("/crontab/task", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: task.id,
      minute: task.minute,
      hour: task.hour,
      dom: task.dom,
      month: task.month,
      dow: task.dow,
      mirror_key: task.mirror_key || "",
      sync_script: task.sync_script,
      executor,
    }),
  });
  const d = await r.json();
  if (!r.ok) { toast(d.error || "切换失败", "error"); return false; }
  toast(d.message || "已切换");
  return true;
}

function showCtx(e, task) {
  hideCtx();
  const menu = document.createElement("div");
  menu.className = "ctx-menu";
  const switchItem = task.executor === "ansible"
    ? `<button type="button" class="ctx-item" data-act="to-wrapper">${Icon("play")}切为本机</button>`
    : `<button type="button" class="ctx-item" data-act="to-ansible">${Icon("term")}切为 Ansible</button>`;
  menu.innerHTML = `
    <button type="button" class="ctx-item" data-act="settings">${Icon("set")}设置</button>
    ${switchItem}
    <button type="button" class="ctx-item danger" data-act="delete">${Icon("trash")}删除任务</button>`;
  document.body.appendChild(menu);
  const x = Math.min(e.clientX, window.innerWidth - 200);
  const y = Math.min(e.clientY, window.innerHeight - 140);
  menu.style.left = x + "px";
  menu.style.top = y + "px";
  _ctxMenu = menu;
  menu.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-act]"); if (!btn) return;
    ev.stopPropagation();
    if (btn.dataset.act === "settings") openSettings(task);
    else if (btn.dataset.act === "to-ansible") {
      hideCtx();
      try { if (await switchExecutor(task, "ansible") && _doRender) _doRender(); }
      catch (err) { toast("切换失败：" + err.message, "error"); }
    } else if (btn.dataset.act === "to-wrapper") {
      hideCtx();
      try { if (await switchExecutor(task, "wrapper") && _doRender) _doRender(); }
      catch (err) { toast("切换失败：" + err.message, "error"); }
    } else if (btn.dataset.act === "delete") {
      hideCtx();
      try {
        if (await deleteTask(task) && _doRender) _doRender();
      } catch (err) { toast("删除失败：" + err.message, "error"); }
    }
  });
}

register("crontab", function (c) {
  c._cleanup = () => {
    if (c._stop) c._stop();
    hideCtx();
    if (_onDocClick) document.removeEventListener("click", _onDocClick);
    if (_onDocScroll) document.removeEventListener("scroll", _onDocScroll, true);
    _onDocClick = _onDocScroll = _doRender = null;
  };

  c.innerHTML = `
    ${Toolbar('<span class="dim small">同步调度机 root crontab · 支持本机 wrapper / Ansible 分发 · 每 60 秒自动刷新</span>',
      `<button class="btn sm" id="cronRefresh">${Icon("refresh")}刷新</button>
       <button class="btn sm" id="cronRaw">${Icon("eye")}查看原始</button>
       <button class="btn sm" id="cronAdd">${Icon("plus")}新建任务</button>
       <button class="btn sm primary" id="cronEdit">${Icon("edit")}编辑 crontab</button>`)}
    <div id="cronBody"></div>`;

  async function render() {
    const box = $("cronBody"); if (!box) return;
    box.innerHTML = `<div class="card"><div class="card-body">${empty("加载中…", "", "#i-timer")}</div></div>`;
    try {
      const [d] = await Promise.all([
        api("/crontab").then(r => r.json()),
        loadMirrors(),
      ]);
      if (d.error) {
        box.innerHTML = `<div class="card"><div class="card-body">${empty("加载失败", d.error, "#i-warn")}</div></div>`;
        return;
      }
      _tasks = d.tasks || [];
      if (!_tasks.length) {
        box.innerHTML = `<div class="card"><div class="card-body">${empty("同步调度机上无 crontab 定时任务", "", "#i-timer")}</div></div>`;
        return;
      }
      const daily = _tasks.filter(t => t.dow === "*" && t.dom === "*" && t.month === "*");
      const weekly = _tasks.filter(t => t.dow !== "*" && t.dom === "*" && t.month === "*");
      const other = _tasks.filter(t =>
        !(t.dow === "*" && t.dom === "*" && t.month === "*") &&
        !(t.dow !== "*" && t.dom === "*" && t.month === "*")
      );
      box.innerHTML = `
        <div class="cron-groups">
          ${cronGroup("每日执行", daily, "ok")}
          ${cronGroup("每周执行", weekly, "brand")}
          ${cronGroup("其他周期", other, "warn")}
        </div>`;
      c._raw = d.raw || "";
    } catch (e) {
      box.innerHTML = `<div class="card"><div class="card-body">${empty("加载失败", e.message, "#i-warn")}</div></div>`;
    }
  }
  _doRender = render;

  $("cronRefresh").addEventListener("click", render);
  $("cronAdd").addEventListener("click", async () => {
    await loadMirrors();
    openCreate();
  });

  c.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act=settings]");
    if (!btn) return;
    const row = btn.closest(".cron-row"); if (!row) return;
    const task = _tasks.find(t => t.id === parseInt(row.dataset.id, 10));
    if (task) openSettings(task);
  });

  c.addEventListener("contextmenu", (e) => {
    const row = e.target.closest(".cron-row"); if (!row) return;
    e.preventDefault();
    const task = _tasks.find(t => t.id === parseInt(row.dataset.id, 10));
    if (task) showCtx(e, task);
  });

  c.addEventListener("dblclick", (e) => {
    if (e.target.closest("button")) return;
    const row = e.target.closest(".cron-row"); if (!row) return;
    const task = _tasks.find(t => t.id === parseInt(row.dataset.id, 10));
    if (task) openSettings(task);
  });

  _onDocClick = () => hideCtx();
  _onDocScroll = () => hideCtx();
  document.addEventListener("click", _onDocClick);
  document.addEventListener("scroll", _onDocScroll, true);

  $("cronRaw").addEventListener("click", async () => {
    let raw = c._raw;
    if (raw == null) {
      try { const d = await (await api("/crontab")).json(); raw = d.raw || ""; } catch (e) { toast("加载失败：" + e.message, "error"); return; }
    }
    raw = (raw || "").trim() || "(空)";
    openModal("原始 crontab", `<div class="small muted mb">同步调度机 · crontab -l</div><pre class="codebox" style="max-height:58vh">${esc(raw)}</pre>`, {
      wide: true,
      foot: (() => {
        const f = document.createElement("button");
        f.className = "btn"; f.innerHTML = Icon("copy") + "复制";
        f.addEventListener("click", () => navigator.clipboard.writeText(raw).then(() => toast("已复制")).catch(() => toast("复制失败", "error")));
        return f;
      })(),
    });
  });

  $("cronEdit").addEventListener("click", async () => {
    let raw = "";
    try {
      const d = await (await api("/crontab")).json();
      if (d.error) { toast("加载失败：" + d.error, "error"); return; }
      raw = d.raw || "";
    } catch (e) { toast("加载失败：" + e.message, "error"); return; }
    openModal("编辑 crontab", `
      <div class="callout warn mb">
        <b>注意</b>：保存后将整体覆盖同步调度机的 root crontab。每行须为标准 5 段 cron（分 时 日 月 周 命令）、注释、空行或环境变量。保存前会自动备份当前内容。
      </div>
      <textarea id="cronEditTa" class="textarea mono" style="min-height:44vh;white-space:pre" spellcheck="false">${esc(raw)}</textarea>
      <div class="row mt"><button class="btn sm" id="cronReset">${Icon("refresh")}恢复原内容</button><span id="cronSaveMsg" class="small muted"></span></div>`,
      { wide: true, foot: modalFoot("保存并覆盖", async () => {
          if (!(await confirmBox("确认覆盖同步调度机的 root crontab？", "覆盖确认", "覆盖"))) {
            const err = new Error("cancelled"); err.silent = true; throw err;
          }
          const ta = $("cronEditTa");
          const r = await putJSON("/crontab", { raw: ta.value });
          const d = await r.json();
          if (r.ok) { closeModal(); toast(d.message || "保存成功"); render(); }
          else { $("cronSaveMsg").textContent = d.error || "保存失败"; throw new Error(d.error || "保存失败"); }
        }) });
    $("cronReset").addEventListener("click", () => {
      $("cronEditTa").value = raw;
      $("cronSaveMsg").textContent = "已恢复到原始内容";
    });
  });

  render();
  c._stop = poller(() => { if (state.currentRoute === "crontab") render(); }, 60000);
});
