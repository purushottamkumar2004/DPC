"""F63 (Since = now - started_at), F64 (program log, capture tab, older days), F66 (plain dashboard
image, Detections tab from the F65 pair), R6 (model on the camera cards)."""
import datetime
import json
import logging
import os
import re

from monitor.models import KnownState
from monitor.sources import detections as det_src
from monitor.sources.detections import OFF_PATH_COLOUR, PairReader, class_colour, set_reader
from monitor.state import set_cache
from monitor.state.cache import StateCache
from monitor.state.event_store import EventStore
from monitor.tests import helpers
from monitor.tests.test_views import ViewTestBase, write_jpeg


def iso_ago(seconds):
    return (datetime.datetime.now() - datetime.timedelta(seconds=seconds)).isoformat(timespec="seconds")


# ------------------------------------------------------------------------------------- F63
class SinceTests(ViewTestBase):
    def _restarted_inspection(self):
        """The UI saw inspection_left RUNNING 7 h ago (its own record); the Supervisor restarted it
        5 min ago between two status messages, so the UI never saw another state."""
        KnownState.objects.update_or_create(key="worker:inspection_left", defaults={
            "value": "RUNNING", "changed_at": datetime.datetime.now() - datetime.timedelta(hours=7)})
        cache = StateCache(recorder=EventStore())
        helpers.feed_all_running(cache)
        cache.handle(*helpers.status("inspection_left", "RUNNING", started_at=iso_ago(300), restart_count=1))
        set_cache(cache)

    def test_programs_since_is_now_minus_started_at(self):
        self._restarted_inspection()
        html = self.client.get("/part/programs/").content.decode()
        row = re.search(r"LEFT inspection</a>.*?</tr>", html, re.S).group(0)
        self.assertIn(">5 min<", row)
        self.assertNotIn("7 h", row)
        self.assertContains(self.client.get("/programs/inspection_left/"), "running for 5 min")

    def test_stopped_state_shows_time_in_state(self):
        cache = StateCache(recorder=EventStore())
        set_cache(cache)
        helpers.feed_all_running(cache)
        cache.handle(*helpers.status("plc", "FAILED", wtype="plc", started_at=iso_ago(7200)))
        html = self.client.get("/programs/plc/").content.decode()
        self.assertIn("in this state for 0 s", html)          # not 2 h: the process is gone


# ------------------------------------------------------------------------------------- F64
class LogChoiceTests(ViewTestBase):
    def write(self, name, text, folder="logs"):
        path = os.path.join(self.root, folder, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_program_log_by_default_capture_as_second_tab(self):
        capture = self.write(os.path.join("workers", "inspection_left.log"), "=== start 2026-11-10T07:50:11 ===\n")
        self.write("inspection_left.log", "INFO wheel 12 checked\n")
        self.cache.handle(*helpers.status("inspection_left", "RUNNING", log_file=capture))
        page = self.client.get("/programs/inspection_left/").content.decode()
        self.assertIn("Program log", page)
        self.assertIn("Start and crash output", page)
        r = self.client.get("/part/programs/inspection_left/log/")
        self.assertContains(r, "wheel 12 checked")
        self.assertNotContains(r, "=== start")
        r = self.client.get("/part/programs/inspection_left/log/?log=start")
        self.assertContains(r, "=== start")

    def test_names_from_rendered_json_else_mapping(self):
        with open(os.path.join(self.root, "config", "rendered", "frame_capture_cam1.json"), "w") as f:
            json.dump({"paths": {"logs": os.path.join(self.root, "logs")},
                       "log": {"file_name": "LeftCam.log"}, "db": {"password": helpers.SECRET}}, f)
        self.write("LeftCam.log", "INFO grabbed 4 fps\n")
        self.write("FrameCapture_CAM2.log", "INFO cam2 ok\n")
        expected = {"frame_capture_cam1": "grabbed 4 fps", "frame_capture_cam2": "cam2 ok",
                    "plc": "plc line", "cleanup": "cleanup line"}
        self.write("plc.log", "INFO plc line\n")
        self.write("cleanup.log", "INFO cleanup line\n")
        for worker, text in expected.items():
            r = self.client.get(f"/part/programs/{worker}/log/")
            self.assertContains(r, text, msg_prefix=worker)
            self.assertNotContains(r, helpers.SECRET)
        from monitor.sources.logs import program_log_path
        for worker, wtype, name in (("mail", "mail", "mail.log"), ("ui", "ui", "ui.log"),
                                    ("inspection_right", "inspection", "inspection_right.log")):
            self.assertEqual(os.path.basename(program_log_path(self.cfg, worker, wtype)), name)

    def test_older_days(self):
        self.write("inspection_left.log", "INFO today\n")
        self.write("inspection_left.log.2026-11-08", "INFO eighth\n")
        self.write("inspection_left.log.2026-11-09", "INFO ninth\n")
        self.write("inspection_left.log.bak", "INFO not a day\n")
        page = self.client.get("/programs/inspection_left/").content.decode()
        self.assertLess(page.index(">2026-11-09<"), page.index(">2026-11-08<"))      # newest first
        self.assertNotIn(".bak", page)
        self.assertContains(self.client.get("/part/programs/inspection_left/log/?day=2026-11-08"), "eighth")
        self.assertContains(self.client.get("/part/programs/inspection_left/log/?day=1999-01-01"), "today")
        from monitor.sources.logs import older_days
        self.write("cleanup.log.2026-11-09_10-00-00", "x\n")
        self.assertEqual(older_days(os.path.join(self.root, "logs", "cleanup.log"))[0][0], "2026-11-09 10:00:00")

    def test_ui_writes_its_own_log(self):
        import run_ui
        root_logger = logging.getLogger()
        saved, level = list(root_logger.handlers), root_logger.level
        try:
            run_ui.setup_logging(self.cfg)
            logging.getLogger("dpc.ui").info("UI own log line")
            for h in root_logger.handlers:
                h.flush()
        finally:
            for h in list(root_logger.handlers):
                root_logger.removeHandler(h)
                if h not in saved:
                    h.close()
            for h in saved:
                root_logger.addHandler(h)
            root_logger.setLevel(level)
        with open(os.path.join(self.root, "logs", "ui.log")) as f:
            self.assertIn("UI own log line", f.read())


# ------------------------------------------------------------------------------------- F66
def write_pair(root, side="left", seq=1, mode="filter", boxes=None, bands=None, mtime_ns=None):
    folder = os.path.join(root, "data", "state", f"inspection_{side}")
    os.makedirs(folder, exist_ok=True)
    frame = os.path.join(folder, "live_frame.jpg")
    write_jpeg(frame, 960, 540)
    data = {"frame": f"2026-11-10_08-00-{seq:02d}.jpg", "time": datetime.datetime.now().isoformat(timespec="milliseconds"),
            "seq": seq, "size": [960, 540], "frame_mtime_ns": mtime_ns or os.stat(frame).st_mtime_ns,
            "boxes": boxes if boxes is not None else [
                {"class": "wheel", "box": [10, 20, 110, 120], "score": 0.97, "off_path": False},
                {"class": "wheel", "box": [400, 300, 480, 380], "score": 0.61, "off_path": True},
                {"class": "bucket_clip", "box": [200, 210, 260, 330], "score": 0.9, "off_path": False}],
            "bands": bands if bands is not None else {"wheel": [[0, 200, 360], [960, 210, 370]]},
            "auto_roi_mode": mode}
    with open(os.path.join(folder, "live_detections.json"), "w") as f:
        json.dump(data, f)
    return folder, frame


class DetectionsTests(ViewTestBase):
    def setUp(self):
        super().setUp()
        set_reader(PairReader(sleep=lambda s: None))
        self.addCleanup(set_reader, PairReader())

    def test_live_tab_is_the_default_and_plain(self):
        html = self.client.get("/cameras/LEFT/").content.decode()
        self.assertIn('data-live="/live/CAM1.jpg', html)
        self.assertNotIn('id="det"', html)
        self.assertIn("?tab=detections", html)

    def test_not_available_before_f65(self):
        self.assertContains(self.client.get("/cameras/LEFT/?tab=detections"), "Detections not available yet")
        data = self.client.get("/cameras/LEFT/detections.json").json()
        self.assertEqual(data, {"available": False, "reason": "Detections not available yet."})

    def test_pair_json_classes_unticked_and_off_path_red(self):
        folder, frame = write_pair(self.root)
        data = self.client.get("/cameras/LEFT/detections.json").json()
        self.assertTrue(data["available"])
        self.assertEqual(data["image"], "/cameras/LEFT/frame/1.jpg")
        self.assertEqual([c["name"] for c in data["classes"]], ["bucket_clip", "wheel"])
        wheel = next(c for c in data["classes"] if c["name"] == "wheel")
        self.assertEqual((wheel["count"], wheel["off"], wheel["band"]), (2, 1, True))
        off = [b for b in data["boxes"] if b["off_path"]]
        self.assertEqual([(b["colour"], b["note"]) for b in off], [(OFF_PATH_COLOUR, "removed")])
        for b in data["boxes"]:
            if not b["off_path"]:
                self.assertNotEqual(b["colour"], OFF_PATH_COLOUR)
                self.assertEqual(b["note"], "")
        with open(frame, "rb") as f:
            self.assertEqual(self.client.get("/cameras/LEFT/frame/1.jpg").content, f.read())
        page = self.client.get("/cameras/LEFT/?tab=detections").content.decode()
        boxes = re.findall(r'<input type="checkbox"[^>]*>', page)
        self.assertEqual(len(boxes), 2)
        self.assertFalse(any("checked" in b for b in boxes))          # nothing drawn until ticked
        self.assertIn("detections.js", page)

    def test_observe_mode_says_would_be_removed(self):
        write_pair(self.root, side="right", mode="observe")
        data = self.client.get("/cameras/RIGHT/detections.json").json()
        self.assertEqual({b["note"] for b in data["boxes"] if b["off_path"]}, {"would be removed"})
        self.assertEqual(data["off_note"], "would be removed")

    def test_image_and_boxes_stay_one_pair(self):
        write_pair(self.root, seq=1)
        self.client.get("/cameras/LEFT/detections.json")              # page got seq 1 ...
        folder, frame = write_pair(self.root, seq=2, boxes=[])        # ... inspection writes seq 2
        old = self.client.get("/cameras/LEFT/frame/1.jpg")
        self.assertEqual(old.status_code, 200)                        # seq 1 image still served
        self.assertEqual(self.client.get("/cameras/LEFT/detections.json").json()["seq"], 2)
        self.assertEqual(self.client.get("/cameras/LEFT/frame/99.jpg").status_code, 404)

    def test_half_written_pair_is_not_mixed(self):
        write_pair(self.root, seq=1)
        self.assertEqual(self.client.get("/cameras/LEFT/detections.json").json()["seq"], 1)
        write_pair(self.root, seq=2, mtime_ns=1)                      # JSON names another image
        data = self.client.get("/cameras/LEFT/detections.json").json()
        self.assertEqual(data["seq"], 1)                              # keeps the last matched pair
        set_reader(PairReader(sleep=lambda s: None))                  # fresh reader: nothing matched yet
        self.assertFalse(self.client.get("/cameras/LEFT/detections.json").json()["available"])

    def test_class_colours_stable_and_never_red(self):
        for name in ("wheel", "bucket_clip", "half_wheel", "nut_bolt", "wheel_stand", "plate", "chain"):
            self.assertNotEqual(class_colour(name), OFF_PATH_COLOUR)
            self.assertEqual(class_colour(name), class_colour(name))
        self.assertNotIn(OFF_PATH_COLOUR, det_src.CLASS_COLOURS)

    def test_unknown_side(self):
        self.assertEqual(self.client.get("/cameras/MIDDLE/detections.json").status_code, 404)


# ------------------------------------------------------------------------------------- R6 / F60
class CameraCardTests(ViewTestBase):
    def heartbeat(self, **fields):
        payload = {"worker_id": "inspection_left", "healthy": True, "fps": 3.9, "inf_queue_depth": 2}
        payload.update(fields)
        self.cache.handle("dpc/worker/inspection_left/health", payload)

    def test_model_from_heartbeat(self):
        self.heartbeat(model={"name": "left", "version": "2026.10.29"}, ms_per_image=85)
        html = self.client.get("/part/side/LEFT/").content.decode()
        self.assertIn("left 2026.10.29", html)
        self.assertIn("85 ms/img · 2 waiting", html)
        self.heartbeat(model=None)
        self.assertIn(">Loading<", self.client.get("/part/side/LEFT/").content.decode())
        self.assertIn("does not publish its model", self.client.get("/part/side/RIGHT/").content.decode())

    def test_waiting_for_plate(self):
        self.heartbeat(waiting_for_plate=True)
        html = self.client.get("/part/side/LEFT/").content.decode()
        self.assertIn("Waiting for plate", html)
        self.assertRegex(html, r'c-warn">Waiting for plate')


class TemplateTextTests(ViewTestBase):
    def setUp(self):
        super().setUp()
        set_reader(PairReader(sleep=lambda s: None))
        self.addCleanup(set_reader, PairReader())

    def test_no_template_comment_or_tag_leaks_onto_a_page(self):
        write_pair(self.root)
        for url in ("/", "/cameras/LEFT/", "/cameras/LEFT/?tab=detections", "/cameras/RIGHT/?tab=detections",
                    "/alerts/", "/programs/", "/programs/inspection_left/", "/health/", "/defects/", "/defects/7/"):
            html = self.client.get(url).content.decode()
            for marker in ("{#", "#}", "{%", "%}", "{{"):
                self.assertNotIn(marker, html, f"{marker} on {url}")
