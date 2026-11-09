/* DPC UI (D-098): clock, theme toggle, tooltips, live images, in-place updates, new alerts.
   Values are refreshed by HTMX (one poll, small out-of-band fragments). */
(function () {
  "use strict";

  /* ---- clock ---- */
  function tick() {
    var el = document.getElementById("clock");
    if (!el) { return; }
    var d = new Date();
    el.textContent = d.toLocaleDateString([], {weekday: "short", day: "numeric", month: "short"}) + ", " +
                     d.toLocaleTimeString([], {hour12: false});
  }

  /* ---- theme: dark by default, light stored per browser ---- */
  function toggleTheme() {
    var light = document.documentElement.dataset.theme !== "light";
    if (light) { document.documentElement.dataset.theme = "light"; } else { delete document.documentElement.dataset.theme; }
    try { localStorage.setItem("dpc-theme", light ? "light" : "dark"); } catch (e) { /* private mode */ }
  }

  /* ---- tooltips: one floating element, never clipped by a card ---- */
  var tip;
  function showTip(target) {
    var text = target.getAttribute("data-tip");
    if (!text) { return; }
    tip.textContent = text;
    var r = target.getBoundingClientRect();
    var right = target.getAttribute("data-tip-side") === "right";
    tip.classList.add("show");
    var w = tip.offsetWidth, h = tip.offsetHeight;
    var x = right ? r.right + 10 : Math.min(window.innerWidth - w - 8, Math.max(8, r.left));
    var y = right ? r.top + (r.height - h) / 2 : r.bottom + 6;
    if (!right && y + h > window.innerHeight - 8) { y = r.top - h - 6; }
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }
  function hideTip() { tip.classList.remove("show"); }

  /* ---- live images: next frame off-screen, swapped in when complete (no flicker) ---- */
  function refreshLive() {
    document.querySelectorAll("img[data-live]").forEach(function (img) {
      if (img.dataset.loading === "1") { return; }
      img.dataset.loading = "1";
      var next = new Image();
      var box = img.parentElement;
      next.onload = function () {
        img.src = next.src;
        img.dataset.loading = "0";
        box.classList.remove("stale");
        /* the box takes the camera's own aspect ratio: no black bars (D-098 3) */
        if (next.naturalWidth && next.naturalHeight) {
          box.style.setProperty("--ar", next.naturalWidth + " / " + next.naturalHeight);
        }
      };
      next.onerror = function () { img.dataset.loading = "0"; box.classList.add("stale"); };
      next.src = img.dataset.live + "?t=" + Date.now();
    });
  }

  /* ---- new alerts slide in highlighted; changed values flash once ---- */
  var seenAlerts = null;
  function markAlerts(list) {
    var rows = list.querySelectorAll("[data-alert]");
    var first = seenAlerts === null;
    if (first) { seenAlerts = {}; }
    rows.forEach(function (row) {
      var id = row.getAttribute("data-alert");
      if (!first && !seenAlerts[id]) { row.classList.add("fresh"); }
      seenAlerts[id] = true;
    });
  }
  var lastText = {};
  function markChanged(el) {
    el.querySelectorAll(".val, .fps, .pilltext").forEach(function (v, i) {
      var key = (el.id || "") + ":" + i;
      var text = v.textContent.trim();
      if (lastText[key] !== undefined && lastText[key] !== text) { v.classList.add("changed"); }
      lastText[key] = text;
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    tip = document.createElement("div");
    tip.id = "tip";
    document.body.appendChild(tip);
    document.addEventListener("mouseover", function (e) {
      var t = e.target.closest ? e.target.closest("[data-tip]") : null;
      if (t) { showTip(t); } else { hideTip(); }
    });
    document.addEventListener("scroll", hideTip, true);
    var toggle = document.getElementById("theme-toggle");
    if (toggle) { toggle.addEventListener("click", toggleTheme); }
    document.querySelectorAll("a.later").forEach(function (a) {
      a.addEventListener("click", function (e) { e.preventDefault(); });
    });
    var alerts = document.getElementById("dash-alerts");
    if (alerts) { markAlerts(alerts); }
    document.querySelectorAll("[id]").forEach(markChanged);
    document.body.addEventListener("htmx:oobAfterSwap", function (e) {
      var el = e.detail.target;
      if (!el) { return; }
      if (el.id === "dash-alerts") { markAlerts(el); }
      markChanged(el);
    });
    tick();
    setInterval(tick, 1000);
    refreshLive();
    setInterval(refreshLive, 1000);
  });
})();
