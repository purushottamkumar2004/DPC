"""Tests for FrameCapture_Basler.py (STATUS P6): config from the rendered JSON (CONTRACTS 13),
camera recovery, frame hand-off, health. A fake pypylon stands in for the camera.

Run from the repo root:
    python3 -m unittest discover -s workers/frame_capture_basler/tests -t . -v
"""
import copy
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest

import cv2
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(os.path.dirname(WORKER_DIR))
sys.path.insert(0, os.path.join(HERE, "fake_pypylon"))      # before any real pypylon
sys.path.insert(0, WORKER_DIR)
sys.path.insert(0, REPO_ROOT)

import FrameCapture_Basler as F            # noqa: E402
from pypylon import pylon                  # noqa: E402  (the fake)
from pymysql.constants import CLIENT       # noqa: E402

REFERENCE_SITE = os.path.join(REPO_ROOT, "docs", "sites", "tadipatri.site.reference.yaml")


def capture_json(root, camera_id="CAM1", **camera_overrides):
    """A capture file shaped as CONTRACTS 13 describes it."""
    frames = os.path.join(root, "data", "frames", camera_id)
    camera = {"id": camera_id, "side": "LEFT", "driver": "basler", "serial": "24824427",
              "fps": 4.0, "exposure_us": 1500, "rotate": 0, "ip": None}
    camera.update(camera_overrides)
    return {
        "_generated": {"from": "site.yaml", "sha256": "0" * 64, "at": "2026-10-25T10:00:00", "renderer": 1},
        "worker_id": "frame_capture_cam1",
        "paths": {"root": root, "logs": os.path.join(root, "logs"),
                  "live": f"{frames}/live", "tmp": f"{frames}/_tmp", "inf": f"{frames}/INF",
                  "ui": f"{frames}/UI", "raw_images": f"{frames}/RAW_IMAGES"},
        "log": {"backup_count": 5, "file_name": f"FrameCapture_{camera_id}.log"},
        "camera": camera,
        "db": {"host": "localhost", "port": 3306, "name": "dpc", "user": "u", "password": "secret-pw",
               "health_table": "tadipatri4_health_status_table"},
    }


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "INSIGHTZZ")
        os.makedirs(os.path.join(self.root, "logs"))
        self.path = os.path.join(self._tmp.name, "frame_capture_cam1.json")

    def tearDown(self):
        for handler in list(F.logger.handlers):
            handler.close()
            F.logger.removeHandler(handler)
        F.cfg.clear()
        self._tmp.cleanup()

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return self.path


class LoadConfig(Base):
    def test_all_keys_mapped(self):
        c = F.load_worker_config(self.write(capture_json(self.root)))
        self.assertEqual((c["serial"], c["fps"], c["exposure_us"], c["rotate_deg"]), ("24824427", 4.0, 1500.0, 0))
        self.assertEqual(c["tmp_dir"], os.path.join(self.root, "data/frames/CAM1/_tmp"))
        self.assertEqual((c["log_file_name"], c["log_backup_count"]), ("FrameCapture_CAM1.log", 5))
        self.assertEqual((c["health_table"], c["db_port"], c["db_password"]),
                         ("tadipatri4_health_status_table", 3306, "secret-pw"))
        self.assertEqual(c["health_interval_s"], 600)                      # default when absent

    def test_health_interval_from_json(self):
        data = capture_json(self.root)
        data["health_interval_s"] = 120
        self.assertEqual(F.load_worker_config(self.write(data))["health_interval_s"], 120)

    def test_bad_values_name_the_key(self):
        cases = [
            (lambda d: d["camera"].update(rotate=45), "camera.rotate"),
            (lambda d: d["camera"].update(driver="flir"), "camera.driver"),
            (lambda d: d["camera"].update(fps=0), "camera.fps"),
            (lambda d: d["camera"].update(serial=" "), "camera.serial"),
            (lambda d: d["db"].update(health_table="x; DROP"), "db.health_table"),
            (lambda d: d["log"].update(file_name="../a.log"), "log.file_name"),
            (lambda d: d.update(health_interval_s=0), "health_interval_s"),
            (lambda d: d.update(health_interval_s="600"), "health_interval_s"),
            (lambda d: d["camera"].pop("exposure_us"), "camera.exposure_us"),
            (lambda d: d.pop("db"), "'db'"),
        ]
        for change, key in cases:
            data = capture_json(self.root)
            change(data)
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as ctx:
                    F.load_worker_config(self.write(data))
                self.assertIn(key, str(ctx.exception))
                self.assertNotIn("secret-pw", str(ctx.exception))

    def test_reads_what_the_renderer_writes(self):
        """The real renderer (common/config/render.py) on the Tadipatri reference site.yaml."""
        from common.config import render as rd
        with open(REFERENCE_SITE, encoding="utf-8") as f:
            site = yaml.safe_load(f)
        site["paths"]["root"] = self.root
        site["db"]["password"] = "secret-pw"
        site["graph"] = {"tenant_id": "t", "client_id": "c", "client_secret": "s"}
        site_path = os.path.join(self._tmp.name, "site.yaml")
        with open(site_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(site, f, sort_keys=False)
        os.chmod(site_path, 0o600)
        rd.render(site_path)
        rendered = os.path.join(self.root, "config", "rendered")
        cam1 = F.load_worker_config(os.path.join(rendered, "frame_capture_cam1.json"))
        cam2 = F.load_worker_config(os.path.join(rendered, "frame_capture_cam2.json"))
        self.assertEqual((cam1["serial"], cam1["side"], cam1["log_file_name"]), ("24824427", "LEFT", "FrameCapture_CAM1.log"))
        self.assertEqual((cam2["serial"], cam2["exposure_us"], cam2["rotate_deg"]), ("24874775", 2000.0, 0))
        self.assertEqual(cam2["inf_dir"], os.path.join(self.root, "data", "frames", "CAM2", "INF"))


class Main(Base):
    def run_main(self, env_value):
        old = os.environ.pop(F.CONFIG_ENV, None)
        if env_value is not None:
            os.environ[F.CONFIG_ENV] = env_value
        try:
            with self.assertRaises(SystemExit) as ctx:
                F.main()
            return ctx.exception.code
        finally:
            os.environ.pop(F.CONFIG_ENV, None)
            if old is not None:
                os.environ[F.CONFIG_ENV] = old

    def test_env_missing_or_file_bad_exits_2(self):
        self.assertEqual(self.run_main(None), 2)
        self.assertEqual(self.run_main(os.path.join(self._tmp.name, "nope.json")), 2)
        data = capture_json(self.root)
        data["camera"]["rotate"] = 45
        self.assertEqual(self.run_main(self.write(data)), 2)


class Run(Base):
    """Frames, a timeout, a failed grab, a pulled cable, two failed reopens, frames, stop."""

    def start(self, data):
        F.cfg.clear()
        F.cfg.update(F.load_worker_config(self.write(data)))
        F._stop_requested = False
        F._last_logged.clear()
        F.initializeLogger()
        F.prepare_directories()
        self.health = []
        F.update_health_status = lambda h, s: self.health.append(h)
        self.sleeps = 0

        def fake_sleep(seconds):
            self.sleeps += 1
            if self.sleeps == 3:                      # cable back after two failed reopen attempts
                pylon.STATE["available"] = True
        F.sleep_unless_stopped = fake_sleep

    def setUp(self):
        super().setUp()
        self._saved = (F.update_health_status, F.sleep_unless_stopped)

    def tearDown(self):
        F.update_health_status, F.sleep_unless_stopped = self._saved
        super().tearDown()

    def run_script(self, data):
        self.start(data)
        pylon.reset(["frame"] * 6 + ["timeout", "failed"] + ["frame"] * 2 + ["remove"] + ["frame"] * 5)
        pylon.STATE["on_empty"] = lambda: setattr(F, "_stop_requested", True)
        F.BaslerCam().run()
        frames = os.path.join(self.root, "data", "frames", data["camera"]["id"])
        inf = sorted(os.listdir(os.path.join(frames, "INF")))
        hashes = []
        for name in inf:
            with open(os.path.join(frames, "INF", name), "rb") as f:
                hashes.append(hashlib.md5(f.read()).hexdigest())
        with open(os.path.join(self.root, "logs", data["log"]["file_name"])) as f:
            log = f.read()
        return frames, inf, hashes, log

    def test_recovery_handoff_and_health(self):
        frames, inf, hashes, log = self.run_script(capture_json(self.root))
        self.assertEqual(len(inf), 13)                                       # 6 + 2 + 5
        self.assertFalse(any(a == b for a, b in zip(hashes, hashes[1:])))   # never republished
        self.assertEqual(os.listdir(os.path.join(frames, "_tmp")), [])      # clean stop
        self.assertEqual(cv2.imread(os.path.join(frames, "live", "IMG_1.jpg")).shape, (1080, 1920, 3))
        self.assertIn("LOST", log)
        self.assertIn("REOPENED", log)
        self.assertEqual(pylon.STATE["opens"], 2)
        self.assertEqual(self.health, ["OK", "NOK", "OK"])
        self.assertNotIn("secret-pw", log)

    def test_settings_come_from_the_json(self):
        """What the CAM2 exposure check on site proves: the JSON value reaches the camera."""
        data = capture_json(self.root, camera_id="CAM2", exposure_us=2750, fps=5.0, rotate=90)
        frames, inf, _, log = self.run_script(data)
        self.assertEqual(pylon.STATE["settings"]["ExposureTimeAbs"], 2750.0)
        self.assertEqual(pylon.STATE["settings"]["AcquisitionFrameRateAbs"], 5.0)
        self.assertIn("Camera setting ExposureTimeAbs = 2750.0", log)
        self.assertEqual(cv2.imread(os.path.join(frames, "INF", inf[0])).shape, (1920, 1080, 3))


class Health(Base):
    def setUp(self):
        super().setUp()
        F.cfg.update(F.load_worker_config(self.write(capture_json(self.root))))
        self._connect = F.pymysql.connect

    def tearDown(self):
        F.pymysql.connect = self._connect
        super().tearDown()

    def test_found_rows_flag_and_port(self):                                # F7
        seen = {}

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, args):
                seen["sql"], seen["args"] = sql, args
                return 1

        class Conn:
            def cursor(self): return Cursor()
            def commit(self): pass
            def close(self): pass

        def connect(**kwargs):
            seen["kwargs"] = kwargs
            return Conn()

        F.pymysql.connect = connect
        F.update_health_status("OK", 0)
        self.assertTrue(seen["kwargs"]["client_flag"] & CLIENT.FOUND_ROWS)
        self.assertEqual(seen["kwargs"]["port"], 3306)
        self.assertIn("`tadipatri4_health_status_table`", seen["sql"])
        self.assertEqual(seen["args"], ("OK", 0, "24824427"))

    def test_interval_from_config(self):
        F.cfg["health_interval_s"] = 1
        calls = []
        saved = F.update_health_status
        F.update_health_status = lambda h, s: calls.append(h)
        try:
            r = F.HealthReporter()
            r.report("OK"); r.report("OK"); time.sleep(1.1); r.report("OK"); r.report("NOK")
        finally:
            F.update_health_status = saved
        self.assertEqual(calls, ["OK", "OK", "NOK"])


if __name__ == "__main__":
    unittest.main()
