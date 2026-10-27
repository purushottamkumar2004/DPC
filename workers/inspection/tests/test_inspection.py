"""Tests for the shared inspection program (STATUS P7). No GPU, no DB, no broker: the model,
the DB cursor and the store are replaced by fakes; settings come from the real renderer.

Run from the repo root:
    python3 -m unittest discover -s workers/inspection/tests -t . -v
"""
import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(os.path.dirname(WORKER_DIR))
sys.path.insert(0, HERE)
sys.path.insert(0, WORKER_DIR)
sys.path.insert(0, REPO_ROOT)

import check_bucket_clip as CB            # noqa: E402
import heartbeat as HB                    # noqa: E402
import inspection as I                    # noqa: E402
import last_error as LE                   # noqa: E402
import settings as S                      # noqa: E402
from check_cycle_restart import CycleRestartCheck   # noqa: E402
from check_skidding import SkiddingCheck            # noqa: E402
from check_wheel_missing import WheelMissingCheck   # noqa: E402
from defects import DUPLICATE, INSERTED, DefectStore  # noqa: E402
from tracking import PlateTracker, WheelCounter     # noqa: E402
from wheel_report import WheelReport                 # noqa: E402
from tests_support import FakeConn, FakeCursor, det, frame_name, load_reference_settings   # noqa: E402

LOG = I.logger


class FakeStore:
    def __init__(self, outcome=INSERTED):
        self.calls, self.live, self.outcome = [], [], outcome

    def record(self, defect_type, wheel_counter, box, image, repeated_list, use_wheel_gate=True, extra_boxes=()):
        self.calls.append((defect_type, wheel_counter, box, use_wheel_gate))
        return self.outcome

    def write_live_wheel_count(self, n):
        self.live.append(n)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.left, cls.right = load_reference_settings(cls.tmp)
        cls.files = {f"inspection_{s.side.lower()}": s.config_path for s in (cls.left, cls.right)}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


# ----------------------------------------------------------------------
class SettingsTest(Base):
    def test_reference_values_per_side(self):
        l, r = self.left, self.right
        self.assertEqual((l.side, l.defect_side, l.motion, l.writes_live_wheel_counter), ("LEFT", "Left", "rtl", False))
        self.assertEqual((r.side, r.defect_side, r.motion, r.writes_live_wheel_counter), ("RIGHT", "Right", "ltr", True))
        self.assertEqual((l.tracker.clip_confirm.max_gap_ms, l.tracker.clip_confirm.max_dist_px), (300, 200))
        self.assertEqual((r.tracker.clip_confirm.max_gap_ms, r.tracker.clip_confirm.max_dist_px), (500, 500))
        self.assertEqual((l.tracker.skidding.time_gap_reset_ms, r.tracker.skidding.time_gap_reset_ms), (500, 1000))
        self.assertEqual(l.bucket_clip_window, S.Window(50, 1990, 750, 1085))
        self.assertEqual(r.bucket_clip_window, S.Window(10, 1270, 550, 780))
        self.assertEqual((l.expected_cycle_total, l.total_wheel_count), (190, 190))
        self.assertEqual(l.tracker.wheel_missing.min_frames, 9)
        self.assertEqual(l.daily_report_time, datetime.time(17, 58))
        self.assertTrue(l.paths.defect_raw.endswith("data/images/RAW/LEFT"))
        self.assertTrue(r.paths.raw.endswith("data/frames/CAM2/RAW_IMAGES"))

    def test_missing_key_is_named(self):
        with open(self.files["inspection_left"]) as f:
            data = json.load(f)
        del data["tracker"]["skidding"]["min_magnet_travel_px"]
        path = os.path.join(self.tmp, "broken.json")
        with open(path, "w") as f:
            json.dump(data, f)
        with self.assertRaises(S.SettingsError) as ctx:
            S.load_settings(path)
        self.assertIn("tracker.skidding.min_magnet_travel_px", str(ctx.exception))

    def test_summary_has_no_secret(self):
        self.assertNotIn(self.left.db.password, self.left.summary())


class WheelCounterTest(unittest.TestCase):
    def test_rtl_counts_each_wheel_once(self):
        c = WheelCounter("rtl", 100, LOG)
        # one wheel moving left over 3 frames, then the next wheel appears on the right
        for xmin in (1200, 1000, 800):
            c.update([det("wheel", xmin, 0, xmin + 500, 500)], [])
        self.assertEqual(c.count, 1)
        c.update([det("wheel", 1300, 0, 1800, 500)], [])
        self.assertEqual(c.count, 2)
        c.update([], [])
        self.assertEqual(c.count, 2)

    def test_ltr_counts_each_wheel_once(self):
        c = WheelCounter("ltr", 100, LOG)
        for xmax in (600, 900, 1200):
            c.update([], [det("half_wheel", xmax - 400, 0, xmax, 400)])
        self.assertEqual(c.count, 1)
        c.update([det("wheel", 100, 0, 500, 400)], [])
        self.assertEqual(c.count, 2)

    def test_reset_keeps_watermark(self):
        c = WheelCounter("rtl", 100, LOG)
        c.update([det("wheel", 800, 0, 1300, 500)], [])
        c.reset_count()
        c.update([det("wheel", 750, 0, 1250, 500)], [])     # same wheel: not counted again
        self.assertEqual(c.count, 0)


class PlateTrackerTest(unittest.TestCase):
    def test_first_then_same_then_new(self):
        p = PlateTracker(150, LOG)
        self.assertTrue(p.is_new_plate([det("plate", 100, 0, 400, 100)], "a"))
        self.assertFalse(p.is_new_plate([det("plate", 200, 0, 500, 100)], "b"))
        self.assertFalse(p.is_new_plate([det("plate", 300, 0, 450, 100)], "c"))
        self.assertTrue(p.is_new_plate([det("plate", 0, 0, 300, 100)], "d"))
        self.assertFalse(p.is_new_plate([], "e"))

    def test_b5_kept_plate_moving_left_retriggers(self):
        """B5 (INSPECTION.md): the tracker assumes left-to-right travel. A plate moving left
        (the Left camera) is reported again every new_plate_jump_px of travel. Kept as it is
        (P7, developer instruction: B1-B19 stay); this test fails when it is changed."""
        p = PlateTracker(150, LOG)
        hits = [p.is_new_plate([det("plate", x - 300, 0, x, 100)], "f") for x in range(1500, 900, -60)]
        self.assertEqual([i for i, h in enumerate(hits) if h], [0, 3, 6, 9])


# ----------------------------------------------------------------------
class BucketClipTest(Base):
    def setUp(self):
        self.frames = tempfile.mkdtemp(dir=self.tmp)

    def _frame(self, second, ms):
        path = os.path.join(self.frames, frame_name(second, ms))
        open(path, "wb").close()
        return path

    def test_confirmed_after_five_close_frames_then_cleared(self):
        store = FakeStore()
        check = CB.BucketClipCheck(self.left, store, LOG)
        clip = det("bucket_clip", 400, 800, 800, 1000)
        one_bolt = [det("nut_bolt", 500, 850, 540, 890)]
        for i in range(5):
            check.run(one_bolt, [clip], [], [], [], np.zeros((10, 10, 3), np.uint8), 7, [], self._frame(1, i * 250))
        self.assertEqual(store.calls, [(CB.DEFECT_TYPE, 7, (400, 800, 800, 1000), True)])
        self.assertEqual(check.candidates, {})
        self.assertEqual(len(os.listdir(self.left.paths.bolt_data)) > 0, True)

    def test_two_bolts_or_dusty_or_outside_window_no_defect(self):
        store = FakeStore()
        check = CB.BucketClipCheck(self.left, store, LOG)
        clip = det("bucket_clip", 400, 800, 800, 1000)
        two = [det("nut_bolt", 500, 850, 540, 890), det("nut_bolt", 700, 850, 740, 890)]
        dusty = [det("high_dusty_bucket_clip", 410, 800, 790, 1000)]
        high = det("bucket_clip", 400, 100, 800, 300)                     # above the window
        for i in range(6):
            img = np.zeros((10, 10, 3), np.uint8)
            check.run(two, [clip], [], [], [], img, 7, [], self._frame(2, i * 250))
            check.run([], [clip], dusty, [], [], img, 7, [], self._frame(3, i * 250))
            check.run([], [high], [], [], [], img, 7, [], self._frame(4, i * 250))
        self.assertEqual(store.calls, [])

    def test_slow_frames_not_confirmed(self):
        confirm = self.left.tracker.clip_confirm
        t = datetime.datetime(2026, 1, 1)
        cands = {str(i): {"timestamp": t + datetime.timedelta(milliseconds=400 * i), "centroid": (0, 0)} for i in range(5)}
        self.assertFalse(CB.confirmed_over_frames(cands, confirm))     # 400 ms > 300 ms on Left
        self.assertTrue(CB.confirmed_over_frames(cands, self.right.tracker.clip_confirm))   # < 500 ms

    def test_half_clip_needs_half_high_dusty_detection(self):
        store = FakeStore()
        check = CB.BucketClipCheck(self.left, store, LOG)
        half = det("half_bucket_clip", 400, 500, 600, 700)
        img = np.zeros((10, 10, 3), np.uint8)
        check.run([], [], [], [half], [], img, 3, [], self._frame(5, 0))       # B3: never checked
        self.assertEqual(store.calls, [])
        far_dusty = [det("half_high_dusty_bucket_clip", 1500, 500, 1700, 700)]
        check.run([], [], [], [half], far_dusty, img, 3, [], self._frame(5, 250))
        self.assertEqual(store.calls, [(CB.DEFECT_TYPE, 3, (400, 500, 600, 700), False)])


class WheelMissingTest(Base):
    def run_frames(self, check, pattern, counter=5):
        img = np.zeros((4, 4, 3), np.uint8)
        for seen in pattern:
            wheels = [det("wheel", 0, 0, 10, 10)] if seen else []
            check.run(wheels, [], img, counter, [])

    def test_gap_reported_once(self):
        store = FakeStore()
        check = WheelMissingCheck(self.left, store, LOG)
        self.run_frames(check, [True] + [False] * 10 + [True])
        self.assertEqual([c[0] for c in store.calls], ["WheelMissing"])
        self.run_frames(check, [False] * 10 + [True])                     # is_reported stays (B7)
        self.assertEqual(len(store.calls), 1)

    def test_short_gap_and_belt_stop_not_reported(self):
        store = FakeStore()
        check = WheelMissingCheck(self.left, store, LOG)
        self.run_frames(check, [True] + [False] * 8 + [True])              # 8 < 9
        self.run_frames(check, [False] * 25 + [True])                      # belt stopped
        self.assertEqual(store.calls, [])

    def test_previous_slot_evaluated_when_counter_moves(self):
        store = FakeStore()
        check = WheelMissingCheck(self.left, store, LOG)
        self.run_frames(check, [True] + [False] * 10, counter=8)
        self.run_frames(check, [True], counter=9)
        self.assertEqual([(c[0], c[1]) for c in store.calls], [("WheelMissing", 8)])


class SkiddingTest(Base):
    def _run(self, check, magnet_y_values, wheel_x_step=20):
        img = np.zeros((4, 4, 3), np.uint8)
        for i, my in enumerate(magnet_y_values):
            x = 300 + i * wheel_x_step
            wheel = det("wheel", x, 100, x + 500, 600)
            magnet = det("wheel_magnet", x + 100, my, x + 140, my + 40)
            bolt = det("wheel_bolt", x + 230, 330, x + 270, 370)
            check.run([wheel], [magnet], [bolt], img, 12, [], os.path.join(self.tmp, frame_name(10, i * 250)))

    def test_magnet_not_moving_vertically_is_skidding(self):
        store = FakeStore()
        self._run(SkiddingCheck(self.left, store, LOG), [200, 201, 202])
        self.assertEqual([(c[0], c[1]) for c in store.calls], [("Wheel_Skidding", 12)])

    def test_rotating_wheel_and_stopped_belt_are_not(self):
        store = FakeStore()
        self._run(SkiddingCheck(self.left, store, LOG), [200, 205, 210, 215])    # 10 px over 3 frames
        self._run(SkiddingCheck(self.left, store, LOG), [200, 200, 200], wheel_x_step=0)   # belt stopped
        self.assertEqual(store.calls, [])


class CycleRestartTest(Base):
    def test_short_cycle_only(self):
        store = FakeStore()
        check = CycleRestartCheck(self.left, store, LOG)
        img = np.zeros((4, 4, 3), np.uint8)
        for count in (0, 190, 200, 120):
            check.run(count, img, [])
        self.assertEqual(store.calls, [("Batch_Wheels_Missing", 120, (0, 0, 0, 0), True)])


# ----------------------------------------------------------------------
class DefectStoreTest(Base):
    def store(self, found=False):
        cur = FakeCursor(found)
        return DefectStore(FakeConn(cur), cur, self.right, LOG), cur

    def test_new_defect_images_then_row(self):
        store, cur = self.store()
        img = np.zeros((20, 20, 3), np.uint8)
        self.assertEqual(store.record("WheelMissing", 4, (1, 2, 3, 4), img, []), INSERTED)
        insert = cur.queries[-1]
        self.assertIn("'Right','WheelMissing'", insert)
        boxed = insert.split("'")[5]
        self.assertTrue(os.path.isfile(boxed) and "/DEFECTIVE/RIGHT/" in boxed, boxed)

    def test_duplicate_and_gate_go_to_repeated(self):
        store, cur = self.store(found=True)
        img = np.zeros((20, 20, 3), np.uint8)
        self.assertEqual(store.record("Wheel_Skidding", 4, (1, 2, 3, 4), img, []), DUPLICATE)
        store, cur = self.store(found=False)
        self.assertEqual(store.record("WheelMissing", 191, (1, 2, 3, 4), img, []), DUPLICATE)
        self.assertFalse(any(q.startswith("insert") for q in cur.queries))


class WheelReportTest(Base):
    def test_daily_copy_skips_missing_images(self):
        report = WheelReport(self.right, LOG)
        present = os.path.join(self.right.paths.raw, frame_name(20))
        open(present, "wb").close()
        report.update(det("wheel", 400, 0, 900, 500), 2, present)          # centre 650: in band
        report.update(det("wheel", 400, 0, 900, 500), 3, os.path.join(self.right.paths.raw, frame_name(21)))
        report.run_daily(datetime.datetime(2026, 10, 26, 17, 58, 5))
        day = os.path.join(self.right.paths.wheel_status_images, "2026-10-26")
        self.assertEqual(os.listdir(day), [f"wheel_2_{frame_name(20)}"])
        with open(os.path.join(self.right.paths.wheel_status_images, "wheel.txt")) as f:
            self.assertEqual(len(f.read().splitlines()), 3)                 # wheels 1 (empty), 2, 3


# ----------------------------------------------------------------------
class FakeDetector:
    def __init__(self, script):
        self.script = script        # frame name -> detections

    def detect(self, image):
        return self.script.pop(0)

    @staticmethod
    def label(name):
        return name


class InspectionFlowTest(Base):
    def test_skip_until_plate_then_count_and_live_counter(self):
        s = self.right
        store = FakeStore()
        wheel = det("wheel", 300, 100, 800, 600)
        script = [[wheel], [det("plate", 100, 0, 400, 100)], [wheel], [det("wheel", 600, 100, 1100, 600)]]
        insp = I.Inspection(s, FakeDetector(script), store, HB.LoopStats())
        for i in range(4):
            path = os.path.join(s.paths.inf, frame_name(30 + i))
            cv2.imwrite(path, np.zeros((8, 8, 3), np.uint8))
            insp.process(path)
            self.assertFalse(os.path.exists(path))
            self.assertTrue(os.path.exists(os.path.join(s.paths.raw, frame_name(30 + i))))
        self.assertEqual(insp.wheel_counter, 1)
        self.assertEqual(store.live, [1])                                     # Right writes it
        self.assertEqual(os.listdir(s.paths.plate), [frame_name(31)])


class ExitTest(Base):
    def test_last_error_scrubbed(self):
        try:
            raise RuntimeError("login failed for secret-pw")
        except RuntimeError as e:
            path = LE.write_last_error(self.tmp, "inspection_left", e, ("secret-pw",))
        with open(path) as f:
            rec = json.load(f)
        self.assertEqual(rec["type"], "RuntimeError")
        self.assertIn("test_inspection.py:", rec["where"])      # the test file is not under the worker code dir

    def test_last_error_where_points_at_own_code(self):
        import defects
        store = defects.DefectStore(FakeConn(FakeCursor()), None, self.left, LOG)   # cursor None: insert fails
        try:
            store.insert("WheelMissing", "x.jpg", 1, 0, 0, 0, 0)
        except AttributeError as e:
            rec = LE.error_record(e)
        self.assertTrue(rec["where"].startswith("defects.py:") and rec["where"].endswith("in insert"), rec["where"])
        self.assertNotIn("secret-pw", json.dumps(rec))

    def test_bad_config_exits_1_with_last_error(self):
        rendered = os.path.join(self.tmp, "config", "rendered")
        bad = os.path.join(rendered, "inspection_bad.json")
        with open(bad, "w") as f:
            f.write("{}")
        old = os.environ.get(S.CONFIG_ENV)
        os.environ[S.CONFIG_ENV] = bad
        try:
            self.assertEqual(I.main(), 1)
        finally:
            os.environ.pop(S.CONFIG_ENV) if old is None else os.environ.__setitem__(S.CONFIG_ENV, old)
        with open(os.path.join(self.tmp, "run", "inspection_bad.last_error.json")) as f:
            self.assertEqual(json.load(f)["type"], "SettingsError")

    def test_heartbeat_payload(self):
        stats = HB.LoopStats()
        stats.frame_done()
        p = HB.heartbeat_payload("inspection_left", stats, "v0.1", "abc")
        self.assertTrue(p["healthy"])
        self.assertIn("last_frame_age_s", p)
        stale = HB.heartbeat_payload("inspection_left", stats, "v0.1", "abc", now=stats.last_tick + 61)
        self.assertFalse(stale["healthy"])


if __name__ == "__main__":
    unittest.main()
