// 视图：Ansible 调度 —— 8.74 控制机 inventory / playbook / 执行历史 + 日志。
import { register } from "../core/router.js";
import { api, postJSON } from "../core/api.js";
import { $, esc, toast, confirmBox, empty, skel, openModal, closeModal, modalFoot } from "../core/dom.js";
import { Icon, Badge, Table, Toolbar, Select, Field, Input } from "../core/components.js";

const LIMITS = [
  { v: "auto", t: "auto — 按体积自动选 1 台 bnode（推荐）" },
  { v: "bnode1", t: "bnode1" },
  { v: "bnode5", t: "bnode5" },
  { v: "bnode10", t: "bnode10" },
  { v: "bnode15", t: "bnode15" },
  { v: "bnode20", t: "bnode20" },
  { v: "bnode28", t: "bnode28" },
  { v: "all", t: "all — 全部主机（仅运维探测）" },
  { v: "sync_nodes", t: "sync_nodes — 同步节点" },
  { v: "bnode", t: "bnode — 整组30台（禁止用于 run_sync）" },
  { v: "web", t: "web — Web 节点" },
  { v: "rhel9", t: "rhel9 — RHEL 9" },
  { v: "rhel8", t: "rhel8 — RHEL 8" },
];
const cssId = (s) => (s || "").replace(/[^a-zA-Z0-9]/g, "-");

register("ansible", function (c) {
  c._cleanup = () => {
    if (c._ansTimer) { clearInterval(c._ansTimer); c._ansTimer = null; }
    if (c._ansPoll) { clearInterval(c._ansPoll); c._ansPoll = null; }
  };
  let _pbList = [];

  c.innerHTML = `
    ${Toolbar('<span class="dim small">即时执行 playbook · run_sync.yml 可「加入定时」走 ai_dispatch</span>',
      `<button class="btn sm" id="ansRefresh">${Icon("refresh")}刷新</button><button class="btn sm" id="ansLogBtn">${Icon("file")}查看日志</button>`)}
    <div class="callout mb">
      <b>与定时任务的关系</b>：日常镜像同步仍由 crontab 触发。
      选 <b>本机</b> 走 <code>wrapper.py</code>；选 <b>Ansible</b> 走 <code>ai_dispatch.py</code> → <strong>自动选 1 台</strong> bnode（按体积分档），禁止 <code>-l bnode</code> 打满 30 台。
      运维类 playbook（如 ping）可整组执行；<code>run_sync.yml</code> 选 auto 即可。
    </div>    <div class="stats-grid mb" id="ansSummary" style="grid-template-columns:repeat(auto-fit,minmax(160px,1fr))">${'<div class="skel" style="height:66px"></div>'.repeat(2)}</div>
    <div id="ansInv"><div class="card"><div class="card-body">${skel(3)}</div></div></div>
    <div class="mt" id="ansPb"><div class="card"><div class="card-body">${skel(3)}</div></div></div>
    <div class="mt" id="ansRuns"><div class="card"><div class="card-body">${skel(3)}</div></div></div>`;

  // ---------- Inventory ----------
  async function loadInventory() {
    const box = $("ansInv"); if (!box) return;
    try {
      const d = await (await api("/ansible/inventory")).json();
      if (d.error) { box.innerHTML = `<div class="card"><div class="card-body">${empty("加载失败", d.error, "#i-warn")}</div></div>`; return; }
      $("ansSummary").innerHTML =
        `<div class="stat"><div class="stat-ico brand">${Icon("globe")}</div><div><div class="stat-val">${d.hosts_total || 0}</div><div class="stat-label">主机总数</div></div></div>
         <div class="stat"><div class="stat-ico ok">${Icon("box")}</div><div><div class="stat-val">${(d.groups || []).length}</div><div class="stat-label">分组数</div></div></div>`;
      if (!d.groups || !d.groups.length) { box.innerHTML = `<div class="card"><div class="card-body">${empty("inventory 为空", "", "#i-box")}</div></div>`; return; }
      box.innerHTML = `<div class="card"><div class="card-head"><h3>Inventory 主机清单</h3></div><div class="card-body">
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px">
          ${(d.groups || []).map(invCard).join("")}
        </div></div></div>`;
    } catch (e) { box.innerHTML = `<div class="card"><div class="card-body">${empty("加载失败", e.message, "#i-warn")}</div></div>`; }
  }

  function invCard(g) {
    const cnt = (g.hosts || []).length;
    const chips = (g.hosts || []).map(h => `<span class="kbd">${esc(h)}</span>`).join(" ");
    const children = (g.children || []).length ? `<div class="small muted" style="margin-top:7px">含子组: ${(g.children || []).map(esc).join(", ")}</div>` : "";
    return `<div class="card" style="padding:13px 15px">
      <div class="spread" style="margin-bottom:8px">
        <strong class="mono" style="font-size:13px">[${esc(g.name)}]</strong>
        ${Badge(cnt + " 台", "brand")}
      </div>
      <div class="row wrap" style="gap:5px">${chips || '<span class="small muted">（无直接主机）</span>'}</div>
      ${children}
    </div>`;
  }

  // ---------- Playbook ----------
  async function loadPlaybooks() {
    const box = $("ansPb"); if (!box) return;
    try {
      const d = await (await api("/ansible/playbooks")).json();
      if (d.error) { box.innerHTML = `<div class="card"><div class="card-body">${empty("加载失败", d.error, "#i-warn")}</div></div>`; return; }
      _pbList = d.playbooks || [];
      if (!_pbList.length) { box.innerHTML = `<div class="card"><div class="card-body">${empty("playbooks/ 目录下无 playbook（.yml）", "", "#i-term")}</div></div>`; return; }
      box.innerHTML = `<div class="card"><div class="card-head"><h3>Playbook</h3><span class="sub">${_pbList.length} 个</span></div><div class="card-body">
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:10px">
          ${_pbList.map(pbCard).join("")}
        </div></div></div>`;
      _pbList.forEach(pb => {
        const id = cssId(pb.name);
        const b1 = $("pbRun-" + id); if (b1) b1.addEventListener("click", async () => {
          await loadMirrors();
          openRunModal(pb.name);
        });
        const bSched = $("pbSched-" + id); if (bSched) bSched.addEventListener("click", () => openScheduleModal());
        const b2 = $("pbToggle-" + id); if (b2) b2.addEventListener("click", () => {
          const body = $("pbBody-" + id);
          const open = body.style.display !== "none";
          body.style.display = open ? "none" : "block";
          b2.innerHTML = open ? Icon("eye") + "查看" : Icon("chev-d") + "收起";
        });
      });
    } catch (e) { box.innerHTML = `<div class="card"><div class="card-body">${empty("加载失败", e.message, "#i-warn")}</div></div>`; }
  }

  function pbCard(pb) {
    const id = cssId(pb.name);
    const isSync = /^run_sync\.ya?ml$/i.test(pb.name);
    return `<div class="card" style="padding:13px 15px;display:flex;flex-direction:column;gap:8px">
      <strong class="mono" style="font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(pb.name)}">${esc(pb.name)}</strong>
      <div class="row wrap" style="gap:6px">
        ${Badge("目标: " + (pb.hosts || "-"), "info")}
        ${Badge((pb.task_count || 0) + " tasks")}
        ${isSync ? Badge("可定时", "brand") : ""}
      </div>
      <div class="row wrap" style="gap:6px">
        <button class="btn sm primary" id="pbRun-${id}">${Icon("play")}执行</button>
        ${isSync ? `<button class="btn sm" id="pbSched-${id}">${Icon("timer")}加入定时</button>` : ""}
        <button class="btn sm" id="pbToggle-${id}">${Icon("eye")}查看</button>
      </div>
      <div id="pbBody-${id}" style="display:none"><pre class="codebox" style="max-height:40vh">${esc(pb.content || "")}</pre></div>
    </div>`;
  }

  // ---------- 执行历史 ----------
  async function loadRuns() {
    const box = $("ansRuns"); if (!box) return;
    try {
      const d = await (await api("/ansible/runs")).json();
      if (d.error) { box.innerHTML = `<div class="card"><div class="card-body">${empty("加载失败", d.error, "#i-warn")}</div></div>`; return; }
      if (!d.runs || !d.runs.length) { box.innerHTML = `<div class="card"><div class="card-body">${empty("暂无执行历史", "", "#i-term")}</div></div>`; return; }
      const rows = d.runs.map(runRow).join("");
      box.innerHTML = `<div class="card"><div class="card-head"><h3>执行历史</h3><span class="sub">${d.count} 条</span></div><div class="card-body tight">
        ${Table([{ t: "开始时间" }, { t: "Playbook" }, { t: "目标" }, { t: "状态" }, { t: "完成时间" }, { t: "输出", w: "80px" }], rows)}
      </div></div>`;
      d.runs.forEach(rn => {
        const id = cssId(rn.run_id);
        const tg = $("runTg-" + id);
        if (tg) tg.addEventListener("click", async () => {
          const body = $("runBody-" + id);
          if (body.style.display !== "none") { body.style.display = "none"; tg.textContent = "查看"; return; }
          body.style.display = "block"; tg.textContent = "收起";
          body.innerHTML = `<div class="codebox" style="margin:0 .8rem .8rem">加载中…</div>`;
          try {
            const dd = await (await api("/ansible/runs/" + encodeURIComponent(rn.run_id))).json();
            body.innerHTML = `<div class="codebox" style="margin:0 .8rem .8rem;max-height:45vh">${esc(dd.output || "(无输出)")}</div>`;
          } catch {}
        });
      });
      // 有运行中任务则 5s 轮询
      const hasRunning = d.runs.some(x => x.status === "running");
      if (hasRunning) { if (!c._ansPoll) c._ansPoll = setInterval(loadRuns, 5000); }
      else if (c._ansPoll) { clearInterval(c._ansPoll); c._ansPoll = null; }
    } catch (e) { box.innerHTML = `<div class="card"><div class="card-body">${empty("加载失败", e.message, "#i-warn")}</div></div>`; }
  }

  function runRow(rn) {
    const tone = rn.status === "ok" ? "ok" : rn.status === "running" ? "info" : rn.status === "timeout" ? "warn" : "danger";
    const lbl = rn.status === "ok" ? "成功" : rn.status === "running" ? "运行中" : rn.status === "timeout" ? "超时" : "失败";
    const id = cssId(rn.run_id);
    return `<tr>
      <td class="num" style="white-space:nowrap">${esc(rn.started_at || "")}</td>
      <td class="mono">${esc(rn.playbook || "")}</td>
      <td>${Badge(rn.lim || "-", "info")}</td>
      <td>${Badge(lbl, tone, { dot: true, pulse: rn.status === "running" })}</td>
      <td class="num small">${esc(rn.finished_at || "-")}</td>
      <td><button class="btn sm" id="runTg-${id}">查看</button></td>
    </tr>
    <tr><td colspan="6" style="padding:0"><div id="runBody-${id}" style="display:none"></div></td></tr>`;
  }

  // ---------- 执行面板（模态） ----------
  let _mirrors = [];
  async function loadMirrors() {
    try {
      const r = await api("/mirrors");
      if (!r.ok) return;
      const list = await r.json();
      _mirrors = Array.isArray(list) ? list : [];
    } catch { _mirrors = []; }
  }

  function openRunModal(presetPb) {
    const list = (_pbList.length ? _pbList : [{ name: presetPb }]);
    const isSync = /^run_sync\.ya?ml$/i.test(presetPb);
    const mirOpts = [{ v: "", t: "（手动填写脚本）" }].concat(
      _mirrors.filter(m => m.sync_script).map(m => ({
        v: m.key,
        t: `${m.name || m.key} (${m.key})`,
      }))
    );
    openModal("执行 Playbook", `
      <div class="small mb" style="padding:9px 12px;background:var(--warn-bg);border:1px solid var(--warn-bd);border-radius:8px;color:var(--text-dim);line-height:1.6">
        <b>注意</b>：将以 <b>root</b> 在所选主机组执行 playbook，可能影响批量节点。后台异步跑完后写入执行历史。
      </div>
      ${Field("Playbook", Select("ansRunPb", list.map(p => ({ v: p.name, t: p.name })), presetPb, { id: "ansRunPb" }))}
      ${Field("目标主机 (-l)", Select("ansRunLim", LIMITS, isSync ? "auto" : "all", { id: "ansRunLim" }),
        isSync ? "run_sync 默认 auto：ai_dispatch 按体积选 1 台；禁止整组 bnode" : "运维探测可选整组")}
      <div id="ansSyncFields" style="${isSync ? "" : "display:none"}">
        ${Field("镜像源", Select("ansRunMir", mirOpts, "", { id: "ansRunMir" }))}
        ${Field("同步脚本 (-e sync_script)", Input("ansRunScript", "", { id: "ansRunScript", mono: true, placeholder: "/hpcfs/fpublic/.../xxx.py" }), "run_sync.yml 必填")}
      </div>`,
      { foot: modalFoot("开始执行", async () => {
          const pb = $("ansRunPb").value, lim = $("ansRunLim").value;
          const syncScript = ($("ansRunScript") && $("ansRunScript").value || "").trim();
          if (/^run_sync\.ya?ml$/i.test(pb) && !syncScript) {
            toast("run_sync.yml 需要填写同步脚本", "error");
            const e = new Error("need sync_script"); e.silent = true; throw e;
          }
          const isSyncPb = /^run_sync\.ya?ml$/i.test(pb);
          if (!(await confirmBox(
            isSyncPb
              ? `确认同步 ${pb}？（limit=${lim}；auto/整组将自动改走 ai_dispatch 单节点）`
              : `确认在「${lim}」上以 root 执行 ${pb}？`,
            "执行确认", "执行"))) { const e = new Error("cancelled"); e.silent = true; throw e; }
          const body = { playbook: pb, limit: lim };
          if (syncScript) body.sync_script = syncScript;
          const mir = ($("ansRunMir") && $("ansRunMir").value || "").trim();
          if (mir) body.key = mir;
          const r = await postJSON("/ansible/run", body);
          const d = await r.json();
          if (r.ok) {
            closeModal();
            toast(d.routed === "ai_dispatch"
              ? "已交 ai_dispatch 单节点：" + (d.message || d.limit || "auto")
              : "已开始执行: " + (d.run_id || ""));
            loadRuns();
          }
          else { $("modalBody").insertAdjacentHTML("beforeend", `<div class="login-err show" style="display:block;margin-top:10px">${esc(d.error || "提交失败")}</div>`); throw new Error(d.error || "提交失败"); }
        }) });
    const pbSel = $("ansRunPb");
    const syncBox = $("ansSyncFields");
    const mirSel = $("ansRunMir");
    const scriptIn = $("ansRunScript");
    if (pbSel && syncBox) {
      const syncVis = () => {
        const on = /^run_sync\.ya?ml$/i.test(pbSel.value);
        syncBox.style.display = on ? "" : "none";
      };
      pbSel.addEventListener("change", syncVis);
    }
    if (mirSel && scriptIn) {
      mirSel.addEventListener("change", () => {
        const m = _mirrors.find(x => x.key === mirSel.value);
        if (m && m.sync_script) scriptIn.value = m.sync_script;
      });
    }
  }

  async function openScheduleModal() {
    await loadMirrors();
    const mirOpts = [{ v: "", t: "请选择镜像…" }].concat(
      _mirrors.filter(m => m.sync_script).map(m => ({
        v: m.key,
        t: `${m.name || m.key} (${m.key})`,
      }))
    );
    openModal("加入定时（Ansible 分发）", `
      <div class="callout mb">将写入调度机 crontab，命令为 <code>ai_dispatch.py &lt;同步脚本&gt;</code>，由 Ansible 分发到 bnode 池。默认定时每天 02:00。</div>
      <div class="form-grid">
        ${Field("分", Input("minute", "0", { mono: true, required: true }))}
        ${Field("时", Input("hour", "2", { mono: true, required: true }))}
        ${Field("日", Input("dom", "*", { mono: true, required: true }))}
        ${Field("月", Input("month", "*", { mono: true, required: true }))}
        <div class="span2">${Field("周", Input("dow", "*", { mono: true, required: true }))}</div>
        <div class="span2">${Field("镜像源", Select("mirror_key", mirOpts, ""))}</div>
        <div class="span2">${Field("同步脚本", Input("sync_script", "", { mono: true, placeholder: "/hpcfs/fpublic/.../xxx.py" }))}</div>
      </div>
      <div class="mt"><span id="ansSchedMsg" class="small muted"></span></div>`, {
      wide: true,
      foot: modalFoot("写入定时", async () => {
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
          executor: "ansible",
        };
        if (!payload.sync_script && !payload.mirror_key) {
          $("ansSchedMsg").textContent = "请选择镜像或填写同步脚本";
          throw new Error("missing");
        }
        const r = await api("/crontab/task", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const d = await r.json();
        if (!r.ok) {
          $("ansSchedMsg").textContent = d.error || "创建失败";
          throw new Error(d.error || "创建失败");
        }
        closeModal();
        toast(d.message || "已加入定时");
      }),
    });
    const mirSel = $("modalBody").querySelector('[name="mirror_key"]');
    const scriptIn = $("modalBody").querySelector('[name="sync_script"]');
    if (mirSel && scriptIn) {
      mirSel.addEventListener("change", () => {
        const m = _mirrors.find(x => x.key === mirSel.value);
        if (m && m.sync_script) scriptIn.value = m.sync_script;
      });
    }
  }

  // ---------- 日志（模态） ----------
  $("ansLogBtn").addEventListener("click", async () => {
    let log = "";
    try {
      const d = await (await api("/ansible/log?lines=200")).json();
      log = (d.log || "").trim() || "(空)";
    } catch (e) { toast("加载失败：" + e.message, "error"); return; }
    openModal("Ansible 日志", `<div class="small muted mb">logs/ansible.log · 末 200 行</div><pre class="codebox" style="max-height:58vh">${esc(log)}</pre>`, {
      wide: true,
      foot: (() => {
        const f = document.createElement("button");
        f.className = "btn"; f.innerHTML = Icon("copy") + "复制";
        f.addEventListener("click", () => navigator.clipboard.writeText(log).then(() => toast("已复制")).catch(() => toast("复制失败", "error")));
        return f;
      })(),
    });
  });

  $("ansRefresh").addEventListener("click", async () => { await loadInventory(); await loadPlaybooks(); await loadRuns(); });

  loadInventory(); loadPlaybooks(); loadRuns();
  c._ansTimer = setInterval(async () => { try { await loadInventory(); await loadPlaybooks(); } catch {} }, 60000);
});
