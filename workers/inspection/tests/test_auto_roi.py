"""P13 auto region of interest (D-090, D-091, D-092). No GPU: detections are scripted.

Run from the repo root:
    python3 -m unittest discover -s workers/inspection/tests -t . -v
"""
import dataclasses
import datetime
import json
import logging
import os
import random
import shutil
import sys
import tempfile
import unittest

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from tests_support import FakeConn, FakeCursor, load_reference_settings   # noqa: E402

import auto_roi as A            # noqa: E402
import heartbeat as HB          # noqa: E402
import inspection as I          # noqa: E402
import settings as S            # noqa: E402
from defects import DefectStore  # noqa: E402
from test_checks import ScriptedDetector, scenario   # noqa: E402

LOG = logging.getLogger("auto_roi_test")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False
W, H = 1920, 1080
FRAME = np.zeros((H, W, 3), np.uint8)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_lines(path):
    with open(path, encoding="utf-8") as f:
        return f.readlines()


def det(cls, xmin, ymin, xmax, ymax, score=0.9):
    return [score, ymin, ymax, xmin, xmax, cls]


def slanted_clip(x, rng, noise=4):
    """A bucket clip on the path centre y = 0.1 x + 500, 300 x 100 px."""
    cy = 0.1 * x + 500 + rng.uniform(-noise, noise)
    return det("bucket_clip", x - 150, cy - 50, x + 150, cy + 50)


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.left, self.right = load_reference_settings(self.root)
        self.events = []

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def settings(self, side="LEFT", **roi):
        s = self.left if side == "LEFT" else self.right
        return dataclasses.replace(s, auto_roi=dataclasses.replace(s.auto_roi, **roi))

    def make_roi(self, s):
        return A.AutoRoi(s, LOG, lambda name, payload: self.events.append((name, payload)), "dpc_test", "v1")

    def request(self, s):
        open(os.path.join(s.paths.root, "run", s.worker_id + ".roi_learn"), "w").close()

    def state(self, s, name):
        return os.path.join(s.paths.root, "data", "state", s.worker_id, name)

    def learn_cycle(self, roi, frames):
        """plate, the frames (lists of detections), plate."""
        roi.process({"plate": [det("plate", 1300, 0, 1600, 100)]}, FRAME, "p1.jpg")
        roi.on_new_plate()
        for i, dets in enumerate(frames):
            by_class = {}
            for d in dets:
                by_class.setdefault(d[5], []).append(d)
            roi.process(by_class, FRAME, f"f{i}.jpg")
        roi.on_new_plate()


def moving_frames(n, extra=lambda i, rng: [], seed=1, step=60):
    """A wheel moving right to left by `step` px per frame (LEFT, rtl) and 4 slanted clips."""
    rng, frames, x = random.Random(seed), [], 1400
    for i in range(n):
        x = 1400 if x < 100 else x - step
        dets = [det("wheel", x, 100, x + 500, 600)]
        dets += [slanted_clip(rng.uniform(0, W), rng) for _ in range(4)]
        frames.append(dets + extra(i, rng))
    return frames


# ----------------------------------------------------------------------
class CalculationTest(unittest.TestCase):
    cfg = S.AutoRoi()

    def test_slanted_path_points(self):
        rng = random.Random(3)
        boxes, frames = [], []
        for i in range(3000):
            d = slanted_clip(rng.uniform(0, W), rng)
            boxes.append(A.box_of(d))
            frames.append(i)
        entry = A.learn_class(boxes, frames, 3000, W, self.cfg)
        self.assertTrue(entry["learned"], entry)
        self.assertAlmostEqual(entry["line"]["slope"], 0.1, delta=0.005)
        self.assertEqual(entry["x_range"], [0, W])
        for x, y_min, y_max in entry["points"]:
            # each point pools the boxes within +-96 px (D-091 b): on a slant the band covers the
            # top edge at the window's left end and the bottom edge at its right end
            left, right = max(0, x - 96), min(W, x + 96)
            top, bottom = 0.1 * left + 450, 0.1 * right + 550
            self.assertAlmostEqual(y_min, top - 4 - 100, delta=5, msg=(x, y_min))
            self.assertAlmostEqual(y_max, bottom + 4 + 100, delta=5, msg=(x, y_max))
        self.assertEqual([p[0] for p in entry["points"]], [round(k * 192) for k in range(11)])

    def test_fixed_false_detection_and_outliers_removed(self):
        rng = random.Random(4)
        boxes, frames = [], []
        for i in range(2000):
            boxes.append(A.box_of(slanted_clip(rng.uniform(0, W), rng)))
            frames.append(i)
            if i % 3 == 0:                                  # a reflection in 33 % of the frames
                boxes.append((1000 + rng.randint(-1, 1), 150, 1100, 210))
                frames.append(i)
            if i % 97 == 0:                                 # rare wrong boxes far off the line
                boxes.append((500, 950, 600, 1000))
                frames.append(i)
        entry = A.learn_class(boxes, frames, 2000, W, self.cfg)
        self.assertGreaterEqual(entry["dropped_static"], 600)
        self.assertGreaterEqual(entry["dropped_outliers"], 20)
        paths = A.RoiPaths({"learned_from": {"frame_size": [W, H]}, "classes": {"bucket_clip": entry}})
        self.assertGreater(paths.distance_outside("bucket_clip", (1000, 150, 1100, 210)), 0)
        self.assertGreater(paths.distance_outside("bucket_clip", (500, 950, 600, 1000)), 0)
        self.assertEqual(paths.distance_outside("bucket_clip", A.box_of(slanted_clip(960, rng))), 0)

    def test_too_few_boxes_not_learned(self):
        entry = A.learn_class([(100, 10, 200, 60)] * 5, list(range(5)), 2000, W, self.cfg)
        self.assertFalse(entry["learned"])
        paths = A.RoiPaths({"learned_from": {"frame_size": [W, H]}, "classes": {"plate": entry}})
        self.assertEqual(paths.distance_outside("plate", (0, 900, 10, 1000)), 0)     # never filtered

    def test_sparse_grid_point_interpolated(self):
        rng = random.Random(5)
        xs = [rng.uniform(0, 600) for _ in range(400)] + [rng.uniform(1300, W) for _ in range(400)]
        entry = A.learn_class([A.box_of(slanted_clip(x, rng)) for x in xs], list(range(800)), 800, W, self.cfg)
        point = next(p for p in entry["points"] if p[0] == 960)              # no box near x = 960
        self.assertAlmostEqual(point[1], 0.1 * 960 + 450 - 104, delta=25)

    def test_motion_judge(self):
        for motion, edges in (("rtl", (1000, 940, 940, 880, 1500)), ("ltr", (300, 360, 360, 420, 0))):
            judge = A.MotionJudge(motion, 5, 100)
            wheel = (lambda e: det("wheel", e, 0, e + 500, 500)) if motion == "rtl" else \
                    (lambda e: det("wheel", e - 500, 0, e, 500))
            got = [judge.moved([wheel(e)]) for e in edges]
            self.assertEqual(got, [False, True, False, True, True], motion)     # first, move, stop, move, new wheel
            self.assertFalse(judge.moved([]))


# ----------------------------------------------------------------------
class LearningTest(Base):
    def test_request_cycle_writes_files_and_keeps_previous(self):
        s = self.settings()
        roi = self.make_roi(s)
        self.request(s)
        frames = moving_frames(1200)
        frames[600].append(det("plate", 1000, 0, 1300, 100))               # a plate is seen once per cycle
        self.learn_cycle(roi, frames)
        data = read_json(self.state(s, "roi.json"))
        self.assertTrue(data["classes"]["bucket_clip"]["learned"])
        self.assertTrue(data["classes"]["wheel"]["learned"])
        self.assertFalse(data["classes"]["plate"]["learned"])
        self.assertEqual(data["learned_from"]["model"], {"name": "dpc_test", "version": "v1"})
        self.assertTrue(os.path.isfile(self.state(s, "roi_preview.jpg")))
        self.assertFalse(os.path.exists(os.path.join(s.paths.root, "run", s.worker_id + ".roi_learn")))
        self.assertEqual(self.events[-1][0], "roi_learned")
        self.learn_cycle(roi, moving_frames(1200, seed=2))                  # no request: nothing happens
        self.assertFalse(os.path.exists(self.state(s, "roi.prev.json")))
        self.request(s)
        self.learn_cycle(roi, moving_frames(1200, seed=2))
        self.assertEqual(read_json(self.state(s, "roi.prev.json")), data)

    def test_frames_without_wheel_movement_ignored(self):
        s = self.settings()
        roi = self.make_roi(s)
        self.request(s)
        frames = moving_frames(1200)
        stopped = [det("wheel", 700, 100, 1200, 600), det("bucket_clip", 800, 100, 1100, 160)]
        frames[300:600] = [list(stopped) for _ in range(300)]               # belt stopped: wheel still
        frames[600:650] = [[det("bucket_clip", 800, 100, 1100, 160)] for _ in range(50)]   # no wheel at all
        self.learn_cycle(roi, frames)
        data = read_json(self.state(s, "roi.json"))
        self.assertLess(data["learned_from"]["frames_used"], 860)
        paths = A.RoiPaths(data)
        self.assertGreater(paths.distance_outside("bucket_clip", (800, 100, 1100, 160)), 0)

    def test_gives_up_without_second_plate(self):
        s = self.settings(learn_max_frames=100)
        roi = self.make_roi(s)
        self.request(s)
        roi.process({}, FRAME, "a.jpg")
        roi.on_new_plate()
        for i, dets in enumerate(moving_frames(150)):
            roi.process({"wheel": dets[:1], "bucket_clip": dets[1:]}, FRAME, f"{i}.jpg")
        self.assertFalse(os.path.exists(self.state(s, "roi.json")))
        self.assertFalse(os.path.exists(os.path.join(s.paths.root, "run", s.worker_id + ".roi_learn")))
        self.assertEqual(self.events[-1][0], "roi_learn_failed")


class UsingTest(Base):
    def learned(self, **roi):
        s = self.settings(**roi)
        learner = self.make_roi(s)
        self.request(s)
        self.learn_cycle(learner, moving_frames(1200))
        return s, self.make_roi(s)                                          # a fresh start reads roi.json

    def test_filter_drops_off_path_detection(self):
        s, roi = self.learned(mode="filter")
        good, bad = slanted_clip(960, random.Random(1)), det("bucket_clip", 900, 100, 1200, 160)
        out = roi.process({"bucket_clip": [good, bad], "wheel_magnet": [det("wheel_magnet", 0, 0, 5, 5)]},
                          FRAME, "x.jpg")
        self.assertEqual(out["bucket_clip"], [good])
        self.assertEqual(len(out["wheel_magnet"]), 1)                       # class not learned: untouched

    def test_observe_keeps_everything_and_list_fills(self):
        s, roi = self.learned(mode="observe", outside_keep=5)
        for i in range(12):
            bad = det("bucket_clip", 900, 100 + i, 1200, 160 + i)
            out = roi.process({"bucket_clip": [bad]}, FRAME, f"x{i}.jpg")
            self.assertEqual(out["bucket_clip"], [bad])
        lines = [json.loads(l) for l in read_lines(self.state(s, "roi_outside.jsonl"))]
        self.assertLessEqual(len(lines), 6)                                 # trimmed back to outside_keep
        self.assertEqual(lines[-1]["class"], "bucket_clip")
        self.assertGreater(lines[-1]["distance_px"], 0)
        crops = os.listdir(self.state(s, "roi_outside"))
        self.assertEqual(sorted(crops), sorted(os.path.basename(l["crop"]) for l in lines))
        roi.write_status()
        status = read_json(self.state(s, "roi_status.json"))
        self.assertEqual(status["classes"]["bucket_clip"]["outside"], 12)

    def test_rate_limit_counts_but_does_not_list(self):
        s, roi = self.learned(mode="observe", outside_max_per_minute=3)
        for i in range(10):
            roi.process({"bucket_clip": [det("bucket_clip", 900, 100, 1200, 160)]}, FRAME, f"x{i}.jpg")
        self.assertLessEqual(len(read_lines(self.state(s, "roi_outside.jsonl"))), 3 + 1)
        self.assertEqual(roi.counts["bucket_clip"]["outside"], 10)

    def test_drift_reported_once(self):
        s, roi = self.learned(mode="observe", drift_window=20, outside_max_per_minute=0)
        self.events.clear()
        x = 1400
        for i in range(60):
            x = 1400 if x < 100 else x - 60
            roi.process({"wheel": [det("wheel", x, 100, x + 500, 600)],
                         "bucket_clip": [det("bucket_clip", 900, 100, 1200, 160)]}, FRAME, f"d{i}.jpg")
        drift = [e for e in self.events if e[0] == "roi_drift"]
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0][1]["class"], "bucket_clip")

    def test_off_and_broken_file_and_wrong_size(self):
        s, roi = self.learned(mode="off")
        bad = det("bucket_clip", 900, 100, 1200, 160)
        self.assertEqual(roi.process({"bucket_clip": [bad]}, FRAME, "x.jpg")["bucket_clip"], [bad])
        self.assertFalse(os.path.exists(self.state(s, "roi_outside.jsonl")))
        s2 = self.settings(mode="filter")
        roi = self.make_roi(s2)
        small = np.zeros((540, 960, 3), np.uint8)                           # other frame size: ignored
        self.assertEqual(roi.process({"bucket_clip": [bad]}, small, "x.jpg")["bucket_clip"], [bad])
        with open(self.state(s2, "roi.json"), "w") as f:
            f.write("{broken")
        roi = self.make_roi(s2)                                             # unreadable: ignored, no crash
        self.assertIsNone(roi.paths)
        self.assertEqual(roi.process({"bucket_clip": [bad]}, FRAME, "x.jpg")["bucket_clip"], [bad])


# ----------------------------------------------------------------------
class ObserveIdenticalDefectsTest(Base):
    """The full frame flow (scenario of test_checks) with learning on request and observe mode
    must give exactly the same defect rows as without any ROI; off-path detections injected after
    learning still reach the checks, and land in the out-of-bound list."""

    def run_flow(self, s, with_request):
        if with_request:
            self.request(s)
        cur = FakeCursor()
        frames = scenario()
        for i in range(850, 1200, 10):                                      # off-path nut bolts late in the run
            frames[i].append([0.9, 100, 140, 1000, 1040, "nut_bolt"])
        insp = I.Inspection(s, ScriptedDetector(frames), DefectStore(FakeConn(cur), cur, s, I.logger),
                            HB.LoopStats(), lambda n, p: self.events.append((n, p)))
        t0 = datetime.datetime(2026, 11, 5, 10, 0, 0)
        for i in range(1200):
            name = (t0 + datetime.timedelta(milliseconds=250 * i)).strftime("%Y_%m_%d_%H_%M_%S_%f") + ".jpg"
            path = os.path.join(s.paths.inf, name)
            cv2.imwrite(path, np.zeros((H, W, 3), np.uint8))
            insp.process(path)
        return [q.split("values(")[1].split("'")[1::2] for q in cur.queries if q.lower().startswith("insert")]

    def test_observe_identical(self):
        I.logger.addHandler(logging.NullHandler())
        I.logger.propagate = False
        plain = self.run_flow(self.settings(mode="observe"), with_request=False)
        root_b = tempfile.mkdtemp()
        try:
            left_b, _ = load_reference_settings(root_b)
            s_b = dataclasses.replace(left_b, auto_roi=dataclasses.replace(left_b.auto_roi, mode="observe",
                                                                          min_per_point=10))
            observed = self.run_flow(s_b, with_request=True)
            strip = lambda rows: [(r[1], r[3], tuple(r[4:8])) for r in rows]   # without the image path
            self.assertEqual(strip(observed), strip(plain))
            self.assertGreater(len(plain), 0)
            state = os.path.join(root_b, "data", "state", "inspection_left")
            self.assertTrue(os.path.isfile(os.path.join(state, "roi.json")))
            listed = [json.loads(l) for l in read_lines(os.path.join(state, "roi_outside.jsonl"))]
            self.assertTrue(any(e["class"] == "nut_bolt" and e["box"][1] == 100 for e in listed))
        finally:
            shutil.rmtree(root_b, ignore_errors=True)


class OfflineToolTest(Base):
    def test_offline_equals_live(self):
        """roi_offline.py on saved frames learns exactly what live learning learns from the same
        detections (plate to plate); it writes only to --out."""
        import roi_offline
        s = self.settings()
        # the first plate is last seen at x 1000; the next one enters at 1300 (D-077: a new plate)
        frames = [[det("plate", 1000, 0, 1300, 100)]] + moving_frames(1000) + [[det("plate", 1300, 0, 1600, 100)]]
        folder = os.path.join(self.root, "saved")
        os.makedirs(folder)
        paths = []
        for i in range(len(frames)):
            paths.append(os.path.join(folder, f"2026_11_05_10_{i // 240:02d}_{(i // 4) % 60:02d}_{(i % 4) * 250000:06d}.jpg"))
            cv2.imwrite(paths[-1], np.zeros((H, W, 3), np.uint8))
        data, message, preview = roi_offline.learn_from_frames(s, paths, detector=ScriptedDetector(frames),
                                                               package_info=("dpc_test", "v1"), out=lambda m: None)
        live = self.make_roi(s)
        self.request(s)
        self.learn_cycle(live, frames[1:-1])
        self.assertEqual(data["classes"], read_json(self.state(s, "roi.json"))["classes"])
        self.assertEqual(preview.shape, (H, W, 3))


class SettingsTest(unittest.TestCase):
    def test_defaults_and_errors(self):
        self.assertEqual(S._auto_roi({"auto_roi": {"mode": "observe", "learn_images": 30}}).defaulted,
                         tuple(S.AUTO_ROI_KEYS))
        got = S._auto_roi({"auto_roi": {"mode": "filter", "margin_px": 80, "static_share": 0.3}})
        self.assertEqual((got.mode, got.margin_px, got.static_share), ("filter", 80, 0.3))
        for bad, key in (({"mode": "learn"}, "auto_roi.mode"), ({"margin_px": -1}, "auto_roi.margin_px"),
                         ({"static_share": "0.2"}, "auto_roi.static_share"),
                         ({"top_percentile": 50, "bottom_percentile": 50}, "top_percentile")):
            with self.assertRaises(S.SettingsError) as ctx:
                S._auto_roi({"auto_roi": bad})
            self.assertIn(key, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
