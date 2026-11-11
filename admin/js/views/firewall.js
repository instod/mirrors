// 视图：防火墙端口放行（firewalld）。
import { register } from "../core/router.js";
import { api, postJSON } from "../core/api.js";
import { $, esc, empty, skel, toast, confirmBox } from "../core/dom.js";
import { Icon, Badge, Toolbar, Table, Callout, Field, Input, Select } from "../core/components.js";

register("firewall", function (c) {
  c.innerHTML = `
    ${Toolbar(
      '<span class="dim small">firewalld 端口放行 · 受保护端口不可删</span>',
      `<button class="btn sm primary" id="fwRefresh">${Icon("refresh")}刷新</button>`
    )}
    <div id="fwTip" class="mb"></div>
    <div class="card mb">
      <div class="card-head"><h3>放行端口</h3><span class="sub">永久 + reload</span></div>
      <div class="card-body">
        <div class="row" style="gap:10px;flex-wrap:wrap;align-items:flex-end">
          ${Field("端口", Input("fwPort", "", { placeholder: "8080 或 8000-8010" }))}
          ${Field("协议", Select("fwProto", [{ v: "tcp", t: "tcp" }, { v: "udp", t: "udp" }], "tcp"))}
          <button class="btn primary" id="fwAdd">${Icon("plus")}添加</button>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h3>当前规则</h3><span class="sub" id="fwZone">zone</span></div>
      <div class="card-body tight" id="fwList">${skel(4)}</div>
    </div>`;

  $("fwRefresh").onclick = load;
  $("fwAdd").onclick = addPort;

  async function load() {
    try {
      const d = await (await api("/firewall")).json();
      if (d.enabled === false) {
        $("fwTip").innerHTML = Callout(d.note || "firewalld 未启用", { tone: "warn", title: "防火墙不可用" });
        $("fwList").innerHTML = empty("无规则", d.note || "", "#i-shield");
        $("fwZone").textContent = d.zone || "-";
        return;
      }
      if (d.error) throw new Error(d.error);
      $("fwTip").innerHTML = Callout(
        `区 ${d.zone || "-"} · 服务 ${(d.services || []).join(" ") || "—"} · 网卡 ${(d.interfaces || []).join(" ") || "—"}`,
        { tone: "ok", title: "firewalld" }
      );
      $("fwZone").textContent = `zone=${d.zone || "-"}`;
      const ports = d.ports || [];
      $("fwList").innerHTML = ports.length
        ? Table(
            [{ t: "端口" }, { t: "协议" }, { t: "保护" }, { t: "操作" }],
            ports.map(p => `<tr>
              <td class="mono">${esc(p.port)}</td>
              <td>${esc(p.proto || "tcp")}</td>
              <td>${p.protected ? Badge("保护", "warn") : Badge("可改", "muted")}</td>
              <td>${p.protected
                ? `<span class="dim small">—</span>`
                : `<button class="btn sm danger" data-rm-port="${esc(p.port)}" data-rm-proto="${esc(p.proto || "tcp")}">${Icon("trash")}移除</button>`}</td>
            </tr>`).join(""),
            { dense: true }
          )
        : empty("无放行端口", "可在上方添加", "#i-shield");
      c.querySelectorAll("[data-rm-port]").forEach(btn => {
        btn.onclick = () => removePort(btn.dataset.rmPort, btn.dataset.rmProto);
      });
    } catch (e) {
      $("fwTip").innerHTML = Callout(e.message || String(e), { tone: "danger", title: "读取失败" });
    }
  }

  async function addPort() {
    const port = (c.querySelector('[name="fwPort"]')?.value || "").trim();
    const proto = (c.querySelector('[name="fwProto"]')?.value || "tcp").trim();
    if (!port) { toast("请填写端口", "error"); return; }
    try {
      const r = await postJSON("/firewall", { port, proto });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "添加失败");
      toast(d.message || "已添加", "ok");
      load();
    } catch (e) {
      toast(e.message || String(e), "error");
    }
  }

  async function removePort(port, proto) {
    const ok = await confirmBox(`确定移除 ${port}/${proto}？`);
    if (!ok) return;
    try {
      const r = await api("/firewall", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ port, proto }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "移除失败");
      toast(d.message || "已移除", "ok");
      load();
    } catch (e) {
      toast(e.message || String(e), "error");
    }
  }

  load();
});
