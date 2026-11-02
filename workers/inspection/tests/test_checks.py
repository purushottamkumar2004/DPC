"""D-085 / F42: per-check switches. A switched-off check is not built and not run: no images,
no defect rows from it, and the other checks behave exactly as with every check on.

One scripted Left scenario (plates crossing, wheels, a missing bolt, skids, wheel gaps, big wheels
without a magnet) goes through the real frame flow and the real DefectStore (fake DB cursor),
once with every check on and once with each check off.

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

import heartbeat as HB          # noqa: E402
import inspection as I          # noqa: E402
import settings as S            # noqa: E402
from defects import DefectStore  # noqa: E402

TYPES = {"bucket_clip": "Nut_Bolt_Missing_In_Bucket_Clip", "skidding": "Wheel_Skidding",
         "wheel_missing": "WheelMissing", "cycle_restart": "Batch_Wheels_Missing"}
I.logger.addHandler(logging.NullHandler())
I.logger.propagate = False


def scenario(n=1200, seed=7):
    """Detections per frame, Left camera (everything moves right to left)."""
    rng = random.Random(seed)
    frames, plate_x, wheel_x, mag_y, clip_x = [], None, 1500, 200, 1500
    bolts_missing = gap = skid = 0
    for i in range(n):
        d = []
        if i in (5, 400, 800):
            plate_x = 1300
        if plate_x is not None:
            d.append([0.9, 0, 100, plate_x, plate_x + 300, "plate"])
            plate_x = plate_x - 60 if plate_x >= 60 else None
        wheel_x = 1300 if wheel_x < 60 else wheel_x - 60
        if gap == 0 and rng.random() < 0.01:
            gap = rng.choice((11, 12, 30))
        if gap:
            gap -= 1
        else:
            width = 650 if rng.random() < 0.05 else 500
            d.append([0.9, 100, 600, wheel_x, wheel_x + width, "wheel"])
            if width == 500:                                  # big wheels come without a magnet
                if skid == 0 and rng.random() < 0.01:
                    skid = 4
                if skid:
                    skid -= 1
                else:
                    mag_y = 150 + (mag_y - 150 + 15) % 300
                d.append([0.9, mag_y, mag_y + 40, wheel_x + 100, wheel_x + 140, "wheel_magnet"])
                d.append([0.9, 330, 370, wheel_x + 230, wheel_x + 270, "wheel_bolt"])
        clip_x = 1600 if clip_x < 60 else clip_x - 40
        if rng.random() < 0.05:
            bolts_missing = rng.choice((0, 0, 1))
        d.append([0.9, 800, 980, clip_x, clip_x + 320, "bucket_clip"])
        for k in range(2 - bolts_missing):
            d.append([0.9, 850, 890, clip_x + 40 + 150 * k, clip_x + 80 + 150 * k, "nut_bolt"])
        frames.append(d)
    return frames


class ScriptedDetector:
    def __init__(self, frames):
        self.frames = list(frames)

    def detect(self, image):
        return self.frames.pop(0)

    @staticmethod
    def label(name):
        return name


def run_scenario(checks=None):
    """Returns (rows, images, side_folders, settings) for one run in a fresh layout."""
    root = tempfile.mkdtemp()
    left, _ = load_reference_settings(root)
    if checks is not None:
        left = dataclasses.replace(left, checks=checks)
    cur = FakeCursor()
    store = DefectStore(FakeConn(cur), cur, left, I.logger)
    insp = I.Inspection(left, ScriptedDetector(scenario()), store, HB.LoopStats())
    t0 = datetime.datetime(2026, 11, 2, 10, 0, 0)
    blank = np.zeros((8, 8, 3), np.uint8)
    for i in range(1200):
        name = (t0 + datetime.timedelta(milliseconds=250 * i)).strftime("%Y_%m_%d_%H_%M_%S_%f") + ".jpg"
        path = os.path.join(left.paths.inf, name)
        cv2.imwrite(path, blank)
        insp.process(path)
    rows = []
    for q in cur.queries:
        if q.lower().startswith("insert"):
            v = q.split("values(")[1].split("'")[1::2]          # side, type, path, wheel, box...
            rows.append((v[1], v[3], tuple(v[4:8])))
    images = []
    for folder in (left.paths.defective, left.paths.defect_raw, left.paths.repeated):
        for _, _, names in os.walk(folder):
            images += names
    side_folders = {"bolt_data": os.listdir(left.paths.bolt_data), "magnet": sum(
        len(n) for _, _, n in os.walk(left.paths.magnet))}
    shutil.rmtree(root, ignore_errors=True)
    return rows, images, side_folders


class CheckSwitchesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.all_on = run_scenario()            # the reference JSON has no `checks` block: default

    def test_all_on_every_type_found(self):
        rows, images, side = self.all_on
        found = {t for t, _, _ in rows}
        self.assertEqual(found, set(TYPES.values()), "the scenario must exercise every check")
        self.assertTrue(side["bolt_data"] and side["magnet"])

    def test_explicit_all_on_equals_default(self):
        rows, _, _ = run_scenario(S.Checks())
        self.assertEqual(rows, self.all_on[0])

    def test_each_check_off(self):
        for name, defect_type in TYPES.items():
            with self.subTest(check=name):
                rows, images, side = run_scenario(S.Checks(**{name: False}))
                self.assertFalse([r for r in rows if r[0] == defect_type], "no rows from the check")
                self.assertFalse([n for n in images if defect_type in n], "no images from the check")
                others = [r for r in self.all_on[0] if r[0] != defect_type]
                self.assertEqual(rows, others, "the other checks are unchanged")
                if name == "bucket_clip":
                    self.assertEqual(side["bolt_data"], [])        # candidate copies are the check's too
                if name == "skidding":
                    self.assertEqual(side["magnet"], 0)            # magnet diagnostics too


class ChecksSettingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        left, _ = load_reference_settings(cls.tmp)
        with open(left.config_path) as f:
            cls.data = json.load(f)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def load(self, checks):
        data = dict(self.data)
        if checks is None:
            data.pop("checks", None)
        else:
            data["checks"] = checks
        path = os.path.join(self.tmp, "x.json")
        with open(path, "w") as f:
            json.dump(data, f)
        return S.load_settings(path)

    def test_missing_block_all_on_with_flag(self):
        c = self.load(None).checks
        self.assertEqual((c.off(), c.from_default), ((), True))

    def test_block_read(self):
        c = self.load({"bucket_clip": True, "skidding": True, "wheel_missing": False, "cycle_restart": True}).checks
        self.assertEqual((c.off(), c.from_default), (("wheel_missing",), False))
        self.assertEqual(self.load({"skidding": False}).checks.off(), ("skidding",))   # missing name = on

    def test_bad_values_are_named(self):
        with self.assertRaises(S.SettingsError) as ctx:
            self.load({"wheel_mising": False})
        self.assertIn("wheel_mising", str(ctx.exception))
        with self.assertRaises(S.SettingsError) as ctx:
            self.load({"wheel_missing": "false"})
        self.assertIn("checks.wheel_missing", str(ctx.exception))

    def test_start_log_lines(self):
        I.logger.propagate = True
        try:
            with self.assertLogs(I.logger, "INFO") as logs:
                I.log_checks(S.Checks(wheel_missing=False))
                I.log_checks(S.Checks(from_default=True))
        finally:
            I.logger.propagate = False
        self.assertIn("INFO:inspection:Checks off: wheel_missing", logs.output)
        self.assertTrue(any(l.startswith("WARNING:") and "no 'checks' block" in l for l in logs.output))
        self.assertIn("INFO:inspection:Checks off: none (all checks on)", logs.output)


if __name__ == "__main__":
    unittest.main()
