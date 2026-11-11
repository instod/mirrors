// 轮询：页面不可见自动暂停；返回 stop()，存入 c._cleanup 离页时停。
export function poller(fn, ms = 30000) {
  let timer = null, running = true;
  const tick = async () => {
    if (!running) return;
    if (document.visibilityState === "visible") {
      try { await fn(); } catch (e) { console.warn("poller:", e); }
    }
    if (running) timer = setTimeout(tick, ms);
  };
  timer = setTimeout(tick, 0);
  return () => { running = false; if (timer) clearTimeout(timer); };
}
