"""UI-1c (D-101): overview (level 1), side pages (level 2), System sub-tabs, 6-entry sidebar."""
import json
import os
import re

from django.conf import settings

from monitor.sources.detections import PairReader, set_reader
from monitor.state import samples as samples_mod
from monitor.state.samples import PlateClock, Samples, set_plate_clock, set_samples
from monitor.tests import helpers
from monitor.tests.test_dashboard_1b import write_roi
from monitor.tests.test_fixes_f63_f66 import write_pair
from monitor.tests.test_views import ViewTestBase, write_jpeg


def write_rendered(root, worker_id, data):
    with open(os.path.join(root, "config", "rendered", f"{worker_id}.json"), "w") as f:
        json.dump(data, f)


class Base(ViewTestBase):
    def setUp(self):
        super().setUp()
        set_samples(Samples())
        set_plate_clock(PlateClock())
        set_reader(PairReader(sleep=lambda s: None))
        self.addCleanup(set_samples, Samples())
        self.addCleanup(set_plate_clock, PlateClock())
        self.addCleanup(set_reader, PairReader())
        for side, wm in (("left", True), ("right", False)):
            write_rendered(self.root, f"inspection_{side}", {
                "writes_live_wheel_counter": side == "right", "db": {"password": helpers.SECRET},
                "model": {"name": f"dpc_maskrcnn_{side}", "models_dir": os.path.join(self.root, "data", "models")},
                "checks": {"bucket_clip": True, "skidding": True, "wheel_missing": wm, "cycle_restart": True}})

    def overview(self):
        return self.client.get("/part/overview/").content.decode()


# ------------------------------------------------------------------------------------- level 1
class OverviewTests(Base):
    def test_no_images_and_no_defect_list(self):
        html = self.client.get("/").content.decode()
        main = html[html.index("<main"):]
        self.assertNotIn("<img", main)
        self.assertNotIn("data-live", main)
        self.assertNotIn("/defects/7/", main)                    # no defect rows on level 1
        self.assertNotIn('class="spark"', main)                  # sparklines moved to the side pages

    def test_function_chips_from_checks(self):
        html = self.overview()
        right = re.search(r'(?s)id="side-RIGHT".*?</a>', html).group(0)
        self.assertIn("Wheel missing off", right)
        self.assertIn('class="dot bg-ok"></span>Bolt check', right)
        left = re.search(r'(?s)id="side-LEFT".*?</a>', html).group(0)
        self.assertEqual(left.count("bg-ok"), 4)
        os.remove(os.path.join(self.root, "config", "rendered", "inspection_left.json"))
        self.assertIn("Bolt check ?", self.overview())          # not known without the rendered file

    def test_services_list(self):
        self.cache.handle("dpc/worker/frame_capture_cam1/health", {"worker_id": "frame_capture_cam1", "fps": 4.0,
                                                                   "last_frame_age_s": 0.2, "camera_connected": True})
        self.cache.handle("dpc/worker/cleanup/health", {"worker_id": "cleanup", "last_pass_at": "2026-11-10T10:00:00",
                                                        "disk_percent": 61.6})
        self.cache.handle("dpc/worker/inspection_left/health", {"worker_id": "inspection_left", "ms_per_image": 85,
                                                                "inf_queue_depth": 3})
        html = self.overview()
        services = html[html.index('id="ov-services"'):html.index('id="ov-alerts"')]
        for name, value in (("Camera LEFT", "4.0 fps"), ("Inspection LEFT", "85 ms/img"), ("Cleanup", "disk 62 %"),
                            ("PLC service", "connected"), ("Mail sender", "Switched off")):
            row = re.search(rf'(?s)<span>{name}</span>.*?</a>', services).group(0)
            self.assertIn(value, row, name)
        self.assertIn('href="/system/programs/frame_capture_cam1/"', services)
        self.assertIn("heartbeat", services)
        self.assertIn("6 running · 1 off", html)

    def test_alerts_scroll_inside_their_card(self):
        for i in range(30):
            self.cache.handle("dpc/worker/plc/event", {"worker_id": "plc", "event": "worker_failed"})
        html = self.client.get("/").content.decode()
        self.assertRegex(html, r'(?s)class="card listcard">\s*<div class="h"><a href="/system/alerts/">Alerts</a>.*?'
                               r'<div class="scroll" id="ov-alerts">')
        self.assertGreaterEqual(html.count("data-alert="), 30)
        with open(os.path.join(settings.STATIC_DIR, "monitor", "ui.css")) as f:
            css = f.read()
        self.assertIn(".listcard .scroll { overflow-y: auto;", css)
        self.assertIn(".main.ov { display: grid; grid-template-rows: auto auto minmax(0, 1fr) auto; gap: 12px; overflow: hidden; }", css)

    def test_wheel_no_value_yet_names_the_writer(self):
        self.db.live_wheel = lambda: None                        # Tadipatri: no row ID 1 (F75)
        html = self.overview()
        self.assertIn("Cycle: wheel no value yet", html)
        self.assertIn("written by the RIGHT inspection", html)

    def test_last_plate_and_waiting(self):
        clock = PlateClock(clock=lambda: 1000.0)
        set_plate_clock(clock)
        self.assertIn("last plate not seen yet", self.overview())
        clock.observe("189")
        clock.observe("2")                                        # the wheel number started again
        clock._clock = lambda: 1000.0
        self.cache._clock = lambda: 1000.0 + 660
        self.assertIn("last plate 11 min ago", self.overview())
        self.cache.handle("dpc/worker/inspection_right/health", {"worker_id": "inspection_right", "waiting_for_plate": True})
        self.assertIn("waiting for plate", self.overview())

    def test_removed_in_the_last_hour(self):
        write_roi(self.root, "RIGHT", outside=116)
        s = Samples()
        set_samples(s)
        for total in (100, 110, 3, 9):                            # inspection restarted between 110 and 3
            s.add("roi_out:RIGHT", total)
        self.assertIn("Filter · 19 removed in the last hour", self.overview())
        self.assertIsNone(Samples().increase("x"))


# ------------------------------------------------------------------------------------- level 2
class SidePageTests(Base):
    def test_sections_in_order(self):
        write_roi(self.root, "RIGHT")
        html = self.client.get("/side/RIGHT/").content.decode()
        order = [html.index(m) for m in ('id="side-head"', 'class="card imgcard"', 'id="side-functions"', 'id="side-roi"',
                                         "<span>Model</span>", "<span>Camera</span>")]
        self.assertEqual(order, sorted(order))
        self.assertIn("RIGHT camera", html)                        # its two programs in the header
        self.assertIn("RIGHT inspection", html)

    def test_whole_frame_css(self):
        with open(os.path.join(settings.STATIC_DIR, "monitor", "ui.css")) as f:
            css = f.read()
        self.assertIn(".img img { width: 100%; height: 100%; object-fit: contain;", css)
        self.assertNotIn("object-fit: cover", css)
        self.assertNotRegex(css, r"\.img \{[^}]*max-height: calc")
        html = self.client.get("/side/LEFT/").content.decode()
        self.assertIn('class="img whole"', html)

    def test_detections_messages(self):
        html = self.client.get("/side/LEFT/?tab=detections").content.decode()
        self.assertIn("Inspection has not written detections yet; it needs the F65 version (restart inspection).", html)
        write_pair(self.root)
        html = self.client.get("/side/LEFT/?tab=detections").content.decode()
        self.assertIn("Tick a class to draw it", html)
        with open(os.path.join(settings.STATIC_DIR, "monitor", "detections.js")) as f:
            self.assertIn("Tick a class to draw it", f.read())

    def test_functions_read_only(self):
        html = self.client.get("/side/RIGHT/").content.decode()
        block = html[html.index('id="side-functions"'):html.index('id="side-roi"')]
        self.assertRegex(block, r"(?s)Wheel missing</span>.*?c-off")
        self.assertIn("switches come in UI-2", block)
        self.assertNotIn("<input", block)
        self.assertNotIn("<button", block)

    def test_roi_without_roi_json(self):
        write_roi(self.root, "LEFT", learned=False)
        html = self.client.get("/side/LEFT/").content.decode()
        self.assertIn("No region learned yet", html)

    def test_roi_with_previews_and_outside_list(self):
        folder = os.path.join(self.root, "data", "state", "inspection_right")
        write_roi(self.root, "RIGHT")
        with open(os.path.join(folder, "roi.json"), "w") as f:
            json.dump({"classes": {}}, f)
        os.makedirs(os.path.join(folder, "roi_outside"), exist_ok=True)
        write_jpeg(os.path.join(folder, "roi_preview_01_bucket_clip__wheel.jpg"), 320, 180)
        write_jpeg(os.path.join(folder, "roi_outside", "2026_11_10_12_00_00_000000_wheel.jpg"), 60, 50)
        with open(os.path.join(folder, "roi_status.json")) as f:
            status = json.load(f)
        status["roi"] = {"learned_at": "2026-11-06T16:38:02", "learned_classes": ["wheel"], "not_learned": ["wheel_stand"],
                         "previews": ["roi_preview_01_bucket_clip__wheel.jpg", "../../../etc/passwd"]}
        status["outside_list"] = {"entries": 1, "not_listed_rate_limit": 0}
        with open(os.path.join(folder, "roi_status.json"), "w") as f:
            json.dump(status, f)
        with open(os.path.join(folder, "roi_outside.jsonl"), "w") as f:
            f.write(json.dumps({"time": "2026-11-10T12:00:00.000", "frame": "f.jpg", "class": "wheel", "box": [1, 2, 3, 4],
                                "score": 0.6, "distance_px": 132.5, "mode": "filter",
                                "crop": "roi_outside/2026_11_10_12_00_00_000000_wheel.jpg"}) + "\n")
        html = self.client.get("/side/RIGHT/").content.decode()
        self.assertIn("2026-11-06 16:38:02", html)
        self.assertIn("wheel_stand", html)
        self.assertIn('src="/side/RIGHT/roi/roi_preview_01_bucket_clip__wheel.jpg"', html)
        self.assertIn('src="/side/RIGHT/roi/roi_outside/2026_11_10_12_00_00_000000_wheel.jpg"', html)
        self.assertNotIn("passwd", html)
        self.assertEqual(self.client.get("/side/RIGHT/roi/roi_preview_01_bucket_clip__wheel.jpg").status_code, 200)
        self.assertEqual(self.client.get("/side/RIGHT/roi/roi_status.json").status_code, 404)    # only images
        self.assertEqual(self.client.get("/side/RIGHT/roi/roi_outside/../roi.json").status_code, 404)

    def test_model_and_camera_cards(self):
        models = os.path.join(self.root, "data", "models", "dpc_maskrcnn_left")
        for version in ("2026.10.12", "2026.10.29"):
            os.makedirs(os.path.join(models, version))
            with open(os.path.join(models, version, "manifest.json"), "w") as f:
                json.dump({"trained_at": version}, f)
        os.symlink("2026.10.29", os.path.join(models, "current"))
        with open(os.path.join(models, "pinned"), "w") as f:
            f.write("2026.10.29\n")
        write_rendered(self.root, "frame_capture_cam1", {"camera": {"serial": "24824427", "exposure_us": 1500, "fps": 4,
                                                                    "rotate": 0, "driver": "basler"},
                                                         "db": {"password": helpers.SECRET}})
        html = self.client.get("/side/LEFT/").content.decode()
        self.assertIn("2026.10.29 (current), 2026.10.12", html)
        self.assertRegex(html, r"(?s)Pinned</span><span>2026.10.29")
        for text in ("24824427", "1500 µs", "4 fps", "0°"):
            self.assertIn(text, html)
        self.assertNotIn(helpers.SECRET, html)


# ------------------------------------------------------------------------------------- System + sidebar
class SystemAndSidebarTests(Base):
    def test_sidebar_has_six_entries_in_order(self):
        html = self.client.get("/").content.decode()
        nav = html[html.index('class="sb"'):html.index("</nav>")]
        labels = re.findall(r'<a [^>]*aria-label="([^"]+)">', nav)
        self.assertEqual(labels[:6], ["Overview", "LEFT", "RIGHT", "Defects", "System", "Setup"])
        # S8 enabled Setup (was greyed "Site setup wizard: coming" in UI-1c)
        self.assertIn('href="/setup/" class="" data-tip="Setup: the site setup wizard and commissioning"', nav)
        for gone in ("Alerts", "Programs", "Health", "Cameras", "Models", "Auto-ROI"):
            self.assertNotIn(f'aria-label="{gone}"', nav)

    def test_system_sub_tabs(self):
        for url, active in (("/system/programs/", "Programs"), ("/system/health/", "Health"),
                            ("/system/alerts/", "Alert history")):
            html = self.client.get(url).content.decode()
            tabs = html[html.index('class="tabs systabs"'):]
            tabs = tabs[:tabs.index("</nav>")]
            self.assertRegex(tabs, rf'class="on"[^>]*>{active}<', url)
            self.assertIn('class="on" data-tip="System: programs and logs', html)    # sidebar entry lit
        self.assertEqual(self.client.get("/system/").url, "/system/programs/")
        self.assertContains(self.client.get("/system/programs/inspection_left/"), "Program log")

    def test_old_addresses_still_work(self):
        for url in ("/programs/", "/alerts/", "/health/", "/programs/plc/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertEqual(self.client.get("/cameras/RIGHT/?tab=detections").url, "/side/RIGHT/?tab=detections")
