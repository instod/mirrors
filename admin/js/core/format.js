// 格式化层：状态/字节/容量/时长/相对时间。
export const STATUS_LABEL = {
  success: "成功",
  pending: "待同步",
  syncing: "同步中",
  fail: "失败",
  timeout: "同步中",  // 兼容旧值：超时已改为自动续跑，不向用户展示「超时」
  none: "未同步",
};


export function fmtBytes(n) {
  if (n == null || isNaN(n) || n < 0) return "-";
  for (const u of ["B", "K", "M", "G", "T", "P"]) { if (Math.abs(n) < 1024) return n.toFixed(n < 10 && u !== "B" ? 1 : 0) + u; n /= 1024; }
  return n.toFixed(1) + "E";
}

// 把 du -sh 输出("288T","541G","2.9G","914M")解析为 GB 浮点数；已是纯数字则当作 GB
export function parseSizeGB(v) {
  if (v == null || v === "" || v === "-" || v === "--" || v === "获取中") return 0;
  if (typeof v === "number") return v;
  const m = String(v).trim().match(/^([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]?B?|)$/i);
  if (!m) { const n = parseFloat(v); return isNaN(n) ? 0 : n; }
  const num = parseFloat(m[1]) || 0;
  const u = (m[2] || "").toUpperCase().charAt(0);
  const f = { K: 1 / 1048576, M: 1 / 1024, G: 1, T: 1024, P: 1048576, E: 1073741824 };
  return num * (f[u] || 1);
}

// GB -> 人类可读（TB 优先）
export function fmtGB(gb) {
  if (!gb || isNaN(gb)) return "--";
  if (gb >= 1024) return (gb / 1024).toFixed(2) + " TB";
  if (gb >= 1) return gb.toFixed(gb < 10 ? 2 : 1) + " GB";
  if (gb >= 1 / 1024) return (gb * 1024).toFixed(1) + " MB";
  return (gb * 1024 * 1024).toFixed(1) + " KB";
}

// 秒 -> 人类时长
export function fmtDur(sec) {
  if (sec == null || isNaN(sec)) return "--";
  sec = Math.round(sec);
  if (sec < 60) return sec + "s";
  if (sec < 3600) return Math.floor(sec / 60) + "m" + (sec % 60 ? (sec % 60) + "s" : "");
  return Math.floor(sec / 3600) + "h" + Math.round((sec % 3600) / 60) + "m";
}

// 时间字符串 -> 相对时间（x 分钟前 / 小时前 / 天前）
export function relTime(ts) {
  if (!ts) return "--";
  const t = new Date(String(ts).replace(/-/g, "/"));
  if (isNaN(t)) return String(ts);
  const diff = (Date.now() - t.getTime()) / 1000;
  if (diff < 0) return futureRel(-diff, ts);
  if (diff < 60) return "刚刚";
  if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
  if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
  if (diff < 86400 * 30) return Math.floor(diff / 86400) + " 天前";
  return String(ts).slice(0, 16);
}

// 未来时间 -> x 分钟后 / 小时后 / 天后（带 title 原值）
export function futureRel(sec, raw) {
  if (sec < 60) return "即将";
  if (sec < 3600) return Math.floor(sec / 60) + " 分钟后";
  if (sec < 86400) return Math.floor(sec / 3600) + " 小时后";
  return Math.floor(sec / 86400) + " 天后";
}

// 延迟毫秒 -> 颜色 tone（默认阈值与 DEFAULT_SETTINGS 的 upstream_latency_warn/crit 保持一致）
export function latencyTone(ms, warn = 500, crit = 1500) {
  if (ms == null || isNaN(ms)) return "";
  if (ms >= crit) return "danger";
  if (ms >= warn) return "warn";
  return "ok";
}
