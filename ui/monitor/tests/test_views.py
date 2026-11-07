"""Page tests with the Django test client, a fake MQTT cache feed, a fake site DB and a
temporary site root (frames, logs, workers.yaml). No broker, MySQL, camera or GPU needed."""
import os
import sys
import time

import cv2
import numpy as np
from django.test import TestCase

from dpc_ui import ui_config
from monitor import state as state_module
from monitor.models import Event
from monitor.sources import dpc_check, files, sitedb
from monitor.state.cache import StateCache
from monitor.state.event_store import EventStore
from monitor.tests import helpers


def write_jpeg(path, width=1920, height=1080):
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, : width // 2] = (40, 120, 200)
    cv2.imwrite(path, image)


class ViewTestBase(TestCase):
    def setUp(self):
        self.root = helpers.make_root()
        self.addCleanup(helpers.remove, self.root)
        old_cfg = ui_config.get_config()
        self.addCleanup(ui_config.set_config, old_cfg)
        self.cfg = helpers.make_cfg(self.root)
        ui_config.set_config(self.cfg)
        helpers.write_workers_file(self.root)
        self.db = helpers.FakeSiteDB(self.root)
        sitedb.set_sitedb(self.db)
        self.addCleanup(sitedb.set_sitedb, None)
        self.store = EventStore()
        self.cache = StateCache(recorder=self.store)
        state_module.set_cache(self.cache)
        self.addCleanup(state_module.set_cache, None)
        files.set_meter(files.FrameMeter(self.cfg.paths["frames"], ["CAM1", "CAM2"]))
        self.addCleanup(files.set_meter, None)
        helpers.feed_all_running(self.cache)
        self.live = files.live_path(self.cfg.paths["frames"], "CAM1")
        write_jpeg(self.live)


class DashboardTests(ViewTestBase):
    def test_dashboard_green(self):
        files.get_meter().sample()
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("Everything is working.", html)
        self.assertIn("LEFT camera is running.", html)
        self.assertIn("117", html)                          # live wheel
        self.assertIn("Wheel missing", html)                # last defects
        self.assertIn("Defect mails are switched off", html)
        self.assertIn("htmx-2.0.4.min.js", html)
        self.assertNotIn("cdn", html.lower())
        self.assertNotIn(helpers.SECRET, html)

    def test_partials_refresh_with_problem(self):
        self.cache.handle(*helpers.status("frame_capture_cam1", "FAILED"))
        r = self.client.get("/part/banner/")
        self.assertContains(r, "LEFT camera is not delivering images")
        self.assertContains(r, "lvl-error")
        r = self.client.get("/part/side/LEFT/")
        self.assertContains(r, "stopped retrying")
        self.assertEqual(self.client.get("/part/side/NOPE/").status_code, 404)
        self.assertEqual(self.client.get("/part/tiles/").status_code, 200)
        self.assertEqual(self.client.get("/part/recent-defects/").status_code, 200)

    def test_live_image_is_read_only_and_downscaled(self):
        before = os.stat(self.live)
        r = self.client.get("/live/CAM1.jpg?w=640")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "image/jpeg")
        image = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(image.shape[1], 640)
        after = os.stat(self.live)                         # still there, untouched (D-031)
        self.assertEqual((before.st_ino, before.st_mtime_ns, before.st_nlink),
                         (after.st_ino, after.st_mtime_ns, after.st_nlink))
        self.assertEqual(self.client.get("/live/CAM2.jpg").status_code, 404)    # no frame yet
        self.assertEqual(self.client.get("/live/CAM9.jpg").status_code, 404)    # not configured
        self.assertEqual(self.client.get("/live/..%2F..%2Fetc.jpg").status_code, 404)

    def test_database_down_is_plain_language(self):
        self.db.fail = True
        r = self.client.get("/")
        self.assertContains(r, "The site database is not reachable.")


class AlertsTests(ViewTestBase):
    def test_events_stored_and_shown_newest_first(self):
        self.cache.handle("dpc/worker/inspection_left/event",
                          {"worker_id": "inspection_left", "event": "worker_failure",
                           "last_error": {"type": "RuntimeError", "message": "CUDA error",
                                          "where": "model.py:10 in run", "traceback": ["line 1", helpers.SECRET]}})
        self.cache.handle("dpc/worker/inspection_right/event",
                          {"worker_id": "inspection_right", "event": "roi_drift", "side": "RIGHT",
                           "class": "half_bucket_clip", "ratio": 0.3, "window": 200})
        self.assertEqual(Event.objects.count(), 2)
        html = self.client.get("/alerts/").content.decode()
        self.assertLess(html.index("RIGHT inspection: 30 %"), html.index("LEFT inspection stopped because of an error"))
        self.assertIn("Details", html)
        self.assertNotIn(helpers.SECRET, html)
        self.assertIn("Auto-ROI reported detections outside", html)    # active now: drift in the last hour

    def test_state_changes_once_and_survive_a_restart(self):
        for _ in range(3):
            self.cache.handle(*helpers.status("plc", "FAILED", wtype="plc"))
        self.assertEqual(Event.objects.filter(source="plc", name="status_changed").count(), 1)
        restarted = StateCache(recorder=EventStore())               # UI restart: retained replay
        restarted.handle(*helpers.status("plc", "FAILED", wtype="plc"))
        self.assertEqual(Event.objects.filter(source="plc", name="status_changed").count(), 1)
        r = self.client.get("/part/alerts/?level=error")
        self.assertContains(r, "The PLC service has stopped")

    def test_filters(self):
        self.cache.handle("dpc/worker/plc/event", {"worker_id": "plc", "event": "light_on_written"})
        self.assertNotContains(self.client.get("/alerts/"), "conveyor light was switched on")
        self.assertContains(self.client.get("/alerts/?level=all"), "conveyor light was switched on")
        self.assertContains(self.client.get("/alerts/?level=all&source=plc"), "conveyor light was switched on")

    def test_prune(self):
        for i in range(5):
            self.cache.handle("dpc/worker/plc/event", {"worker_id": "plc", "event": "light_on_written"})
        self.store.prune(keep_days=90, keep_rows=2)
        self.assertEqual(Event.objects.count(), 2)


class ProgramsTests(ViewTestBase):
    def test_list_and_detail_with_last_error_behind_details(self):
        log = os.path.join(self.root, "logs", "workers", "inspection_left.log")
        with open(log, "w") as f:
            f.write("INFO started\nWARNING slow frame\nERROR boom " + helpers.SECRET + "\n")
        self.cache.handle(*helpers.status("inspection_left", "WAITING_RESTART", restart_count=2, log_file=log,
                                          last_error={"at": "2026-11-06T10:01:00", "type": "ValueError",
                                                      "message": "bad box", "where": "check.py:5 in run",
                                                      "traceback": ["Traceback...", "ValueError: bad box"]}))
        html = self.client.get("/programs/").content.decode()
        self.assertIn("LEFT inspection", html)
        self.assertIn("bad box", html)
        self.assertIn("Defect mails are switched off", html)
        r = self.client.get("/programs/inspection_left/")
        self.assertContains(r, "<summary>Details</summary>", html=False)
        self.assertContains(r, "check.py:5 in run")
        r = self.client.get("/part/programs/inspection_left/log/?level=ERROR")
        self.assertContains(r, "ERROR boom ********")
        self.assertNotContains(r, "slow frame")
        r = self.client.get("/part/programs/inspection_left/log/?q=slow")
        self.assertContains(r, "WARNING slow frame")
        self.assertEqual(self.client.get("/programs/nope/").status_code, 404)

    def test_log_outside_logs_folder_not_shown(self):
        self.cache.handle(*helpers.status("plc", "RUNNING", wtype="plc", log_file="/etc/passwd"))
        r = self.client.get("/part/programs/plc/log/")
        self.assertContains(r, "outside the logs folder")
        self.assertNotContains(r, "root:")


class HealthTests(ViewTestBase):
    def test_health_page(self):
        r = self.client.get("/health/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "The site database answers.")
        self.assertContains(r, "Running; programs are watched.")
        self.assertContains(r, "The PLC answers.")
        self.assertContains(r, "Run dpc-check")
        self.assertNotContains(r, helpers.SECRET)
        self.assertEqual(self.client.get("/part/health/").status_code, 200)

    def test_dpc_check_runs_and_shows_lines(self):
        script_dir = os.path.join(self.root, "code", "scripts")
        os.makedirs(script_dir)
        with open(os.path.join(script_dir, "dpc_check.py"), "w") as f:
            f.write("import sys\nprint('PASS site.yaml ok')\nprint('WARN disk 80 %% used')\n"
                    "print('RESULT 1 warning (exit 2) in 0.1 s')\nsys.exit(2)\n")
        runner = dpc_check.DpcCheckRunner(os.path.join(self.root, "code"), self.cfg.paths["site_yaml"], python=sys.executable)
        dpc_check.set_runner(runner)
        self.addCleanup(dpc_check.set_runner, None)
        self.assertEqual(self.client.get("/health/dpc-check/").status_code, 405)
        r = self.client.post("/health/dpc-check/")
        self.assertEqual(r.status_code, 200)
        for _ in range(100):
            if not runner.snapshot()["running"]:
                break
            time.sleep(0.05)
        r = self.client.get("/part/dpc-check/")
        self.assertContains(r, "grade-WARN")
        self.assertContains(r, "exit 2")


class DefectsTests(ViewTestBase):
    def test_list_filter_and_csv(self):
        r = self.client.get("/defects/?side=Left&from=2026-11-01&to=2026-11-06&type=WheelMissing&wheel=42")
        self.assertContains(r, "1 defect found")
        self.assertEqual(self.db.last_filter.side, "Left")
        self.assertEqual(self.db.last_filter.wheel, "42")
        r = self.client.get("/defects/?side=Sideways&from=garbage")
        self.assertEqual(self.db.last_filter.side, "")
        self.assertIsNone(self.db.last_filter.date_from)
        r = self.client.get("/defects/export.csv?side=Right")
        self.assertEqual(r["Content-Type"], "text/csv")
        body = b"".join(r.streaming_content).decode()
        self.assertTrue(body.startswith("ID,create_datetime,defect_side"))
        self.assertIn("WheelMissing", body)

    def test_detail_and_image(self):
        write_jpeg(self.db.image, 800, 600)
        self.assertContains(self.client.get("/defects/7/"), "wheel 42")
        r = self.client.get("/defects/7/image.jpg")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/defects/8/image.jpg").status_code, 404)   # /etc/passwd: outside data
        self.assertEqual(self.client.get("/defects/99/").status_code, 404)

    def test_db_down(self):
        self.db.fail = True
        self.assertContains(self.client.get("/defects/"), "not reachable")
        self.assertEqual(self.client.get("/defects/export.csv").status_code, 503)


class StaticTests(ViewTestBase):
    def test_bundled_static_files(self):
        for name in ("monitor/ui.css", "monitor/ui.js", "monitor/vendor/htmx-2.0.4.min.js"):
            r = self.client.get("/static/" + name)
            self.assertEqual(r.status_code, 200, name)
