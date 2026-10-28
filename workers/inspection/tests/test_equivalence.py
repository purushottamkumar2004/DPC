"""Same decisions as the old code: the old functions of LeftAlgorithm.py / RightAlgorithm.py
(Detectron2 stubbed, never called) and the new classes get the same random detection
sequences; their decisions must match. Covers the wheel counter (both directions), the plate
tracker, the bucket clip check, wheel missing and skidding. Plate on Left: since D-077 the new
tracker deliberately differs from the old Left plate code (B5); it is compared with the old Left
wheel-counter rule fed with plates, which is exactly what D-077 asks for. The old files stay in the repo
until Tadipatri runs the new program (P7 handoff); this test goes with them.

Run from the repo root:
    python3 -m unittest discover -s workers/inspection/tests -t . -v
"""
import importlib
import logging
import os
import random
import shutil
import sys
import tempfile
import types
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, WORKER_DIR)

from tests_support import (FakeConn, FakeCursor, det, frame_name, load_reference_settings)   # noqa: E402
import check_bucket_clip as CB                     # noqa: E402
from check_skidding import SkiddingCheck           # noqa: E402
from check_wheel_missing import WheelMissingCheck  # noqa: E402
from defects import DefectStore                    # noqa: E402
from tracking import PlateTracker, WheelCounter    # noqa: E402

LOG = logging.getLogger("equivalence")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


def _stub_detectron2():
    for name in ("detectron2", "detectron2.utils", "detectron2.utils.logger", "detectron2.engine",
                 "detectron2.config", "detectron2.data", "detectron2.model_zoo"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["detectron2.utils.logger"].setup_logger = lambda *a, **k: None
    sys.modules["detectron2.engine"].DefaultPredictor = object
    sys.modules["detectron2.config"].get_cfg = lambda: None
    sys.modules["detectron2.data"].MetadataCatalog = None
    sys.modules["detectron2"].model_zoo = sys.modules["detectron2.model_zoo"]


def _old(module_name):
    _stub_detectron2()
    return importlib.import_module(module_name)


def _count_images(folder, needle):
    found = []
    for dirpath, _, names in os.walk(folder):
        found += [n for n in names if needle in n and not n.startswith("RAW_")]
    return sorted(found)


class Equivalence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.left, cls.right = load_reference_settings(cls.tmp)
        cls.L = _old("LeftAlgorithm")
        cls.R = _old("RightAlgorithm")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def old_config(self, module, settings, base):
        """configHashMap of the old program, pointing at separate scratch folders."""
        K = module.CONFIG_KEY_NAME
        dirs = {k: os.path.join(base, k) for k in ("raw", "defective", "repeated")}
        for d in dirs.values():
            os.makedirs(d, exist_ok=True)
        module.configHashMap.clear()
        module.configHashMap.update({
            K.RAW_IMAGE_DIR_PATH: dirs["raw"], K.DEFECTIVE_IMAGE_PATH: dirs["defective"],
            K.REPEATED_IMAGE_DIR_PATH: dirs["repeated"], K.DEFECT_SIDE: settings.defect_side,
            K.TOTAL_WHEEL_COUNT: settings.total_wheel_count, K.BUCKET_CLIP_WIDTH: settings.bucket_clip_w,
            K.HALF_BUCKET_CLIP_WIDTH: settings.half_bucket_clip_w})
        return dirs

    # ------------------------------------------------------------------
    def test_wheel_counter_and_plate_both_directions(self):
        rng = random.Random(1)
        for old_mod, motion in ((self.L, "rtl"), (self.R, "ltr")):
            old_state, new = {"current_wheel_counter": 0}, WheelCounter(motion, 100, LOG)
            old_plate, new_plate = {"last_plate_xmax": None}, PlateTracker(motion, 150, LOG)
            # D-077: Right keeps the old plate rule; Left's plate now follows the old Left WHEEL
            # rule (rtl) with the plate jump, so it is compared with that, not with old B5.
            old_plate_as_wheel = {"current_wheel_counter": 0}
            for i in range(3000):
                wheels = [det("wheel", x, 0, x + rng.randint(200, 600), 500)
                          for x in rng.sample(range(0, 1500), rng.randint(0, 2))]
                halves = [det("half_wheel", x, 0, x + 300, 400) for x in rng.sample(range(0, 1500), rng.randint(0, 1))]
                old = old_mod.checkWheelCounter(LOG, list(wheels), list(halves), old_state)
                self.assertEqual(old, new.update(list(wheels), list(halves)), f"{motion} frame {i}")
                plates = [det("plate", x, 0, x + 300, 100) for x in rng.sample(range(0, 1600), rng.randint(0, 1))]
                if motion == "ltr":
                    expected = old_mod.is_new_plate_detected(LOG, list(plates), "f", old_plate)
                else:
                    before = old_plate_as_wheel["current_wheel_counter"]
                    old_mod.checkWheelCounter(LOG, list(plates), [], old_plate_as_wheel, 150)
                    expected = old_plate_as_wheel["current_wheel_counter"] > before
                self.assertEqual(expected, new_plate.is_new_plate(list(plates), "f"), f"{motion} plate frame {i}")

    def test_bucket_clip_same_defects(self):
        for module, settings in ((self.L, self.left), (self.R, self.right)):
            with self.subTest(side=settings.side):
                self._bucket_clip(module, settings)

    def _bucket_clip(self, module, settings):
        rng = random.Random(2)
        base = tempfile.mkdtemp(dir=self.tmp)
        dirs = self.old_config(module, settings, os.path.join(base, "old"))
        bolt_old = os.path.join(base, "bolt_old")
        os.makedirs(bolt_old)
        frames = os.path.join(base, "frames")
        os.makedirs(frames)
        old_cur, new_cur = FakeCursor(), FakeCursor()
        new_store = DefectStore(FakeConn(new_cur), new_cur, settings, LOG)
        check = CB.BucketClipCheck(settings, new_store, LOG)
        old_dict = {}
        w = settings.bucket_clip_window
        x, missing = w.xmin + 5, 0
        for i in range(6000):
            path = os.path.join(frames, frame_name(i // 4 % 60, (i % 4) * 250 + (120 if rng.random() < 0.05 else 0)))
            open(path, "wb").close()
            x = w.xmin + rng.randint(-30, 10) if x > w.xmax - 300 else x + rng.choice((20, 30, 40, 400))
            y = w.ymin + rng.randint(-15, 40)
            missing = rng.choice((0, 1, 2)) if rng.random() < 0.1 else missing   # bolts missing for a while
            clips = [det("bucket_clip", x, y, x + rng.choice((280, 320, 330)), y + 180)] if rng.random() < 0.9 else []
            bolts = [det("nut_bolt", x + 40 + 120 * k, y + 50, x + 80 + 120 * k, y + 90) for k in range(2 - missing)]
            dusty = [det("high_dusty_bucket_clip", x + 10, y, x + 300, y + 180)] if rng.random() < 0.05 else []
            wheel = (i // 20) % 200 + 1
            img_old = np.zeros((4, 4, 3), np.uint8)
            img_new = img_old.copy()
            module.checkBucketClipNutBoltMissing(LOG, old_cur, FakeConn(old_cur), bolts, clips, dusty, [], [],
                                                 [], [], [], img_old, wheel, [], "Left", old_dict, path, bolt_old)
            check.run(bolts, clips, dusty, [], [], img_new, wheel, [], path)
        old_inserts = [q for q in old_cur.queries if q.lower().startswith("insert")]
        new_inserts = [q for q in new_cur.queries if q.lower().startswith("insert")]
        if module is self.R:      # Right's insert was commented out (R:843): compare saved images
            self.assertEqual(old_inserts, [])
            old_n = len(_count_images(dirs["defective"], CB.DEFECT_TYPE))
            self.assertEqual(old_n, len(new_inserts))
        else:
            strip = lambda q: q.split("values(")[1].split("'")[1::2]     # values without the path
            self.assertEqual([strip(q)[:2] + strip(q)[3:] for q in old_inserts],
                             [strip(q)[:2] + strip(q)[3:] for q in new_inserts])
        self.assertGreaterEqual(len(new_inserts), 5)

    def test_wheel_missing_same_slots(self):
        rng = random.Random(3)
        base = tempfile.mkdtemp(dir=self.tmp)
        dirs = self.old_config(self.L, self.left, os.path.join(base, "old"))
        cur = FakeCursor()
        new_check = WheelMissingCheck(self.left, DefectStore(FakeConn(cur), cur, self.left, LOG), LOG)
        old_hist, counter = {}, 1
        gap = 0
        for i in range(6000):
            if rng.random() < 0.02:
                counter += 1
            if gap == 0 and rng.random() < 0.02:
                gap = rng.randint(3, 26)            # a run of frames without any wheel
            seen = gap == 0
            gap = max(0, gap - 1)
            wheels = [det("wheel", 0, 0, 10, 10)] if seen else []
            img = np.zeros((4, 4, 3), np.uint8)
            self.L.checkWheelMissing(LOG, cur, None, wheels, [], [], img, counter, [], old_hist)
            new_check.run(wheels, [], img, counter, [])
        old_slots = sorted(int(n.split("WheelMissing")[1].split(".")[0])
                           for n in _count_images(dirs["defective"], "WheelMissing"))
        new_slots = sorted(int(q.split("values(")[1].split("'")[7])
                           for q in cur.queries if q.lower().startswith("insert"))
        self.assertEqual(old_slots, new_slots)
        self.assertGreaterEqual(len(new_slots), 5)

    def test_skidding_same_findings(self):
        rng = random.Random(4)
        base = tempfile.mkdtemp(dir=self.tmp)
        dirs = self.old_config(self.L, self.left, os.path.join(base, "old"))
        self.L.MAGNET_NOT_DETECTED_DIR = os.path.join(base, "magnet_old")
        self.L.SOURCE_IMAGE_DIR = os.path.join(base, "nowhere")
        cur = FakeCursor()
        new_check = SkiddingCheck(self.left, DefectStore(FakeConn(cur), cur, self.left, LOG), LOG)
        old_track = {}
        self.L.latest_image_name_list.clear()
        x, my, ms = 300, 200, 0
        for i in range(3000):
            ms += rng.choice((250, 250, 250, 600))
            x = 200 if x > 1300 else x + rng.choice((0, 20, 25, 30))
            my += rng.choice((0, 1, 3, 6, -6))
            wheel = det("wheel", x, 100, x + 500, 600)
            magnet = [det("wheel_magnet", x + 100, my, x + 140, my + 40)] if rng.random() < 0.9 else []
            bolt = [det("wheel_bolt", x + 230, 330, x + 270, 370)] if rng.random() < 0.8 else []
            path = os.path.join(base, frame_name((ms // 1000) % 60, ms % 1000))
            wheel_no = (i // 30) % 190 + 1
            self.L.checkWheelRotation(LOG, cur, None, [wheel], magnet, bolt, np.zeros((4, 4, 3), np.uint8),
                                      wheel_no, [], old_track, path)
            new_check.run([wheel], magnet, bolt, np.zeros((4, 4, 3), np.uint8), wheel_no, [], path)
        old_n = len(_count_images(dirs["defective"], "Wheel_Skidding"))
        new_n = len([q for q in cur.queries if q.lower().startswith("insert")])
        self.assertEqual(old_n, new_n)
        self.assertGreaterEqual(new_n, 5)


if __name__ == "__main__":
    unittest.main()
