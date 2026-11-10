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



def write_roi(root, side, mode="filter", outside=116, drift=False, learned=True):
    d = os.path.join(root, "data", "state", f"inspection_{side.lower()}")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "roi_status.json"), "w") as f:
        json.dump({"mode": mode, "roi": {"learned_classes": ["wheel"]} if learned else None,
                   "learning": {"state": "idle"},
                   "classes": {"wheel": {"inside": 10, "outside": outside, "drift": drift},
                               "bucket_clip": {"inside": 5, "outside": 0, "drift": False}}}, f)


class DashboardLayoutTests(ViewTestBase):
    """UI-1b dashboard tests, adapted to the D-101 overview that replaces it (UI-1c)."""
    OOB_IDS = ["status-pill", "cycle", "side-LEFT", "side-RIGHT", "ov-services-sum", "strip"]

    def test_page_has_reference_sections_in_order(self):
        html = self.client.get("/").content.decode()
        order = [html.index(m) for m in ('class="sb"', 'class="card top"', 'class="sides"', 'id="side-LEFT"',
                                         'id="side-RIGHT"', 'class="lower"', 'id="ov-services"', 'id="ov-alerts"',
                                         'id="strip"')]
        self.assertEqual(order, sorted(order))
        self.assertIn("Test site", html)
        self.assertIn('href="#i-layout-dashboard"', html)           # local icon sprite
        self.assertIn('id="theme-toggle"', html)
        self.assertIsNone(re.search(r'(src|href)="https?://', html))  # nothing external (D-095)

    def test_one_poll_returns_every_fragment_out_of_band(self):
        html = self.client.get("/part/overview/").content.decode()
        for element_id in self.OOB_IDS:
            self.assertRegex(html, rf'id="{element_id}"[^>]*hx-swap-oob="true"', element_id)
        for element_id in ("ov-services", "ov-alerts"):          # lists: only their content, scroll kept
            self.assertRegex(html, rf'id="{element_id}" hx-swap-oob="innerHTML"', element_id)

    def test_every_value_has_a_tooltip_and_a_link(self):
        html = self.client.get("/part/overview/").content.decode()
        cells = re.findall(r'<a class="cell" href="([^"]*)" data-tip="([^"]*)"', html)
        self.assertEqual(len(cells), 7)                           # the device strip
        for link, tip in cells:
            self.assertTrue(tip.strip(), link)
            self.assertNotEqual(link, "#")
        rows = re.findall(r'<div class="row" data-tip="([^"]*)"><span class="muted">', html)
        self.assertEqual(len(rows), 8)                            # 4 rows per side card
        self.assertTrue(all(t.strip() for t in rows))

    def test_wheel_progress_and_status_pill(self):
        html = self.client.get("/").content.decode()
        self.assertIn("Cycle: wheel 117 of 190", html)
        self.assertIn("width:62%", html)
        self.assertIn("All systems running", html)
        self.cache.handle(*helpers.status("inspection_right", "FAILED"))
        html = self.client.get("/part/banner/").content.decode()
        self.assertIn("lvl-error", html)
        self.assertIn("RIGHT inspection has stopped", html)

    def test_device_strip_values(self):
        html = self.client.get("/part/overview/").content.decode()
        strip = html[html.index('id="strip"'):]
        for text in ("Connected", "On", "value 1", "Disk", "GPU", "Database", "MQTT"):
            self.assertIn(text, strip)
        self.assertNotIn(">Mail<", strip)                        # mail is a service, listed in Services
        self.cache.handle("dpc/plc/status", {"connected": False})
        self.assertIn("Disconnected", self.client.get("/part/overview/").content.decode())

    def test_auto_roi_tile(self):
        write_roi(self.root, "RIGHT", outside=116)
        write_roi(self.root, "LEFT", mode="observe", outside=3, drift=True)
        html = self.client.get("/part/overview/").content.decode()
        self.assertIn("Filter · — removed in the last hour", html)  # no samples yet: unknown, not 0
        self.assertIn("Observe · — outside in the last hour", html)
        self.assertRegex(html, r'c-warn">Observe')                # drift = attention
        write_roi(self.root, "LEFT", learned=False)
        self.assertIn("no region learned yet", self.client.get("/part/overview/").content.decode())


class LiveImageTests(ViewTestBase):
    def test_dashboard_ignores_the_overlay(self):
        """F66: even a fresh live_overlay.jpg is not shown; plain live image only (side page)."""
        overlay = os.path.join(self.cfg.paths["data"], "state", "inspection_left", "live_overlay.jpg")
        os.makedirs(os.path.dirname(overlay), exist_ok=True)
        write_jpeg(overlay, 800, 450)
        r = self.client.get("/live/CAM1.jpg?w=1920")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("X-DPC-Overlay", r)
        self.assertEqual(r.content, files.live_image(self.cfg.paths["frames"], "CAM1", 1920))
        html = self.client.get("/").content.decode() + self.client.get("/side/LEFT/").content.decode()
        self.assertNotIn(">overlay<", html)
        self.assertNotIn("live_overlay", html)

    def test_frozen_image_gets_a_badge(self):
        files.get_meter().sample()
        self.assertNotIn("Last image", self.client.get("/side/LEFT/").content.decode())
        old = time.time() - 46
        os.utime(self.live, (old, old))
        files.get_meter().sample()
        self.assertIn("Last image 46 s old", self.client.get("/side/LEFT/").content.decode())
        self.assertIn("Last image 46 s old", self.client.get("/part/side/LEFT/page/").content.decode())


class AlertsListTests(ViewTestBase):
    def test_active_problem_not_repeated_by_its_stored_event(self):
        self.cache.handle(*helpers.status("frame_capture_cam2", "FAILED"))     # stored + active
        html = self.client.get("/part/overview/").content.decode()
        alerts = html[html.index('id="ov-alerts"'):html.index('id="strip"')]
        rows = re.findall(r'<a class="row".*?</a>', alerts, re.S)
        sentence = "RIGHT camera is not delivering images and the system has stopped retrying."
        self.assertEqual(sum(sentence in r for r in rows), 1)           # "now", not again from history

    def test_stored_alerts_carry_ids_for_the_slide_in(self):
        self.cache.handle("dpc/worker/inspection_left/event", {"worker_id": "inspection_left", "event": "roi_drift",
                                                               "side": "LEFT", "class": "wheel", "ratio": 0.4})
        html = self.client.get("/part/overview/").content.decode()
        self.assertRegex(html, r'data-alert="\d+"')
        self.assertIn("/system/alerts/#e", html)                    # drills down to the event


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


class PagesTests(ViewTestBase):
    def test_camera_page_per_side(self):
        """Adapted: the camera page is now the side page (D-101); the old address redirects."""
        write_roi(self.root, "LEFT", outside=7, drift=True)
        r = self.client.get("/side/LEFT/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("LEFT side", html)
        self.assertIn('data-live="/live/CAM1.jpg', html)
        self.assertIn(">drift<", html)
        self.assertIn("/system/programs/inspection_left/", html)
        self.assertEqual(self.client.get("/part/side/right/page/").status_code, 200)
        self.assertEqual(self.client.get("/side/MIDDLE/").status_code, 404)
        self.assertEqual(self.client.get("/cameras/LEFT/").status_code, 302)

    def test_every_page_has_the_top_bar_and_sidebar(self):
        for url in ("/system/alerts/", "/system/programs/", "/system/programs/plc/", "/system/health/",
                    "/defects/", "/defects/7/"):
            html = self.client.get(url).content.decode()
            self.assertIn('class="card top pagetop"', html, url)
            self.assertIn('hx-get="/part/banner/"', html, url)
            self.assertIn('class="sb"', html, url)
        for url in ("/side/LEFT/", "/side/RIGHT/"):
            html = self.client.get(url).content.decode()
            self.assertIn('class="card top sidehead"', html, url)
            self.assertIn('class="sb"', html, url)

    def test_dashboard_alert_links_land_on_their_event(self):
        self.cache.handle("dpc/worker/plc/event", {"worker_id": "plc", "event": "worker_failed"})
        html = self.client.get("/system/alerts/").content.decode()
        event_id = re.search(r'<li id="e(\d+)"', html).group(1)
        self.assertIn(f'href="/system/alerts/#e{event_id}"', self.client.get("/part/overview/").content.decode())
