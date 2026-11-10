/* Camera page, Detections tab (F66).
   Every 3 s: fetch detections.json; when its seq changed, load that pair's image off-screen and
   then swap image and drawing together, so the boxes always belong to the image shown (no flicker).
   Only ticked classes are drawn: their boxes in the class colour, their auto-ROI band, and their
   off-path boxes in red ("removed" in filter mode, "would be removed" in observe).
   Ticked classes are remembered per browser (localStorage), none by default. */
(function () {
  "use strict";
  var KEY = "dpc-det-classes", REFRESH_MS = 3000, SVG = "http://www.w3.org/2000/svg";
  var root, svg, img, stage, message, legend, info, classesBox;
  var data = null, shownSeq = null, loading = false;

  function load() {
    try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (e) { return []; }
  }
  var ticked = new Set(load());
  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(Array.from(ticked))); } catch (e) { /* private mode */ }
  }

  function el(name, attrs, parent) {
    var node = document.createElementNS(SVG, name);
    Object.keys(attrs).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (parent) { parent.appendChild(node); }
    return node;
  }

  /* checkboxes: one per class seen; existing ones are kept so a tick is never lost */
  function syncCheckboxes(classes) {
    classes.forEach(function (c) {
      var box = classesBox.querySelector('input[value="' + CSS.escape(c.name) + '"]');
      if (!box) {
        var label = document.createElement("label");
        label.className = "det-class";
        label.innerHTML = '<input type="checkbox"><span class="sw"></span>';
        box = label.querySelector("input");
        box.value = c.name;
        label.querySelector(".sw").style.background = c.colour;
        label.appendChild(document.createTextNode(c.name));
        classesBox.appendChild(label);
      }
      box.checked = ticked.has(c.name);
      box.parentElement.setAttribute("data-tip", c.count + " detection" + (c.count === 1 ? "" : "s") +
        (c.off ? ", " + c.off + " off path (" + data.off_note + ")" : "") + (c.band ? "; its auto-ROI band is drawn too" : ""));
    });
  }

  function draw() {
    while (svg.firstChild) { svg.removeChild(svg.firstChild); }
    if (!data || !data.available) { legend.innerHTML = ""; return; }
    var w = data.size[0], h = data.size[1];
    svg.setAttribute("viewBox", "0 0 " + w + " " + h);
    var colours = {};
    data.classes.forEach(function (c) { colours[c.name] = c.colour; });
    /* bands first (under the boxes): filled between y_min and y_max along x */
    Object.keys(data.bands).forEach(function (cls) {
      if (!ticked.has(cls)) { return; }
      var pts = data.bands[cls];
      var top = pts.map(function (p) { return p[0] + "," + p[1]; });
      var bottom = pts.slice().reverse().map(function (p) { return p[0] + "," + p[2]; });
      el("polygon", {points: top.concat(bottom).join(" "), fill: colours[cls], "fill-opacity": "0.10",
                     stroke: colours[cls], "stroke-opacity": "0.8", "stroke-dasharray": "6 4",
                     "stroke-width": "1.5", "vector-effect": "non-scaling-stroke"}, svg);
    });
    data.boxes.forEach(function (b) {
      if (!ticked.has(b["class"])) { return; }
      var x1 = b.box[0], y1 = b.box[1], x2 = b.box[2], y2 = b.box[3];
      var g = el("g", {"class": b.off_path ? "off" : "on"}, svg);
      el("rect", {x: x1, y: y1, width: Math.max(1, x2 - x1), height: Math.max(1, y2 - y1), fill: "none",
                  stroke: b.colour, "stroke-width": "2", "vector-effect": "non-scaling-stroke"}, g);
      if (b.off_path) {
        var t = el("text", {x: x1, y: Math.max(12, y1 - 4), fill: b.colour, "font-size": "13", "font-weight": "600"}, g);
        t.textContent = b.note;
      }
    });
    /* legend: ticked classes with counts, then red = off path */
    var parts = [];
    data.classes.forEach(function (c) {
      if (ticked.has(c.name)) {
        parts.push('<span><span class="sw" style="background:' + c.colour + '"></span>' + c.name + " " + c.count +
                   (c.band ? " · band" : "") + "</span>");
      }
    });
    if (parts.length) {
      parts.push('<span><span class="sw" style="background:' + data.off_colour + '"></span>red = ' + data.off_note +
                 " (auto-ROI " + data.auto_roi_mode + ")</span>");
    } else {
      parts.push('<span class="muted">Tick a class to draw its detections.</span>');
    }
    legend.innerHTML = parts.join("");
  }

  function showInfo() {
    if (!data || !data.available) { info.textContent = ""; return; }
    info.textContent = "Frame " + (data.frame || "?") + " · " + (data.time || "").replace("T", " ") +
      (data.age_s != null ? " · " + Math.round(data.age_s) + " s ago" : "") +
      (data.stale ? " · inspection has not written new detections for a while" : "");
    info.classList.toggle("c-warn", !!data.stale);
  }

  function apply(next) {
    data = next;
    message.hidden = !!next.available;
    stage.hidden = !next.available;
    if (!next.available) { message.textContent = next.reason; legend.innerHTML = ""; info.textContent = ""; return; }
    stage.style.setProperty("--ar", next.size[0] + " / " + next.size[1]);
    syncCheckboxes(next.classes);
    draw();
    showInfo();
  }

  function poll() {
    if (loading) { return; }
    loading = true;
    fetch(root.getAttribute("data-url"), {cache: "no-store"}).then(function (r) { return r.json(); }).then(function (next) {
      if (!next.available || next.seq === shownSeq) {
        if (!next.available) { apply(next); shownSeq = null; } else { data.age_s = next.age_s; data.stale = next.stale; showInfo(); }
        loading = false;
        return;
      }
      var pre = new Image();
      pre.onload = function () { img.src = pre.src; shownSeq = next.seq; apply(next); loading = false; };
      pre.onerror = function () { loading = false; };
      pre.src = next.image;
    }).catch(function () { loading = false; });
  }

  document.addEventListener("DOMContentLoaded", function () {
    root = document.getElementById("det");
    if (!root) { return; }
    svg = document.getElementById("det-svg"); img = document.getElementById("det-img");
    stage = root.querySelector(".det-stage"); message = document.getElementById("det-message");
    legend = document.getElementById("det-legend"); info = document.getElementById("det-info");
    classesBox = document.getElementById("det-classes");
    classesBox.addEventListener("change", function (e) {
      if (e.target.type !== "checkbox") { return; }
      if (e.target.checked) { ticked.add(e.target.value); } else { ticked.delete(e.target.value); }
      save();
      draw();
    });
    classesBox.querySelectorAll("input").forEach(function (box) { box.checked = ticked.has(box.value); });
    poll();
    setInterval(poll, REFRESH_MS);
  });
})();
