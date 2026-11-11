// 全局共享状态（跨模块单例）。
export const state = {
  csrf: "",          // 写请求 CSRF token（登录/check 下发，api() 自动注入）
  mirrorsCache: [],  // mirrors 列表缓存（dashboard/mirrors/health 共用）
  charts: {},        // Chart.js 实例缓存；routeTo 切页时统一销毁
  currentRoute: null,
  editingKey: null,  // 镜像编辑模态当前 key（新增=null）
  // /alerts 含 0.5s CPU 采样，跨视图 60s TTL（与 MEMORY.md 一致）
  alertsCache: { at: 0, data: null },
};

/** 带 60s TTL 的 alerts 拉取；force=true 跳过缓存。 */
export async function fetchAlertsCached(apiFn, { force = false } = {}) {
  const now = Date.now();
  if (!force && state.alertsCache.data && now - state.alertsCache.at < 60000) {
    return state.alertsCache.data;
  }
  try {
    const al = await (await apiFn("/alerts")).json();
    state.alertsCache = { at: now, data: al };
    return al;
  } catch {
    return state.alertsCache.data;
  }
}

export const getCsrf = () => state.csrf;
export const setCsrf = (t) => { state.csrf = t || ""; };
