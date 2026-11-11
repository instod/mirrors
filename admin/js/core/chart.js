// Chart.js 按需加载：仅 dashboard / monitor / storage / synchistory 使用。
// 优先本地 /vendor，失败再试 CDN，避免非图表页白白拉 ~205KB。

let _chartPromise = null;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error("脚本加载失败: " + src));
    document.head.appendChild(s);
  });
}

/** @returns {Promise<boolean>} Chart 是否可用 */
export function ensureChart() {
  if (window.Chart) return Promise.resolve(true);
  if (_chartPromise) return _chartPromise;
  _chartPromise = (async () => {
    const sources = [
      "/vendor/chart.umd.min.js",
      "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js",
      "https://unpkg.com/chart.js@4.4.1/dist/chart.umd.min.js",
    ];
    for (const src of sources) {
      try {
        await loadScript(src);
        if (window.Chart) {
          if (!src.startsWith("/")) console.warn("Chart.js 已从兜底源加载:", src);
          return true;
        }
      } catch {}
    }
    console.error("Chart.js 加载失败，图表视图不可用");
    return false;
  })();
  return _chartPromise;
}
