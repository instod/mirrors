// API 层：基址 + fetch 封装（CSRF 自动注入 / 401 弹登录 / 403-CSRF 重登）。
import { state, setCsrf } from "./state.js";
import { toast } from "./dom.js";

export const API = (p) => "/api/admin" + p;

// 401（未登录）/ 会话过期回调，由 main.js 注入 showLogin，避免循环依赖。
let _on401 = null;
export const setUnauthorizedHandler = (fn) => { _on401 = fn; };

// 通用 fetch 封装：写请求自动注入 X-CSRF-Token；401 弹登录；403-CSRF 提示后重登。
export async function api(path, opts = {}) {
  opts = opts || {};
  const method = (opts.method || "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD" && state.csrf) {
    opts.headers = Object.assign({}, opts.headers || {}, { "X-CSRF-Token": state.csrf });
  }
  const r = await fetch(API(path), opts);
  if (r.status === 401) { if (_on401) _on401(); throw new Error("未登录"); }
  if (r.status === 403) {
    try {
      const d = await r.clone().json();
      if (d && d.error && String(d.error).includes("CSRF")) {
        toast("会话已过期，请重新登录", "error");
        setCsrf("");
        if (_on401) _on401();
        throw new Error("CSRF");
      }
    } catch (e) { if (e.message === "CSRF") throw e; }
  }
  return r;
}

// 便捷方法
export const getJSON = async (path) => (await api(path)).json();
export const postJSON = (path, data) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) });
export const putJSON = (path, data) => api(path, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) });
export const delReq = (path) => api(path, { method: "DELETE" });
