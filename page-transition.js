/*! Page transitions — VT-first, directional veil fallback */
(function () {
  "use strict";

  var EXIT_MS = 200;
  var ENTER_MS = 280;
  var FLAG = "pt-nav";
  var ORDER = { index: 0, doc: 1, soft: 2 };

  function reduceMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function supportsMPAViewTransition() {
    try {
      return "startViewTransition" in document &&
        CSS.supports("view-transition-name", "none") &&
        !reduceMotion();
    } catch (_) {
      return false;
    }
  }

  function pageKey(href) {
    try {
      var u = new URL(href, location.href);
      var path = u.pathname.replace(/\/+$/, "") || "/";
      if (path === "/" || /\/index\.html$/i.test(path) || /^\/index$/i.test(path)) return "index";
      var m = path.match(/\/(doc|soft)\.html$/i);
      return m ? m[1].toLowerCase() : "";
    } catch (_) {
      return "";
    }
  }

  function hrefFor(key) {
    if (key === "index") return "index.html";
    return key + ".html";
  }

  function isInternalPageNav(a) {
    if (!a || a.target === "_blank" || a.hasAttribute("download")) return false;
    var rel = a.getAttribute("rel") || "";
    if (/\bexternal\b/i.test(rel)) return false;
    var href = a.getAttribute("href");
    if (!href || href.charAt(0) === "#" || href.indexOf("javascript:") === 0) return false;
    try {
      var u = new URL(a.href, location.href);
      if (u.origin !== location.origin) return false;
      var from = pageKey(location.href);
      var to = pageKey(u.href);
      if (!from || !to || from === to) return false;
      if (u.pathname.replace(/\/+$/, "") === location.pathname.replace(/\/+$/, "") && !to) return false;
      return true;
    } catch (_) {
      return false;
    }
  }

  function direction(from, to) {
    var a = ORDER[from];
    var b = ORDER[to];
    if (a == null || b == null) return "forward";
    return b >= a ? "forward" : "back";
  }

  function ensureVeil() {
    var v = document.getElementById("pt-veil");
    if (!v) {
      v = document.createElement("div");
      v.id = "pt-veil";
      v.setAttribute("aria-hidden", "true");
      (document.body || document.documentElement).appendChild(v);
    }
    return v;
  }

  function setFlag(payload) {
    try { sessionStorage.setItem(FLAG, JSON.stringify(payload)); } catch (_) {}
  }

  function readFlag() {
    try {
      var raw = sessionStorage.getItem(FLAG);
      if (!raw) return null;
      sessionStorage.removeItem(FLAG);
      if (raw === "1" || raw === "vt") return { mode: raw };
      return JSON.parse(raw);
    } catch (_) {
      return null;
    }
  }

  function fallbackNavigate(href, from, to) {
    setFlag({ mode: "veil", from: from, to: to, dir: direction(from, to) });
    document.documentElement.classList.toggle("pt-dir-back", direction(from, to) === "back");
    ensureVeil();
    document.body.classList.add("pt-exit");
    window.setTimeout(function () { location.href = href; }, EXIT_MS);
  }

  function onClick(e) {
    if (e.defaultPrevented) return;
    if (e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;

    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!isInternalPageNav(a)) return;

    var from = pageKey(location.href);
    var to = pageKey(a.href);

    if (reduceMotion()) return;

    if (supportsMPAViewTransition()) {
      setFlag({ mode: "vt", from: from, to: to });
      return;
    }

    e.preventDefault();
    fallbackNavigate(a.href, from, to);
  }

  function clearBoot() {
    document.documentElement.classList.remove("pt-boot");
  }

  function onEnter() {
    var flag = readFlag();
    clearBoot();

    if (!flag || flag.mode === "vt") return;
    if (reduceMotion()) return;
    if (supportsMPAViewTransition()) return;

    if (flag.dir === "back") document.documentElement.classList.add("pt-dir-back");
    else document.documentElement.classList.remove("pt-dir-back");

    ensureVeil();
    document.body.classList.add("pt-enter");
    window.setTimeout(function () {
      document.body.classList.remove("pt-enter");
      document.documentElement.classList.remove("pt-dir-back");
      var v = document.getElementById("pt-veil");
      if (v) v.remove();
    }, ENTER_MS);
  }

  function prefetchUrl(url) {
    if (!url) return;
    var exists = document.head.querySelector('link[rel="prefetch"][href="' + url + '"]');
    if (exists) return;
    var link = document.createElement("link");
    link.rel = "prefetch";
    link.href = url;
    link.as = "document";
    document.head.appendChild(link);
  }

  function prefetchSisters() {
    var here = pageKey(location.href);
    // soft.html ~170KB：只在 hover 预取，idle 时不拉，避免首页白白下载配置页
    ["index", "doc"].forEach(function (p) {
      if (p === here) return;
      prefetchUrl(hrefFor(p));
    });
  }

  function onHoverIntent(e) {
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!isInternalPageNav(a)) return;
    prefetchUrl(a.getAttribute("href"));
  }

  document.addEventListener("click", onClick, true);
  document.addEventListener("pointerenter", onHoverIntent, true);

  window.addEventListener("pageshow", function (ev) {
    // bfcache restore: never leave boot/exit stuck
    if (ev.persisted) {
      clearBoot();
      document.body.classList.remove("pt-exit", "pt-enter");
      document.documentElement.classList.remove("pt-dir-back");
      var v = document.getElementById("pt-veil");
      if (v) v.remove();
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", onEnter);
  } else {
    onEnter();
  }

  if ("requestIdleCallback" in window) {
    requestIdleCallback(prefetchSisters, { timeout: 1600 });
  } else {
    window.setTimeout(prefetchSisters, 500);
  }
})();
