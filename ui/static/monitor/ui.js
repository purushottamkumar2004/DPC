/* DPC UI: the clock and the live images. Everything else refreshes with HTMX. */
(function () {
  "use strict";

  function tick() {
    var el = document.getElementById("clock");
    if (el) { el.textContent = new Date().toLocaleTimeString([], {hour12: false}); }
  }

  /* Live images: load the next frame off-screen, swap it in only when complete (no flicker).
     The server reads live/IMG_1.jpg read-only; this only asks for it once a second. */
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
      };
      next.onerror = function () {
        img.dataset.loading = "0";
        box.classList.add("stale");
      };
      next.src = img.dataset.live + "?t=" + Date.now();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    tick();
    setInterval(tick, 1000);
    refreshLive();
    setInterval(refreshLive, 1000);
  });
})();
