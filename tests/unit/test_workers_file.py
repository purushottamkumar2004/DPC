"""Tests for common/config/workers_file.py (STATUS S2): a generated workers.yaml passes
`supervisor.py check-config`.

Run from the repo root:
    python3 -m unittest tests.unit.test_workers_file -v
"""

import copy
import os
import subprocess
import sys
import tempfile
import unittest

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)

from common.config import workers_file as wf  # noqa: E402
from common.config.site_config import load_site_config  # noqa: E402

EXAMPLE_YAML = os.path.join(REPO_ROOT, "config", "examples", "site.example.yaml")
SUPERVISOR = os.path.join(REPO_ROOT, "supervisor", "supervisor.py")


class Generate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "INSIGHTZZ")
        os.makedirs(os.path.join(self.root, "config"))
        with open(EXAMPLE_YAML, encoding="utf-8") as f:
            self.data = yaml.safe_load(f)
        self.data["paths"]["root"] = self.root

    def tearDown(self):
        self._tmp.cleanup()

    def config(self, edit=None):
        data = copy.deepcopy(self.data)
        if edit:
            edit(data)
        path = os.path.join(self.root, "config", "site.yaml")
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        os.chmod(path, 0o600)
        return load_site_config(path)

    def check_config(self, cfg, mail_enabled=True):
        path = wf.write_workers_file(cfg, mail_enabled=mail_enabled)
        done = subprocess.run([sys.executable, SUPERVISOR, "check-config", "-c", path, "--site-config", cfg.source_path],
                              capture_output=True, text=True, timeout=60, cwd=os.path.dirname(SUPERVISOR))
        return done.returncode, done.stdout + done.stderr

    def ids(self, data):
        return [w["id"] for w in data["workers"]]

    def test_two_basler_cameras_pass_check_config(self):            # S2 done when
        cfg = self.config()
        code, out = self.check_config(cfg)
        self.assertEqual(code, 0, out)
        self.assertIn("OK - 9 enabled worker(s)", out)
        data = wf.generate_workers(cfg)
        self.assertEqual(self.ids(data), ["frame_capture_cam1", "frame_capture_cam2", "inspection_left",
                                          "inspection_right", "cleanup", "mail", "plc", "ui", "transfer"])
        cam2 = data["workers"][1]
        self.assertEqual((cam2["command"], cam2["health"]["path"]),
                         ("python3 FrameCapture_Basler.py", f"{self.root}/data/frames/CAM2/live/IMG_1.jpg"))
        self.assertEqual(data["workers"][3]["follows"], "frame_capture_cam2")
        self.assertEqual((data["runtime"]["root"], data["log_dir"]), (f"{self.root}/run", f"{self.root}/logs"))
        with open(os.path.join(self.root, "config", "workers.yaml"), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("CHANGE_ME", text)                      # no secret from site.yaml

    def test_flir_camera_and_one_side(self):
        def edit(d):
            d["capture"]["cameras"] = [dict(d["capture"]["cameras"][1], driver="flir",
                                            flir={"interface": "GEV Interface 1", "throughput_bps": 1})]
            del d["inspection"]["sides"]["LEFT"]
        cfg = self.config(edit)
        data = wf.generate_workers(cfg)
        self.assertEqual(self.ids(data)[:2], ["frame_capture_cam2", "inspection_right"])
        self.assertEqual((data["workers"][0]["command"], data["workers"][0]["working_directory"]),
                         ("python3 FrameCapture_Flir.py", f"{self.root}/code/workers/frame_capture_flir"))   # F86
        code, out = self.check_config(cfg)
        self.assertEqual(code, 0, out)

    def test_mixed_site_basler_and_flir(self):                      # F100 done when (Hirmi: one of each)
        def edit(d):
            d["capture"]["cameras"][1].update(driver="flir",
                                              flir={"interface": "GEV Interface 1", "throughput_bps": 19662239})
        cfg = self.config(edit)
        data = wf.generate_workers(cfg)
        captures = {w["id"]: (w["command"], w["working_directory"]) for w in data["workers"] if w["type"] == "frame_capture"}
        self.assertEqual(captures, {
            "frame_capture_cam1": ("python3 FrameCapture_Basler.py", f"{self.root}/code/workers/frame_capture_basler"),
            "frame_capture_cam2": ("python3 FrameCapture_Flir.py", f"{self.root}/code/workers/frame_capture_flir")})
        follows = {w["id"]: w["follows"] for w in data["workers"] if w["type"] == "inspection"}
        self.assertEqual(follows, {"inspection_left": "frame_capture_cam1", "inspection_right": "frame_capture_cam2"})
        cam2 = next(w for w in data["workers"] if w["id"] == "frame_capture_cam2")
        self.assertEqual(cam2["health"]["path"], f"{self.root}/data/frames/CAM2/live/IMG_1.jpg")   # same outputs
        code, out = self.check_config(cfg)
        self.assertEqual(code, 0, out)
        self.assertIn("OK - 9 enabled worker(s) in start order: frame_capture_cam1, frame_capture_cam2", out)

    def test_no_legacy_capture_program(self):
        self.assertNotIn("frame_capture", [folder for folder, _ in wf.CAPTURE_PROGRAM.values()])

    def test_mail_disabled_and_no_transfer(self):
        cfg = self.config(lambda d: d.pop("transfer"))
        data = wf.generate_workers(cfg, mail_enabled=False)
        mail = next(w for w in data["workers"] if w["id"] == "mail")
        self.assertFalse(mail["enabled"])
        self.assertNotIn("transfer", self.ids(data))
        code, out = self.check_config(cfg, mail_enabled=False)
        self.assertEqual(code, 0, out)
        self.assertIn("OK - 7 enabled worker(s)", out)

    def test_site_without_mail_section_gets_mail_disabled(self):
        cfg = self.config(lambda d: d.pop("mail"))
        mail = next(w for w in wf.generate_workers(cfg)["workers"] if w["id"] == "mail")
        self.assertFalse(mail["enabled"])
        code, out = self.check_config(cfg)
        self.assertEqual(code, 0, out)

    def test_previous_file_kept(self):
        cfg = self.config()
        path = wf.write_workers_file(cfg)
        wf.write_workers_file(cfg, mail_enabled=False)
        self.assertTrue(os.path.isfile(path + ".bak"))


if __name__ == "__main__":
    unittest.main()
