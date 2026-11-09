"""UI-1b (D-098) dashboard fragments: one poll with out-of-band fragments, overlay, stale image,
tooltips, wheel progress, auto-ROI, sparklines, alerts."""
import json
import os
import re
import time

from django.test import TestCase

from monitor.sources import files
from monitor.state.samples import Samples, set_samples, take
from monitor.templatetags.monitor_tags import spark
from monitor.tests import helpers
from monitor.tests.test_views import ViewTestBase, write_jpeg

OOB_IDS = ["status-pill", "wheel", "fps-LEFT", "lines-LEFT", "badges-LEFT", "fps-RIGHT", "lines-RIGHT",
           "badges-RIGHT", "strip", "dash-alerts", "dash-defects"]


def write_roi(root, side, mode="filter", outside=116, drift=False, learned=True):
    d = os.path.join(root, "data", "state", f"inspection_{side.lower()}")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "roi_status.json"), "w") as f:
        json.dump({"mode": mode, "roi": {"learned_classes": ["wheel"]} if learned else None,
                   "learning": {"state": "idle"},
                   "classes": {"wheel": {"inside": 10, "outside": outside, "drift": drift},
                               "bucket_clip": {"inside": 5, "outside": 0, "drift": False}}}, f)


class DashboardLayoutTests(ViewTestBase):
    def test_page_has_reference_sections_in_order(self):
        html = self.client.get("/").content.decode()
        order = [html.index(m) for m in ('class="sb"', 'class="card top"', 'class="cams"', 'id="strip"',
                                         'class="bottom"', 'id="dash-alerts"', 'id="dash-defects"')]
        self.assertEqual(order, sorted(order))
        self.assertIn("Tadipatri" if False else "Test site", html)
        self.assertIn('href="#i-layout-dashboard"', html)           # local icon sprite
        self.assertIn('id="theme-toggle"', html)
        self.assertIsNone(re.search(r'(src|href)="https?://', html))  # nothing external (D-095)

    def test_one_poll_returns_every_fragment_out_of_band(self):
        html = self.client.get("/part/dash/").content.decode()
        for element_id in OOB_IDS:
            self.assertRegex(html, rf'id="{element_id}"[^>]*hx-swap-oob="true"', element_id)

    def test_every_value_has_a_tooltip_and_a_link(self):
        html = self.client.get("/part/dash/").content.decode()
        cells = re.findall(r'<a class="cell" href="([^"]*)" data-tip="([^"]*)"', html)
        self.assertEqual(len(cells), 8 + 6)                       # 4 tiles per camera + device strip
        for link, tip in cells:
            self.assertTrue(tip.strip(), link)
            self.assertNotEqual(link, "#")

    def test_wheel_progress_and_status_pill(self):
        html = self.client.get("/").content.decode()
        self.assertIn("Wheel 117 of 190", html)
        self.assertIn("width:62%", html)
        self.assertIn("All systems running", html)
        self.cache.handle(*helpers.status("inspection_right", "FAILED"))
        html = self.client.get("/part/banner/").content.decode()
        self.assertIn("lvl-error", html)
        self.assertIn("RIGHT inspection has stopped", html)

    def test_device_strip_values(self):
        html = self.client.get("/part/tiles/").content.decode()
        for text in ("Connected", "On", "value 1", "Mail", "Off"):
            self.assertIn(text, html)
        self.cache.handle("dpc/plc/status", {"connected": False})
        self.assertIn("Disconnected", self.client.get("/part/tiles/").content.decode())

    def test_auto_roi_tile(self):
        write_roi(self.root, "RIGHT", outside=116)
        write_roi(self.root, "LEFT", mode="observe", outside=3, drift=True)
        html = self.client.get("/part/dash/").content.decode()
        self.assertIn("Filter · 116 removed", html)
        self.assertIn("Observe · 3 outside", html)
        self.assertRegex(html, r'c-warn">Observe')               # drift = attention
        write_roi(self.root, "LEFT", learned=False)
        self.assertIn("not learned", self.client.get("/part/side/LEFT/").content.decode())


class LiveImageTests(ViewTestBase):
    def test_fresh_overlay_replaces_live_image_with_badge(self):
        overlay = files.overlay_path(self.cfg.paths["data"], "LEFT")
        os.makedirs(os.path.dirname(overlay), exist_ok=True)
        write_jpeg(overlay, 800, 450)
        r = self.client.get("/live/CAM1.jpg")
        self.assertEqual(r["X-DPC-Overlay"], "1")
        self.assertIn(">overlay<", self.client.get("/part/dash/").content.decode())
        old = time.time() - files.OVERLAY_MAX_AGE_S - 5               # inspection stopped writing
        os.utime(overlay, (old, old))
        self.assertEqual(self.client.get("/live/CAM1.jpg")["X-DPC-Overlay"], "0")
        self.assertNotIn(">overlay<", self.client.get("/part/dash/").content.decode())

    def test_frozen_image_gets_a_badge(self):
        files.get_meter().sample()
        self.assertNotIn("Last image", self.client.get("/part/dash/").content.decode())
        old = time.time() - 46
        os.utime(self.live, (old, old))
        files.get_meter().sample()
        self.assertIn("Last image 46 s old", self.client.get("/part/dash/").content.decode())


class AlertsListTests(ViewTestBase):
    def test_active_problem_not_repeated_by_its_stored_event(self):
        self.cache.handle(*helpers.status("frame_capture_cam2", "FAILED"))     # stored + active
        html = self.client.get("/part/dash/").content.decode()
        alerts = html[html.index('id="dash-alerts"'):html.index('id="dash-defects"')]
        rows = re.findall(r'<a class="row".*?</a>', alerts, re.S)
        sentence = "RIGHT camera is not delivering images and the system has stopped retrying."
        self.assertEqual(sum(sentence in r for r in rows), 1)           # "now", not again from history

    def test_stored_alerts_carry_ids_for_the_slide_in(self):
        self.cache.handle("dpc/worker/inspection_left/event", {"worker_id": "inspection_left", "event": "roi_drift",
                                                               "side": "LEFT", "class": "wheel", "ratio": 0.4})
        html = self.client.get("/part/dash/").content.decode()
        self.assertRegex(html, r'data-alert="\d+"')
        self.assertIn("#e", html)                                   # drills down to the event


class SparklineTests(TestCase):
    def test_spark_svg(self):
        self.assertEqual(spark([1.0]), "")
        svg = spark([1, 2, 3, 2], 60, 16)
        self.assertTrue(svg.startswith("<svg") and "polyline" in svg)
        self.assertNotIn("http", svg.replace('xmlns', ''))

    def test_samples_keep_one_hour(self):
        now = [1000.0]
        s = Samples(clock=lambda: now[0])
        for i in range(400):
            s.add("disk", 50 + i % 3)
            now[0] += 10
        self.assertLessEqual(len(s.values("disk")), 361)
        s.add("disk", None)
        s.add("disk", "bad")
        self.assertEqual(s.values("nothing"), [])

    def test_take_reads_existing_sources_only(self):
        root = helpers.make_root()
        self.addCleanup(helpers.remove, root)
        cfg = helpers.make_cfg(root)
        s = Samples()
        snap = {"heartbeats": {"inspection_left": {"payload": {"fps": 3.9, "ms_per_image": 85}}}}

        class Meter:
            def reading(self, cam):
                return {"fps": 4.0}
        take(cfg, snap, Meter(), {"ok": True, "gpus": [{"temperature_c": 70}]}, {"ok": True, "percent": 61.5}, s)
        self.assertEqual((s.values("fps:CAM1"), s.values("ips:LEFT"), s.values("ms:LEFT"), s.values("gpu_temp"),
                          s.values("disk")), ([4.0], [3.9], [85.0], [70.0], [61.5]))
        set_samples(Samples())
