"""F47: model self-test. The package check is real (sha256 through common/config/model_package.py);
the predictor is a fake that reads a frame number from the image and returns scripted detections.

Run from the repo root:
    python3 -m unittest discover -s workers/inspection/tests -t . -v
"""
import hashlib
import json
import os
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

import model_selftest as M          # noqa: E402
from model import map_label         # noqa: E402

CLASSES = ["wheel", "half_wheel", "bucket_clip", "nut_bolt", "dusty_wheel"]


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def make_package(models_dir, name="dpc_test", version="v2", label_map=None):
    vdir = os.path.join(models_dir, name, version)
    os.makedirs(vdir)
    with open(os.path.join(vdir, "classes.json"), "w") as f:
        json.dump({"classes": CLASSES, "label_map": label_map or {}}, f)
    with open(os.path.join(vdir, "model_final.pth"), "wb") as f:
        f.write(b"fake weights")
    with open(os.path.join(vdir, "cfg.yaml"), "w") as f:
        f.write("fake: 1\n")
    sha = {n: sha256(os.path.join(vdir, n)) for n in ("classes.json", "model_final.pth", "cfg.yaml")}
    with open(os.path.join(vdir, "manifest.json"), "w") as f:
        json.dump({"name": name, "version": version, "trained_at": "2026-11-01", "files": sha}, f)
    return vdir


def write_frames(folder, n):
    """Frames named by time; the frame number is painted into the top-left pixel block."""
    os.makedirs(folder, exist_ok=True)
    paths = []
    for i in range(n):
        img = np.full((40, 40, 3), 30, np.uint8)
        img[0:16, 0:16] = i * 10
        path = os.path.join(folder, f"2026_11_03_10_00_{i:02d}_000000.jpg")
        cv2.imwrite(path, img)
        paths.append(path)
    return paths


class FakeDetector:
    """Scripted detections per frame number; label mapping exactly as inspection's Detector."""
    def __init__(self, package, script):
        self.label_map, self.script, self.seen = dict(package.label_map), script, []

    def detect(self, image):
        i = int(round(float(image[2:14, 2:14].mean()) / 10))
        self.seen.append(i)
        return [[0.9, 0, 10, 0, 10, cls] for cls in self.script.get(i, [])]

    def label(self, name):
        return map_label(self.label_map, name)


def snapshot(folder):
    out = {}
    for dirpath, dirnames, names in os.walk(folder):
        for n in names + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            p = os.path.join(dirpath, n)
            if os.path.islink(p):
                out[p] = ("link", os.readlink(p))
            else:
                st = os.stat(p)
                out[p] = (st.st_mtime_ns, st.st_size, sha256(p))
    return out


class SelfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.models = os.path.join(self.tmp, "data", "models")
        self.vdir = make_package(self.models)
        os.symlink("v1", os.path.join(self.models, "dpc_test", "current"))     # another version runs
        self.frames = os.path.join(self.tmp, "frames")
        write_frames(self.frames, 7)
        self.lines = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_it(self, script, count=5, version="v2", factory=None):
        made = []

        def make(package, threshold):
            made.append(FakeDetector(package, script))
            return made[-1]
        code, summary = M.run_selftest(self.models, "dpc_test", version, self.frames, count,
                                       make_detector=factory or make, out=self.lines.append)
        return code, summary, (made[0] if made else None)

    def test_pass_on_newest_frames_with_counts(self):
        code, summary, det = self.run_it({6: ["wheel", "bucket_clip", "nut_bolt", "nut_bolt"], 5: ["half_wheel"]})
        self.assertEqual(code, 0, self.lines)
        self.assertEqual(det.seen, [2, 3, 4, 5, 6])                      # newest 5 of 7, oldest first
        self.assertIn("2026_11_03_10_00_06_000000.jpg: bucket_clip=1, nut_bolt=2, wheel=1", self.lines)
        self.assertIn("2026_11_03_10_00_02_000000.jpg: no detections", self.lines)
        self.assertTrue(self.lines[-1].startswith("SELFTEST PASS: dpc_test v2, 5 frame(s), 1 'wheel'"))
        self.assertEqual(summary["totals"], {"bucket_clip": 1, "half_wheel": 1, "nut_bolt": 2, "wheel": 1})

    def test_no_wheel_fails(self):
        code, summary, _ = self.run_it({6: ["half_wheel", "bucket_clip"]})        # half_wheel is not wheel
        self.assertEqual(code, 1)
        self.assertIn("no 'wheel' found in 5 frame(s)", self.lines[-1])

    def test_wheel_outside_the_newest_frames_fails(self):
        code, _, _ = self.run_it({0: ["wheel"]}, count=3)
        self.assertEqual(code, 1)

    def test_label_map_applies(self):
        shutil.rmtree(self.vdir)
        make_package(self.models, label_map={"dusty_wheel": "wheel"})
        code, summary, _ = self.run_it({4: ["dusty_wheel"]})
        self.assertEqual((code, summary["totals"]), (0, {"wheel": 1}))

    def test_corrupt_package_fails_before_loading(self):
        with open(os.path.join(self.vdir, "model_final.pth"), "ab") as f:
            f.write(b"x")
        code, _, det = self.run_it({6: ["wheel"]})
        self.assertEqual((code, det), (1, None))
        self.assertIn("model_final.pth: checksum does not match", self.lines[-1])

    def test_missing_version_or_frames(self):
        self.assertEqual(self.run_it({}, version="v9")[0], 1)
        self.assertIn("version folder not found", self.lines[-1])
        for f in os.listdir(self.frames):
            os.remove(os.path.join(self.frames, f))
        self.assertEqual(self.run_it({})[0], 1)
        self.assertIn("no .jpg frames", self.lines[-1])
        shutil.rmtree(self.frames)
        self.assertEqual(self.run_it({})[0], 1)
        self.assertIn("frames folder not found", self.lines[-1])

    def test_model_load_error_fails(self):
        def broken(package, threshold):
            raise RuntimeError("CUDA error: no device")
        code, _, _ = self.run_it({}, factory=broken)
        self.assertEqual(code, 1)
        self.assertIn("model does not load: RuntimeError: CUDA error: no device", self.lines[-1])

    def test_unreadable_frames(self):
        with open(os.path.join(self.frames, "2026_11_03_10_00_06_000000.jpg"), "w") as f:
            f.write("not an image")
        code, _, _ = self.run_it({5: ["wheel"]}, count=2)
        self.assertEqual(code, 0)
        self.assertIn("2026_11_03_10_00_06_000000.jpg: unreadable, skipped", self.lines)
        self.lines.clear()
        with open(os.path.join(self.frames, "2026_11_03_10_00_05_000000.jpg"), "w") as f:
            f.write("not an image")
        self.assertEqual(self.run_it({5: ["wheel"]}, count=2)[0], 1)
        self.assertIn("none of the 2 frames could be read", self.lines[-1])

    def test_read_only(self):
        before = (snapshot(self.frames), snapshot(self.models))
        self.run_it({6: ["wheel"]})
        self.run_it({}, version="v9")
        self.assertEqual((snapshot(self.frames), snapshot(self.models)), before)
        self.assertEqual(os.readlink(os.path.join(self.models, "dpc_test", "current")), "v1")

    def test_cli_exit_codes_and_no_db(self):
        """The command line, in a fresh process: exit codes, --json, and pymysql never imported."""
        script = (
            "import sys, json; sys.path.insert(0, %r); sys.path.insert(0, %r)\n"
            "import model_selftest as M, test_model_selftest as T\n"
            "M._real_detector = lambda package, thr: T.FakeDetector(package, {6: ['wheel']})\n"
            "code = M.main(sys.argv[1:])\n"
            "assert 'pymysql' not in sys.modules, 'pymysql imported'\n"
            "sys.exit(code)\n") % (WORKER_DIR, HERE)
        args = ["--model", "dpc_test", "--version", "v2", "--frames", self.frames, "--models-dir", self.models]
        ok = subprocess.run([sys.executable, "-c", script] + args + ["--json"], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertTrue(json.loads(ok.stdout.strip().splitlines()[-1])["pass"])
        bad = subprocess.run([sys.executable, "-c", script] + args + ["--count", "1", "--version", "v9"],
                             capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1, bad.stderr)
        self.assertIn("SELFTEST FAIL", bad.stdout)

    def test_default_models_dir_follows_layout(self):
        self.assertEqual(M.default_models_dir(),
                         os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(WORKER_DIR))), "data", "models"))


if __name__ == "__main__":
    unittest.main()
