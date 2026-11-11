// 视图：文件浏览 —— 镜像下拉 + 面包屑 + 搜索 + 复制公开访问 URL。
import { register } from "../core/router.js";
import { api } from "../core/api.js";
import { $, esc, toast, empty } from "../core/dom.js";
import { Icon, Pager, Table } from "../core/components.js";

register("browse", async function (c) {
  let curKey = "", curPath = "", curPage = 1;
  const mirrorUrl = {}; // key -> 相对路径 url

  c.innerHTML = `
    <div class="card mb"><div class="card-body">
      <div class="row wrap">
        <select id="fbKey" class="select" style="min-width:230px"><option value="">选择镜像…</option></select>
        <div class="search-box" style="flex:1;min-width:180px;max-width:none">${Icon("search")}<input id="fbQ" class="input" style="width:100%" placeholder="按名称搜索当前目录"></div>
        <button class="btn sm primary" id="fbSearch">${Icon("search")}搜索</button>
        <button class="btn sm" id="fbCopy" title="复制当前目录的公开访问 URL" disabled>${Icon("copy")}复制 URL</button>
      </div>
      <div id="fbCrumb" class="small mt" style="display:flex;align-items:center;flex-wrap:wrap;gap:3px"></div>
      <div id="fbUrl" class="mono small muted mt" style="display:none;word-break:break-all"></div>
    </div></div>
    <div class="card"><div class="card-body tight" id="fbWrap">
      ${Table([{ t: "名称" }, { t: "类型", w: "80px" }, { t: "大小", r: 1, w: "110px" }, { t: "修改时间", r: 1, w: "160px" }], "", { emptyHtml: empty("请先选择一个镜像", "在上方下拉框中选择要浏览的镜像", "#i-folder") })}
    </div></div>
    <div id="fbPage" class="mt row" style="justify-content:center"></div>`;

  const tbody = c.querySelector("table.tbl tbody"); tbody.id = "fbTable";
  const folderIc = '<svg fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px;color:var(--warn);vertical-align:-2px;margin-right:5px"><use href="#i-folder"/></svg>';
  const fileIc = '<svg fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px;color:var(--text-mute);vertical-align:-2px;margin-right:5px"><use href="#i-file"/></svg>';

  function publicUrl() {
    if (!curKey) return "";
    let base = (mirrorUrl[curKey] || (curKey + "/")).replace(/^\//, "");
    if (!base.endsWith("/")) base += "/";
    const sub = curPath ? curPath.replace(/^\/+|\/+$/g, "") + "/" : "";
    return location.origin + "/" + base + sub;
  }

  function updateUrlBar() {
    const url = publicUrl();
    const el = $("fbUrl");
    const btn = $("fbCopy");
    if (!url) {
      el.style.display = "none"; el.textContent = "";
      btn.disabled = true;
      return;
    }
    el.style.display = "";
    el.textContent = "公开访问：" + url;
    btn.disabled = false;
  }

  async function loadKeys() {
    try {
      const d = await (await api("/mirrors")).json();
      const items = Array.isArray(d) ? d : (d.items || []);
      items.forEach(m => { mirrorUrl[m.key] = m.url || (m.key + "/"); });
      $("fbKey").innerHTML = `<option value="">选择镜像…</option>` + items.map(m => `<option value="${esc(m.key)}">${esc(m.name || m.key)} (${esc(m.key)})</option>`).join("");
    } catch (e) { toast("加载镜像列表失败：" + e.message, "error"); }
  }

  function setRows(html) { tbody.innerHTML = html; }

  async function loadDir() {
    if (!curKey) {
      setRows(`<tr><td colspan="4">${empty("请先选择一个镜像", "", "#i-folder")}</td></tr>`);
      $("fbCrumb").innerHTML = ""; $("fbPage").innerHTML = "";
      updateUrlBar();
      return;
    }
    setRows(`<tr><td colspan="4">${empty("加载中…", "", "#i-folder")}</td></tr>`);
    try {
      const p = new URLSearchParams();
      if (curPath) p.set("path", curPath);
      const q = $("fbQ").value.trim(); if (q) p.set("q", q);
      p.set("page", curPage);
      const d = await (await api("/files/" + encodeURIComponent(curKey) + "?" + p.toString())).json();
      if (d.error) { setRows(`<tr><td colspan="4">${empty("加载失败", d.error, "#i-warn")}</td></tr>`); return; }
      const segs = d.path ? d.path.split("/") : [];
      let crumb = `<a class="fb-crumb" data-p="" style="cursor:pointer;font-weight:600">${esc(curKey)}</a>`;
      let acc = "";
      segs.forEach(s => { acc = acc ? acc + "/" + s : s; crumb += ` <span class="muted">/</span> <a class="fb-crumb" data-p="${esc(acc)}" style="cursor:pointer">${esc(s)}</a>`; });
      $("fbCrumb").innerHTML = crumb;
      updateUrlBar();
      let rows = "";
      if (d.path) rows += `<tr><td colspan="4"><a class="fb-up" style="cursor:pointer;font-weight:600;color:var(--brand-text)">.. 返回上级</a></td></tr>`;
      rows += (d.entries || []).map(e => `<tr>
        <td>${e.is_dir
          ? `<a class="fb-dir" data-p="${esc(e.path)}" style="cursor:pointer;font-weight:600">${folderIc}${esc(e.name)}</a>`
          : `<span style="color:var(--text-dim)">${fileIc}${esc(e.name)}</span>`}</td>
        <td class="small dim">${e.is_dir ? "目录" : "文件"}</td>
        <td class="num">${e.is_dir ? "-" : esc(e.size_str || "-")}</td>
        <td class="num small">${esc(e.mtime || "-")}</td>
      </tr>`).join("");
      setRows(rows || `<tr><td colspan="4">${empty("空目录", "", "#i-folder")}</td></tr>`);
      $("fbPage").innerHTML = (d.total_pages > 1)
        ? `<span class="small muted" style="margin-right:4px">共 ${d.total} 项</span><div style="flex:1">${Pager(d.page, d.total_pages, d.total, 100)}</div>`
        : "";
    } catch (e) { setRows(`<tr><td colspan="4">${empty("加载失败", e.message, "#i-warn")}</td></tr>`); }
  }

  $("fbKey").addEventListener("change", () => { curKey = $("fbKey").value; curPath = ""; curPage = 1; loadDir(); });
  $("fbSearch").addEventListener("click", () => { curPage = 1; loadDir(); });
  $("fbQ").addEventListener("keydown", e => { if (e.key === "Enter") { curPage = 1; loadDir(); } });
  $("fbCopy").addEventListener("click", () => {
    const url = publicUrl();
    if (!url) return;
    navigator.clipboard.writeText(url).then(() => toast("已复制公开 URL")).catch(() => toast("复制失败", "error"));
  });
  tbody.addEventListener("click", e => {
    const d = e.target.closest(".fb-dir"); if (d) { curPath = d.dataset.p; curPage = 1; loadDir(); return; }
    const u = e.target.closest(".fb-up"); if (u) { curPath = curPath.split("/").slice(0, -1).join("/"); curPage = 1; loadDir(); }
  });
  $("fbCrumb").addEventListener("click", e => { const d = e.target.closest(".fb-crumb"); if (d) { curPath = d.dataset.p || ""; curPage = 1; loadDir(); } });
  $("fbPage").addEventListener("click", e => {
    const b = e.target.closest("[data-page]");
    if (b && !b.disabled) { curPage = parseInt(b.dataset.page) || 1; loadDir(); }
  });
  await loadKeys();
  const initKey = window._browseInitKey;
  window._browseInitKey = null;
  if (initKey) {
    $("fbKey").value = initKey;
    curKey = initKey;
    await loadDir();
  }
});
