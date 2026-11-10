"""F60: heartbeat ms_per_image / waiting_for_plate, and the live overlay image for the UI.

Run from the repo root:
    python3 -m unittest discover -s workers/inspection/tests -t . -v
"""
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from tests_support import FakeConn, FakeCursor, det, frame_name, load_reference_settings   # noqa: E402

import auto_roi as A            # noqa: E402
import heartbeat as HB          # noqa: E402
import inspection as I          # noqa: E402
import live_overlay as L        # noqa: E402
from defects import DefectStore  # noqa: E402

LOG = logging.getLogger("overlay_test")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False
I.logger.addHandler(logging.NullHandler())
I.logger.propagate = False
W, H = 1920, 1080


class HeartbeatFieldsTest(unittest.TestCase):
    def test_ms_per_image_and_waiting_for_plate_appended(self):
        stats = HB.LoopStats()
        p = HB.heartbeat_payload("inspection_left", stats, "v", "c")
        self.assertEqual((p["ms_per_image"], p["waiting_for_plate"]), (None, True))
        stats.frame_done(ms=100, waiting_for_plate=True)
        stats.frame_done(ms=140, waiting_for_plate=False)
        p = HB.heartbeat_payload("inspection_left", stats, "v", "c")
        self.assertEqual((p["ms_per_image"], p["waiting_for_plate"]), (120.0, False))
        old_keys = ["worker_id", "healthy", "pid", "version", "config_hash", "fps", "inf_queue_depth", "last_frame_age_s"]
        self.assertEqual(list(p)[:len(old_keys)], old_keys)                 # append-only (D-034)
        json.dumps(p)


def flat_paths(cls="bucket_clip", y0=700, y1=1000):
    entry = {"learned": True, "x_range": [0, W], "line": {"slope": 0.0}, "points": [[0, y0, y1], [W, y0, y1]]}
    return A.RoiPaths({"learned_from": {"frame_size": [W, H]}, "classes": {cls: entry}})


class DrawTest(unittest.TestCase):
    def test_boxes_band_red_off_path_legend(self):
        image = np.full((H, W, 3), 50, np.uint8)
        inside = det("bucket_clip", 400, 800, 800, 980)
        off = det("bucket_clip", 1200, 100, 1600, 260)
        other = det("wheel", 100, 100, 700, 600)
        out = L.draw_overlay(image, {"bucket_clip": [inside, off], "wheel": [other]}, [("bucket_clip", off)],
                             flat_paths(), {"side": "LEFT", "mode": "observe", "wheel": 12, "waiting_for_plate": False})
        self.assertEqual(out.shape, (540, 960, 3))                          # downscaled, same aspect
        s = 960 / W
        def near(px, colour):
            return np.abs(px.astype(int) - colour).max() < 60
        self.assertTrue(near(out[int(160 * s), int(1400 * s)], L.RED) or near(out[int(100 * s), int(1400 * s)], L.RED))
        self.assertTrue(near(out[int(980 * s), int(600 * s)], L.class_colour("bucket_clip")))   # box bottom edge
        self.assertTrue(near(out[int(1000 * s), int(1900 * s)], L.class_colour("bucket_clip")))  # band bottom line
        self.assertFalse(near(out[int(980 * s), int(600 * s)], L.RED))
        self.assertTrue((out[2:20, 2:300] == 0).any())                      # legend box

    def test_no_red_in_class_colours_and_main_classes_distinct(self):
        main = ["wheel", "half_wheel", "bucket_clip", "half_bucket_clip", "nut_bolt", "wheel_magnet", "plate"]
        colours = [L.class_colour(c) for c in main]
        self.assertEqual(len(set(colours)), len(main))
        for colour in L.CLASS_COLOURS + list(L.KNOWN_COLOURS.values()):
            self.assertGreater(np.abs(np.array(colour) - L.RED).max(), 80, colour)


class WriterTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_interval_and_atomic(self):
        now = [100.0]
        writer = L.LiveOverlay(self.dir, LOG, interval_s=3, clock=lambda: now[0])
        image, info = np.zeros((H, W, 3), np.uint8), {"side": "LEFT", "mode": "off"}
        self.assertTrue(writer.maybe_write(image, {}, [], None, info))
        now[0] += 1
        self.assertFalse(writer.maybe_write(image, {}, [], None, info))
        now[0] += 2.5
        self.assertTrue(writer.maybe_write(image, {}, [], None, info))
        self.assertEqual(os.listdir(self.dir), ["live_overlay.jpg"])         # no temp file left
        self.assertEqual(cv2.imread(os.path.join(self.dir, "live_overlay.jpg")).shape, (540, 960, 3))


class FrameFlowTest(unittest.TestCase):
    def test_inspection_writes_overlay_with_off_path_marked(self):
        root = tempfile.mkdtemp()
        try:
            left, _ = load_reference_settings(root)
            state = os.path.join(root, "data", "state", "inspection_left")
            os.makedirs(state, exist_ok=True)
            entry = {"learned": True, "x_range": [0, W], "line": {"slope": 0.0}, "points": [[0, 700, 1000], [W, 700, 1000]]}
            with open(os.path.join(state, "roi.json"), "w") as f:
                json.dump({"schema_version": 1, "side": "LEFT", "learned_from": {"frame_size": [W, H], "time": "t"},
                           "classes": {"bucket_clip": entry}}, f)
            cur = FakeCursor()

            class Detector:
                def detect(self, image):
                    return [det("bucket_clip", 1200, 100, 1600, 260)]       # off path
                label = staticmethod(lambda n: n)
            insp = I.Inspection(left, Detector(), DefectStore(FakeConn(cur), cur, left, I.logger), HB.LoopStats())
            path = os.path.join(left.paths.inf, frame_name(1))
            cv2.imwrite(path, np.zeros((H, W, 3), np.uint8))
            insp.process(path)
            out = cv2.imread(os.path.join(state, "live_overlay.jpg"))
            self.assertEqual(out.shape, (540, 960, 3))
            self.assertEqual([c for c, _ in insp.roi.last_outside], ["bucket_clip"])
            s = 960 / W
            self.assertLess(np.abs(out[int(260 * s), int(1400 * s)].astype(int) - L.RED).max(), 60)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
