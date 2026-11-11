// 视图：系统设置 —— 分类：账号 / 告警阈值 / 通知策略 / 钉钉通道。
import { register } from "../core/router.js";
import { api, putJSON, postJSON } from "../core/api.js";
import { $, esc, toast, empty, skel } from "../core/dom.js";
import { Icon, Toolbar, Card, Field, Input, Switch, Badge, Table } from "../core/components.js";
import { relTime } from "../core/format.js";

const SWITCH_KEYS = [
  "sync_fail_alert",
  "notify_sync_success",
  "notify_dingtalk",
];

const SECTIONS = [
  { id: "account",   label: "账号",     icon: "user", hint: "登录身份" },
  { id: "threshold", label: "告警阈值", icon: "warn", hint: "磁盘 / CPU / 上游" },
  { id: "policy",    label: "通知策略", icon: "bell", hint: "事件推送与冷却" },
  { id: "dingtalk",  label: "钉钉通道", icon: "send", hint: "Webhook 与关键词" },
];

const SECTION_IDS = new Set(SECTIONS.map(s => s.id));

register("settings", function (c) {
  const boot = window._settingsSection;
  window._settingsSection = null;
  let active = SECTION_IDS.has(boot) ? boot : "account";

  c.innerHTML = `
    ${Toolbar(
      '<span class="dim small">按分类管理 · 凭证脱敏，留空保存保留原值</span>',
      `<button type="button" class="btn sm" id="stTest">${Icon("send")}测试钉钉</button>
       <button type="button" class="btn sm primary" id="stSave">${Icon("save")}保存设置</button>`
    )}
    <div class="st-layout">
      <nav class="st-nav" id="stNav" aria-label="设置分类">
        ${SECTIONS.map(s => `
          <button type="button" class="st-nav-item${s.id === active ? " active" : ""}" data-sec="${s.id}">
            <span class="st-nav-ico">${Icon(s.icon)}</span>
            <span class="st-nav-text">
              <span class="st-nav-label">${s.label}</span>
              <span class="st-nav-hint">${s.hint}</span>
            </span>
          </button>`).join("")}
      </nav>
      <form id="stForm" class="st-main">
        <section class="st-panel${active === "account" ? " active" : ""}" data-panel="account" id="stAccount">${Card({ body: skel(2, 60) })}</section>
        <section class="st-panel${active === "threshold" ? " active" : ""}" data-panel="threshold" id="stThreshold">${Card({ body: skel(3) })}</section>
        <section class="st-panel${active === "policy" ? " active" : ""}" data-panel="policy" id="stPolicy">${Card({ body: skel(3) })}</section>
        <section class="st-panel${active === "dingtalk" ? " active" : ""}" data-panel="dingtalk" id="stDingtalk">${Card({ body: skel(3) })}</section>
      </form>
    </div>`;

  const num = (k, v, label, hint, opts = {}) =>
    Field(label, Input(k, v, { type: "number", min: opts.min ?? 0, max: opts.max, step: opts.step }), hint);

  const toggle = (k, v, label, hint) => `
    <div class="st-toggle">
      <div class="st-toggle-text">
        <div class="st-toggle-label">${esc(label)}</div>
        ${hint ? `<div class="st-toggle-hint">${esc(hint)}</div>` : ""}
      </div>
      ${Switch(k, v === "1", label)}
    </div>`;

  function showSection(id) {
    if (!SECTION_IDS.has(id)) return;
    active = id;
    c.querySelectorAll(".st-nav-item").forEach(el => el.classList.toggle("active", el.dataset.sec === id));
    c.querySelectorAll(".st-panel").forEach(el => el.classList.toggle("active", el.dataset.panel === id));
  }

  function bindDingToggle() {
    const sw = c.querySelector('input[name="notify_dingtalk"]');
    const chip = $("stDingChip");
    if (!sw || !chip) return;
    const sync = () => {
      chip.textContent = sw.checked ? "已启用" : "未启用";
      chip.classList.toggle("on", sw.checked);
    };
    sw.addEventListener("change", sync);
    sync();
  }

  $("stNav").addEventListener("click", e => {
    const btn = e.target.closest("[data-sec]");
    if (btn) showSection(btn.dataset.sec);
  });

  async function loadAccount() {
    const box = $("stAccount"); if (!box) return;
    try {
      const d = await (await api("/users")).json();
      const cur = d.current || {};
      const logins = (d.recent_logins || []).slice(0, 5);
      box.innerHTML = `
        <div class="st-sec-head">
          <div>
            <h2 class="st-sec-title">账号</h2>
            <p class="st-sec-sub">当前登录身份与近期登录记录</p>
          </div>
        </div>
        <div class="st-card">
          <div class="st-identity">
            <div class="st-avatar">${esc((cur.username || "?").slice(0, 1).toUpperCase())}</div>
            <div>
              <div class="st-identity-name">${esc(cur.username || "-")}</div>
              <div class="st-identity-meta">
                ${Badge(cur.role_name || "未分配角色", "brand")}
                <span class="muted small">${esc(cur.login_via || "-")}</span>
              </div>
            </div>
          </div>
          <div class="st-divider"></div>
          <div class="st-block-label">最近登录</div>
          ${Table(
            [{ t: "用户" }, { t: "结果" }, { t: "时间" }, { t: "相对" }],
            logins.map(l => `<tr>
              <td class="mono">${esc(l.user)}</td>
              <td>${Badge(l.result, l.result === "成功" ? "ok" : "danger", { dot: true })}</td>
              <td class="num small">${esc(l.time || "-")}</td>
              <td class="small muted">${esc(relTime(l.time))}</td>
            </tr>`).join(""),
            { emptyHtml: empty("暂无登录记录", "", "#i-user"), dense: true }
          )}
        </div>`;
    } catch (e) {
      box.innerHTML = Card({ body: empty("账号信息加载失败", e.message, "#i-warn") });
    }
  }

  async function load() {
    try {
      const s = await (await api("/settings")).json();

      $("stThreshold").innerHTML = `
        <div class="st-sec-head">
          <div>
            <h2 class="st-sec-title">告警阈值</h2>
            <p class="st-sec-sub">超阈值写入告警，并参与主机监控 / 上游源标记</p>
          </div>
        </div>
        <div class="st-card">
          <div class="st-block-label">资源与主机</div>
          <div class="st-grid">
            ${num("disk_alert_threshold", s.disk_alert_threshold, "磁盘使用率 (%)", "存储告警触发线", { max: 100 })}
            ${num("cpu_alert_threshold", s.cpu_alert_threshold, "CPU 使用率 (%)", "主机监控对照", { max: 100 })}
            ${num("mem_alert_threshold", s.mem_alert_threshold, "内存使用率 (%)", "主机监控对照", { max: 100 })}
          </div>
          <div class="st-divider"></div>
          <div class="st-block-label">上游延迟</div>
          <div class="st-grid">
            ${num("upstream_latency_warn", s.upstream_latency_warn, "「慢」阈值 (ms)", "上游源页黄色标记")}
            ${num("upstream_latency_crit", s.upstream_latency_crit, "「差」阈值 (ms)", "上游源页红色标记")}
          </div>
        </div>`;

      $("stPolicy").innerHTML = `
        <div class="st-sec-head">
          <div>
            <h2 class="st-sec-title">通知策略</h2>
            <p class="st-sec-sub">控制何时推送，以及冷却去重</p>
          </div>
        </div>
        <div class="st-card">
          <div class="st-block-label">推送事件</div>
          ${toggle("sync_fail_alert", s.sync_fail_alert, "同步失败告警", "镜像同步失败时向已启用通道推送")}
          ${toggle("notify_sync_success", s.notify_sync_success, "同步成功通知", "成功完成后推送（受冷却限制，避免刷屏）")}
          <div class="st-divider"></div>
          <div class="st-block-label">频率</div>
          <div class="st-grid st-grid-1 st-grid-pad">
            ${num("alert_cooldown", s.alert_cooldown, "冷却时间 (秒)", "同一标题在冷却期内不重复推送")}
          </div>
        </div>`;

      $("stDingtalk").innerHTML = `
        <div class="st-sec-head">
          <div>
            <h2 class="st-sec-title">钉钉通道</h2>
            <p class="st-sec-sub">自定义机器人 Webhook · 关键词 / 加签按群安全设置填写</p>
          </div>
          <span class="st-chip ${s.notify_dingtalk === "1" ? "on" : ""}" id="stDingChip">${s.notify_dingtalk === "1" ? "已启用" : "未启用"}</span>
        </div>
        <div class="st-card">
          ${toggle("notify_dingtalk", s.notify_dingtalk, "启用钉钉通知", "关闭后同步事件不再推送到钉钉")}
          <div class="st-divider"></div>
          <div class="st-block-label">连接参数</div>
          ${Field("Webhook 地址", Input("notify_dingtalk_webhook", s.notify_dingtalk_webhook, {
            placeholder: "https://oapi.dingtalk.com/robot/send?access_token=…",
            mono: true,
          }))}
          <div class="st-grid">
            ${Field("自定义关键词", Input("notify_dingtalk_keyword", s.notify_dingtalk_keyword, { placeholder: "如：你好" }), "安全方式为「自定义关键词」时必填，自动写入正文")}
            ${Field("@ 手机号", Input("notify_dingtalk_at", s.notify_dingtalk_at, { placeholder: "多个用逗号分隔" }), "可选，推送时 @ 指定成员")}
          </div>
          ${Field(
            "加签密钥",
            Input("notify_dingtalk_secret", s.notify_dingtalk_secret, { type: "password", placeholder: s.notify_dingtalk_secret === "***" ? "已配置，留空保留" : "SEC…" }),
            s.notify_dingtalk_secret === "***" ? "已配置（脱敏），留空保存保留原值" : "安全设置为加签时填写"
          )}
          <div id="stTestMsg" class="st-test-msg" hidden></div>
        </div>`;
      bindDingToggle();
    } catch (e) {
      ["stThreshold", "stPolicy", "stDingtalk"].forEach(id => {
        const el = $(id);
        if (el) el.innerHTML = Card({ body: empty("设置加载失败", e.message, "#i-warn") });
      });
    }
  }

  $("stForm").addEventListener("submit", e => e.preventDefault());

  $("stSave").addEventListener("click", async () => {
    const btn = $("stSave");
    btn.disabled = true;
    btn.innerHTML = '<span class="spin"></span>保存中';
    try {
      const form = $("stForm");
      const payload = { notify_email: "0" };
      form.querySelectorAll("input[name]").forEach(el => {
        payload[el.name] = SWITCH_KEYS.includes(el.name) ? (el.checked ? "1" : "0") : el.value;
      });
      const r = await putJSON("/settings", payload);
      const d = await r.json();
      if (r.ok) {
        toast(d.message || "设置已更新");
        await load();
        showSection(active);
      } else toast(d.error || "保存失败", "error");
    } catch (e) {
      if (e.message !== "CSRF") toast(e.message || "保存失败", "error");
    } finally {
      btn.disabled = false;
      btn.innerHTML = Icon("save") + "保存设置";
    }
  });

  $("stTest").addEventListener("click", async () => {
    const btn = $("stTest");
    btn.disabled = true;
    btn.innerHTML = '<span class="spin"></span>发送中';
    showSection("dingtalk");
    const msg = $("stTestMsg");
    if (msg) { msg.hidden = false; msg.innerHTML = ""; }
    try {
      const r = await postJSON("/settings/test", { kind: "both" });
      const d = await r.json();
      const lines = d.results || [];
      const ok = r.ok && lines.length && lines.every(x => x.includes("成功"));
      toast(d.message || (r.ok ? "测试完成" : "测试失败"), ok ? "success" : r.ok ? "warn" : "error");
      if (msg) {
        msg.innerHTML = `<div class="st-test-box ${ok ? "ok" : ""}">${(lines.length ? lines : [d.message || "无结果"]).map(x => esc(x)).join("<br>")}</div>`;
      }
    } catch (e) {
      if (e.message !== "CSRF") toast(e.message || "测试失败", "error");
    } finally {
      btn.disabled = false;
      btn.innerHTML = Icon("send") + "测试钉钉";
    }
  });

  loadAccount();
  load().then(() => showSection(active));
});
