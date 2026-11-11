"""Tests for camera_probe.py and flir_probe.py (STATUS S4).

Basler: a fake pylon (tests/fake_pypylon); FLIR: a fake PySpin (tests/fake_pyspin).
One class uses the real pylon SDK with its camera emulator (PYLON_CAMEMU) when pypylon is
installed, else it is skipped.

Run from the repo root:
    python3 -m unittest discover -s workers/frame_capture_basler/tests -t . -v
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "fake_pyspin"))
sys.path.insert(0, os.path.join(HERE, "fake_pypylon"))
sys.path.insert(0, WORKER_DIR)

import camera_probe as probe                 # noqa: E402
import flir_probe                            # noqa: E402
import PySpin                                # noqa: E402  (the fake)
from pypylon import pylon                    # noqa: E402  (the fake)

IN_USE_ERROR = ("Failed to open 'Basler acA1280-60gc#0030534A6217#192.168.1.103:3956'. The device is "
                "controlled by another application. Err: An attempt was made to access an address "
                "location which is currently/momentary not accessible. (0xE1018006)")
LEFT = {"serial": "24824427", "model": "acA1920-40gc", "ip": "192.168.1.102", "class": "BaslerGigE"}
RIGHT = {"serial": "24874775", "model": "acA1920-40gc", "ip": "192.168.1.103", "class": "BaslerGigE"}
USB = {"serial": "40000001", "model": "a2A1920-51gcBAS", "class": "BaslerUsb"}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self._tmp.name, "probe.jpg")
        self._saved = (probe._load_pylon, flir_probe._load_pyspin)
        PySpin.setup([])                        # no FLIR cameras unless a test adds them

    def tearDown(self):
        probe._load_pylon, flir_probe._load_pyspin = self._saved
        self._tmp.cleanup()

    def files(self):
        return sorted(os.listdir(self._tmp.name))


class BaslerList(Base):
    def test_lists_serial_model_ip_and_in_use(self):
        pylon.set_devices([LEFT, dict(RIGHT, in_use=True), USB])
        cams = {c["serial"]: c for c in probe.list_cameras()["cameras"]}
        self.assertEqual((cams["24824427"]["ip"], cams["24824427"]["in_use"]), ("192.168.1.102", False))
        self.assertTrue(cams["24874775"]["in_use"])
        self.assertEqual((cams["40000001"]["ip"], cams["40000001"]["device_class"]), (None, "BaslerUsb"))
        self.assertEqual(cams["24824427"]["driver"], "basler")

    def test_no_cameras(self):
        pylon.set_devices([])
        self.assertEqual(probe.list_cameras(), {"driver": "basler", "sdk_installed": True,
                                                "message": None, "cameras": []})

    def test_sdk_missing_is_reported_not_raised(self):
        probe._load_pylon = lambda: None
        result = probe.list_cameras()
        self.assertEqual((result["sdk_installed"], result["cameras"]), (False, []))
        self.assertIn("Basler SDK not installed", result["message"])


class BaslerGrab(Base):
    def setUp(self):
        super().setUp()
        pylon.set_devices([LEFT, dict(RIGHT, in_use=True)])

    def test_grab_writes_one_image_with_the_exposure(self):
        result = probe.grab_one("24824427", self.out, exposure_us=2500)
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["driver"], result["width"], result["height"], result["exposure_us"]),
                         ("basler", 1920, 1080, 2500.0))
        self.assertEqual(cv2.imread(self.out).shape, (1080, 1920, 3))
        self.assertEqual(pylon.STATE["settings"]["ExposureTimeAbs"], 2500.0)
        self.assertEqual(pylon.STATE["settings"]["TriggerMode"], "Off")
        self.assertEqual(pylon.STATE["closed"], 1)                     # camera released again
        self.assertEqual(self.files(), ["probe.jpg"])                  # no temporary file left

    def test_without_exposure_the_camera_keeps_its_own(self):
        result = probe.grab_one("24824427", self.out)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["exposure_us"])
        self.assertNotIn("ExposureTimeAbs", pylon.STATE["settings"])

    def test_in_use_is_said_clearly_and_camera_not_opened(self):
        result = probe.grab_one("24874775", self.out)
        self.assertEqual(result["status"], probe.IN_USE)
        self.assertIn("in use by another program", result["message"])
        self.assertEqual(pylon.STATE["opens"], 0)
        self.assertEqual(self.files(), [])

    def test_in_use_found_only_when_opening(self):
        pylon.set_devices([dict(LEFT, open_error=IN_USE_ERROR)])
        result = probe.grab_one("24824427", self.out)
        self.assertEqual(result["status"], probe.IN_USE)

    def test_other_open_error_is_failed_with_the_sdk_text(self):
        pylon.set_devices([dict(LEFT, open_error="The device is not reachable (wrong subnet)")])
        result = probe.grab_one("24824427", self.out)
        self.assertEqual(result["status"], probe.FAILED)
        self.assertIn("wrong subnet", result["message"])

    def test_failed_grab_writes_nothing_and_closes(self):
        pylon.set_devices([dict(LEFT, grab_ok=False)])
        result = probe.grab_one("24824427", self.out)
        self.assertEqual(result["status"], probe.FAILED)
        self.assertEqual(self.files(), [])
        self.assertEqual(pylon.STATE["closed"], 1)

    def test_not_found_names_both_drivers(self):
        flir_probe._load_pyspin = lambda: None
        result = probe.grab_one("11111111", self.out)
        self.assertEqual(result["status"], probe.NOT_FOUND)
        self.assertIn("basler", result["message"])
        self.assertIn("FLIR SDK not installed", result["message"])

    def test_bad_requests_never_touch_a_camera(self):
        cases = [("", self.out, {}), ("24824427", self.out[:-4] + ".txt", {}),
                 ("24824427", os.path.join(self._tmp.name, "missing", "a.jpg"), {}),
                 ("24824427", self.out, {"exposure_us": 0}), ("24824427", self.out, {"exposure_us": "abc"}),
                 ("24824427", self.out, {"timeout_ms": 0}), ("24824427", self.out, {"driver": "sony"})]
        for serial, out, kwargs in cases:
            with self.subTest(serial=serial, out=out, kwargs=kwargs):
                self.assertEqual(probe.grab_one(serial, out, **kwargs)["status"], probe.BAD_REQUEST)
        self.assertEqual(pylon.STATE["opens"], 0)


class Flir(Base):
    def setUp(self):
        super().setUp()
        PySpin.setup([{"serial": "21012345", "model": "Blackfly S BFS-PGE-31S4C", "ip": 0xC0A8016E},
                      {"serial": "21012346", "model": "Blackfly S BFS-PGE-31S4C", "ip": 0xC0A8016F,
                       "access": "OpenReadWrite"}])

    def test_list(self):
        cams = {c["serial"]: c for c in flir_probe.list_cameras()["cameras"]}
        self.assertEqual((cams["21012345"]["ip"], cams["21012345"]["in_use"]), ("192.168.1.110", False))
        self.assertTrue(cams["21012346"]["in_use"])
        self.assertEqual(PySpin.LOG[-2:], ["list cleared", "system released"])

    def test_list_all_merges_both_drivers(self):
        pylon.set_devices([LEFT])
        result = probe.list_all()
        self.assertEqual(sorted(c["driver"] for c in result["cameras"]), ["basler", "flir", "flir"])
        self.assertEqual([d["driver"] for d in result["drivers"]], ["basler", "flir"])

    def test_grab_auto_finds_the_flir_camera(self):
        pylon.set_devices([LEFT])
        result = probe.grab_one("21012345", self.out, exposure_us=99999999)     # clamped to the camera max
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["driver"], result["width"], result["exposure_us"]), ("flir", 1440, 30000.0))
        self.assertEqual(cv2.imread(self.out).shape, (1080, 1440, 3))
        self.assertIn("set ExposureAuto=Off", PySpin.LOG)
        for done in ("end", "deinit", "list cleared", "system released"):
            self.assertIn(done, PySpin.LOG)

    def test_in_use_by_access_status(self):
        result = flir_probe.grab_one("21012346", self.out)
        self.assertEqual(result["status"], probe.IN_USE)
        self.assertNotIn("init 21012346", PySpin.LOG)

    def test_in_use_found_at_init(self):
        PySpin.setup([{"serial": "21012345", "init_error": "Spinnaker: Access Denied [-1005]"}])
        self.assertEqual(flir_probe.grab_one("21012345", self.out)["status"], probe.IN_USE)
        self.assertIn("system released", PySpin.LOG)

    def test_incomplete_image(self):
        PySpin.setup([{"serial": "21012345", "incomplete": True}])
        result = flir_probe.grab_one("21012345", self.out)
        self.assertEqual(result["status"], probe.FAILED)
        self.assertEqual(self.files(), [])
        self.assertIn("deinit", PySpin.LOG)

    def test_sdk_missing(self):
        flir_probe._load_pyspin = lambda: None
        self.assertEqual(flir_probe.list_cameras()["message"], "FLIR SDK not installed (Spinnaker + PySpin)")
        self.assertEqual(probe.grab_one("21012345", self.out, driver="flir")["status"], probe.SDK_MISSING)


class Cli(Base):
    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = probe.main(list(args))
        return code, out.getvalue()

    def test_list_json(self):
        pylon.set_devices([LEFT, RIGHT])
        flir_probe._load_pyspin = lambda: None
        code, text = self.run_cli("list", "--json")
        result = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual([c["serial"] for c in result["cameras"]], ["24824427", "24874775"])
        self.assertIn("FLIR SDK not installed", result["drivers"][1]["message"])

    def test_list_text_says_in_use(self):
        pylon.set_devices([dict(RIGHT, in_use=True)])
        code, text = self.run_cli("list", "--driver", "basler")
        self.assertIn("IN USE", text)

    def test_grab_exit_codes(self):
        pylon.set_devices([LEFT, dict(RIGHT, in_use=True)])
        flir_probe._load_pyspin = lambda: None
        code, text = self.run_cli("grab", "24824427", self.out, "--exposure", "1800", "--json")
        self.assertEqual((code, json.loads(text)["exposure_us"]), (0, 1800.0))
        self.assertEqual(self.run_cli("grab", "24874775", self.out)[0], 3)
        self.assertEqual(self.run_cli("grab", "123", self.out)[0], 4)
        self.assertEqual(self.run_cli("grab", "24824427", self.out + ".gif")[0], 2)


def _real_pylon_available():
    try:
        return subprocess.run([sys.executable, "-c", "import pypylon.pylon"], capture_output=True,
                              timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(_real_pylon_available(), "real pypylon not installed")
class RealPylonEmulator(unittest.TestCase):
    """The real SDK end to end, with pylon's emulated cameras (no hardware needed)."""

    def run_probe(self, *args):
        env = dict(os.environ, PYLON_CAMEMU="1")
        done = subprocess.run([sys.executable, os.path.join(WORKER_DIR, "camera_probe.py"), *args],
                              capture_output=True, text=True, env=env, cwd=WORKER_DIR, timeout=120)
        return done.returncode, done.stdout

    def test_list_and_grab(self):
        code, text = self.run_probe("list", "--driver", "basler", "--json")
        cameras = json.loads(text)["cameras"]
        self.assertEqual(code, 0)
        self.assertEqual(len(cameras), 1)
        with tempfile.TemporaryDirectory() as folder:
            out = os.path.join(folder, "emu.png")
            code, text = self.run_probe("grab", cameras[0]["serial"], out, "--exposure", "3000", "--json")
            result = json.loads(text)
            self.assertEqual(code, 0, result)
            self.assertEqual(cv2.imread(out).shape[:2], (result["height"], result["width"]))


if __name__ == "__main__":
    unittest.main()
