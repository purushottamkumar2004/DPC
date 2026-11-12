/* Setup wizard (D-103): reload once a background job finishes (results are merged into the draft
   on the next page view); on the cameras step, start paused captures again when leaving the page. */
(function () {
  "use strict";
  var start = null;
  document.addEventListener("DOMContentLoaded", function () {
    var state = document.getElementById("jobstate");
    start = state ? state.getAttribute("data-finished") : "";
    document.body.addEventListener("htmx:afterSwap", function (e) {
      var s = document.getElementById("jobstate");
      if (!s || s.hasAttribute("hx-get")) { return; }
      var finished = s.getAttribute("data-finished");
      if (finished && finished !== start) { start = finished; window.location.reload(); }
    });
    var paused = document.querySelector("[data-paused]");
    if (paused && paused.getAttribute("data-paused")) {
      window.addEventListener("pagehide", function () { navigator.sendBeacon("/setup/cameras/resume/"); });
    }
  });
})();
