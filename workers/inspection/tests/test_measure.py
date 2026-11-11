"""S6: measure tool (box sizes per class, movement direction). Real package check, fake predictor
that reads the frame number from the image and returns scripted detections.

Run from the repo root:
    python3 -m unittest discover -s workers/inspection/tests -t . -v
"""
import datetime
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import tests_support  # noqa: E402,F401  (paths)

import measure as M                           # noqa: E402
from model import map_label                   # noqa: E402
from test_model_selftest import make_package, snapshot   # noqa: E402

W = 1920
T0 = datetime.datetime(2026, 11, 12, 10, 0, 0)


def det(cls, xmin, ymin, xmax, ymax):
    return [0.9, ymin, ymax, xmin, xmax, cls]


def conveyor(n, direction, step=60, stop=None, rng=None):
    """Scripted detections: wheels 500 x 495 crossing the image in `direction`, one wheel every
    1200 px; 4 bucket clips 320 x 180 per frame; frames in `stop` (a range) do not move."""
    rng = rng or random.Random(1)
    frames, offset = [], 0
    for i in range(n):
        if not (stop and i in stop):
            offset += step
        dets = []
        for k in range(-1, 3):
            pos = (offset + 1200 * k) % 2400 - 250           # wheel centre travelling right
            cx = pos if direction == "ltr" else W - pos
            if 250 <= cx <= W - 250:
                dets.append(det("wheel", cx - 250, 100, cx + 250, 595))
        for _ in range(4):
            x = rng.uniform(0, W - 320)
            dets.append(det("bucket_clip", x, 800, x + rng.choice((316, 320, 324)), 980))
        dets.append(det("half_bucket_clip", 100, 500, 260, 700))
        frames.append(dets)
    return frames


class Fake:
    def __init__(self, package, script):
        self.label_map, self.script = dict(package.label_map) if package else {}, script

    def detect(self, image):
        i = int(image[0, 0, 0]) + 256 * int(image[0, 0, 1])
        return self.script[i]

    def label(self, name):
        return map_label(self.label_map, name)


def write_frames(folder, n, gap_ms=250):
    os.makedirs(folder, exist_ok=True)
    for i in range(n):
        img = np.zeros((40, 40, 3), np.uint8)
        img[:, :, 0], img[:, :, 1] = i % 256, i // 256
        name = (T0 + datetime.timedelta(milliseconds=gap_ms * i)).strftime("%Y_%m_%d_%H_%M_%S_%f") + ".png"
        cv2.imwrite(os.path.join(folder, name), img)          # png: exact pixel values
        os.rename(os.path.join(folder, name), os.path.join(folder, name[:-4] + ".jpg"))


class DirectionTest(unittest.TestCase):
    def track(self, script, gap_s=0.25):
        out = []
        for i, dets in enumerate(script):
            wheels = [((d[3] + d[4]) / 2, d[4] - d[3]) for d in dets if d[5] == "wheel"]
            out.append((T0 + datetime.timedelta(seconds=gap_s * i), wheels))
        return out

    def test_rtl_and_ltr(self):
        for direction, sign in (("rtl", -1), ("ltr", 1)):
            value, ev, reason = M.direction_from(self.track(conveyor(40, direction)))
            self.assertEqual(value, direction, reason)
            self.assertEqual(ev["consistency"], 1.0)
            self.assertAlmostEqual(ev["mean_x_step"], 60 * sign, delta=1)

    def test_stopped_belt_unknown(self):
        value, ev, reason = M.direction_from(self.track(conveyor(40, "rtl", stop=range(0, 40))))
        self.assertEqual(value, "unknown")
        self.assertEqual(ev["moving_steps"], 0)
        self.assertIn("conveyor stopped", reason)

    def test_short_stop_and_noise_still_found(self):
        script = conveyor(60, "rtl", stop=range(10, 25))
        for i in range(0, 60, 9):
            script[i].append(det("wheel", 1700, 100, 1900, 300))   # a wrong small wheel at the edge
        value, ev, _ = M.direction_from(self.track(script))
        self.assertEqual(value, "rtl")
        self.assertGreaterEqual(ev["consistency"], 0.8)

    def test_inconsistent_unknown(self):
        rng = random.Random(3)
        track = [(T0 + datetime.timedelta(seconds=0.25 * i), [(900 + rng.choice((-40, 40)), 500)]) for i in range(40)]
        value, _, reason = M.direction_from(track)
        self.assertEqual(value, "unknown")
        self.assertIn("agree", reason)

    def test_frames_far_apart_not_paired(self):
        value, ev, _ = M.direction_from(self.track(conveyor(20, "ltr"), gap_s=5))
        self.assertEqual((value, ev["frame_pairs"]), ("unknown", 0))


class SizesTest(unittest.TestCase):
    def test_medians_percentiles_and_proposal(self):
        sizes = M.class_sizes({"wheel": [(500, 495)] * 9 + [(260, 495)],
                               "bucket_clip": [(316, 180), (320, 180), (324, 180)],
                               "bottom_chain": [(880, 60)] * 3})
        self.assertEqual((sizes["wheel"]["w_median"], sizes["wheel"]["h_median"], sizes["wheel"]["boxes"]), (500, 495, 10))
        self.assertLess(sizes["wheel"]["w_p10"], 500)
        proposal = M.propose_object_sizes(sizes)
        self.assertEqual(proposal["wheel_w"]["value"], 500)
        self.assertEqual(proposal["wheel_h"]["value"], 495)
        self.assertEqual(proposal["bucket_clip_w"]["value"], 320)
        self.assertEqual((proposal["chain_w"]["value"], proposal["chain_w"]["class"]), (880, "bottom_chain"))
        self.assertIsNone(proposal["half_bucket_clip_w"])                 # class not seen


class SideSettingsTest(unittest.TestCase):
    def test_model_and_threshold_from_rendered_json(self):
        root = tempfile.mkdtemp()
        try:
            left, right = tests_support.load_reference_settings(root)
            self.assertEqual(M.side_settings(root, "LEFT"), (left.model_name, left.threshold))
            self.assertEqual(M.side_settings(root, "RIGHT"), (right.model_name, right.threshold))
        finally:
            shutil.rmtree(root, ignore_errors=True)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.models = os.path.join(self.tmp, "data", "models")
        make_package(self.models)
        os.symlink("v2", os.path.join(self.models, "dpc_test", "current"))
        self.frames = os.path.join(self.tmp, "frames")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_it(self, script, count=40, version=None):
        return M.run(self.frames, count, self.models, "dpc_test", version,
                     make_detector=lambda package, thr: Fake(package, script), out=lambda t: None)

    def test_both_sides_like_tadipatri(self):
        for direction in ("rtl", "ltr"):
            shutil.rmtree(self.frames, ignore_errors=True)
            write_frames(self.frames, 50)
            code, result = self.run_it(conveyor(50, direction))
            self.assertEqual(code, 0, result)
            self.assertEqual(result["direction"]["value"], direction)
            self.assertEqual(result["object_sizes"]["wheel_w"]["value"], 500)
            self.assertEqual(result["object_sizes"]["bucket_clip_w"]["value"], 320)
            self.assertEqual(result["object_sizes"]["half_bucket_clip_w"]["value"], 160)
            self.assertEqual(result["frames"], {"given": 40, "read": 40})          # newest 40 of 50
            self.assertEqual(result["model"], {"name": "dpc_test", "version": "v2"})

    def test_errors(self):
        write_frames(self.frames, 5)
        self.assertIn("not usable", self.run_it({}, version="v9")[1]["error"])
        for f in os.listdir(self.frames):
            os.remove(os.path.join(self.frames, f))
        self.assertIn("no .jpg frames", self.run_it({})[1]["error"])

    def test_read_only(self):
        write_frames(self.frames, 30)
        before = (snapshot(self.frames), snapshot(self.models))
        self.run_it(conveyor(30, "rtl"))
        self.assertEqual((snapshot(self.frames), snapshot(self.models)), before)

    def test_cli_json_no_db(self):
        write_frames(self.frames, 30)
        script = (
            "import sys, json; sys.path.insert(0, %r); sys.path.insert(0, %r)\n"
            "import measure as M, test_measure as T\n"
            "M._real_detector = lambda package, thr: T.Fake(package, T.conveyor(30, 'ltr'))\n"
            "code = M.main(sys.argv[1:])\n"
            "assert 'pymysql' not in sys.modules, 'pymysql imported'\n"
            "sys.exit(code)\n") % (WORKER_DIR, HERE)
        ok = subprocess.run([sys.executable, "-c", script, "--frames", self.frames, "--model", "dpc_test",
                             "--models-dir", self.models, "--count", "30", "--json"], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        result = json.loads(ok.stdout.strip().splitlines()[-1])
        self.assertEqual(result["direction"]["value"], "ltr")
        bad = subprocess.run([sys.executable, "-c", script, "--frames", self.frames, "--model", "missing",
                              "--models-dir", self.models], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("MEASURE FAIL", bad.stdout)


if __name__ == "__main__":
    unittest.main()
