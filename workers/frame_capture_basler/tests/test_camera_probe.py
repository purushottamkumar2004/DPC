"""Tests for camera_probe.py and flir_probe.py (STATUS S4; burst F89).

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
from datetime import datetime, timedelta

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

    def test_burst_cli(self):
        pylon.set_devices([LEFT, dict(RIGHT, in_use=True)])
        folder = os.path.join(self._tmp.name, "b")
        code, text = self.run_cli("burst", "24824427", folder, "--count", "3", "--interval-ms", "0", "--json")
        self.assertEqual((code, json.loads(text)["saved"]), (0, 3))
        code, text = self.run_cli("burst", "24874775", folder)
        self.assertEqual(code, 3)
        self.assertIn("in use", text)

    def test_grab_exit_codes(self):
        pylon.set_devices([LEFT, dict(RIGHT, in_use=True)])
        flir_probe._load_pyspin = lambda: None
        code, text = self.run_cli("grab", "24824427", self.out, "--exposure", "1800", "--json")
        self.assertEqual((code, json.loads(text)["exposure_us"]), (0, 1800.0))
        self.assertEqual(self.run_cli("grab", "24874775", self.out)[0], 3)
        self.assertEqual(self.run_cli("grab", "123", self.out)[0], 4)
        self.assertEqual(self.run_cli("grab", "24824427", self.out + ".gif")[0], 2)


class BaslerBurst(Base):
    def setUp(self):
        super().setUp()
        pylon.set_devices([LEFT, dict(RIGHT, in_use=True)])
        self.dir = os.path.join(self._tmp.name, "burst")          # created by the burst

    def names(self):
        return sorted(os.listdir(self.dir)) if os.path.isdir(self.dir) else []

    def test_frames_named_by_capture_time_in_order(self):
        result = probe.burst("24824427", self.dir, count=5, interval_ms=40, exposure_us=1800)
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["saved"], result["count"], result["interval_ms"]), (5, 5, 40))
        self.assertEqual((result["width"], result["height"], result["exposure_us"]), (1920, 1080, 1800.0))
        names = self.names()
        self.assertEqual(len(names), 5)
        self.assertEqual([os.path.basename(f["path"]) for f in result["frames"]], names)   # by name = by time
        times = [datetime.strptime(n[:-4], probe.FRAME_NAME_FORMAT) for n in names]
        self.assertEqual(times, sorted(set(times)))
        t_ms = [f["t_ms"] for f in result["frames"]]
        self.assertTrue(all(b - a >= 35 for a, b in zip(t_ms, t_ms[1:])), t_ms)          # spaced by the interval
        self.assertEqual(pylon.STATE["strategy"], pylon.GrabStrategy_LatestImageOnly)
        self.assertIs(pylon.STATE["settings"]["AcquisitionFrameRateEnable"], False)       # newest frame each tick
        self.assertEqual(pylon.STATE["closed"], 1)

    def test_interval_zero_and_existing_folder(self):
        os.makedirs(self.dir)
        result = probe.burst("24824427", self.dir, count=3, interval_ms=0)
        self.assertEqual((result["status"], len(self.names())), (probe.OK, 3))

    def test_in_use_like_grab(self):
        result = probe.burst("24874775", self.dir)
        self.assertEqual(result["status"], probe.IN_USE)
        self.assertIn("in use by another program", result["message"])
        self.assertEqual((pylon.STATE["opens"], self.names()), (0, []))
        pylon.set_devices([dict(LEFT, open_error=IN_USE_ERROR)])
        self.assertEqual(probe.burst("24824427", self.dir)["status"], probe.IN_USE)

    def test_failure_mid_burst_keeps_the_saved_frames(self):
        for key, words in (("fail_at", "incomplete"), ("timeout_at", "no frame within")):
            with self.subTest(key=key):
                pylon.set_devices([dict(LEFT, **{key: 3})])
                folder = os.path.join(self._tmp.name, key)
                result = probe.burst("24824427", folder, count=5, interval_ms=0)
                self.assertEqual((result["status"], result["saved"]), (probe.FAILED, 2))
                self.assertIn("frame 3 of 5", result["message"])
                self.assertIn(words, result["message"])
                self.assertEqual(len(os.listdir(folder)), 2)
                self.assertEqual(pylon.STATE["closed"], 1)

    def test_bad_requests_never_touch_a_camera(self):
        a_file = os.path.join(self._tmp.name, "file.jpg")
        open(a_file, "w").close()
        cases = [{"count": 0}, {"count": probe.MAX_BURST_COUNT + 1}, {"count": "x"}, {"interval_ms": -1},
                 {"out_dir": os.path.join(self._tmp.name, "no", "such")}, {"out_dir": a_file},
                 {"exposure_us": 0}, {"serial": ""}]
        for case in cases:
            args = {"serial": "24824427", "out_dir": self.dir, **case}
            with self.subTest(case=case):
                result = probe.burst(args.pop("serial"), args.pop("out_dir"), **args)
                self.assertEqual(result["status"], probe.BAD_REQUEST)
                self.assertEqual(result["frames"], [])
        self.assertEqual(pylon.STATE["opens"], 0)
        self.assertFalse(os.path.exists(os.path.join(self._tmp.name, "no")))

    def test_names_stay_in_order_when_the_clock_repeats(self):
        later = datetime.now() + timedelta(seconds=5)
        path, stamp = probe.frame_path(self.dir, later)
        self.assertEqual(stamp, later + timedelta(microseconds=1))
        self.assertTrue(os.path.basename(path).endswith(".jpg"))

    def test_measure_reads_a_burst(self):
        """The frames are what workers/inspection/measure.py reads: newest N .jpg by name, time from name."""
        inspection = os.path.join(os.path.dirname(WORKER_DIR), "inspection")
        sys.path.insert(0, inspection)
        try:
            from helpers import frame_time
            from model_selftest import newest_frames
        except Exception as exc:                       # inspection needs its own libraries
            self.skipTest(f"inspection helpers not importable here: {exc}")
        finally:
            sys.path.remove(inspection)
        result = probe.burst("24824427", self.dir, count=4, interval_ms=20)
        frames = newest_frames(self.dir, 4)
        self.assertEqual(frames, [f["path"] for f in result["frames"]])
        times = [frame_time(p) for p in frames]
        self.assertTrue(all(0 < (b - a).total_seconds() < 2.0 for a, b in zip(times, times[1:])))


class FlirBurst(Base):
    def test_burst(self):
        PySpin.setup([{"serial": "21012345", "model": "Blackfly S"}])
        folder = os.path.join(self._tmp.name, "flir")
        result = probe.burst("21012345", folder, count=3, interval_ms=0, driver="flir")
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["driver"], result["saved"], len(os.listdir(folder))), ("flir", 3, 3))
        self.assertIn("set StreamBufferHandlingMode=NewestOnly", PySpin.LOG)
        self.assertIn("set AcquisitionMode=Continuous", PySpin.LOG)

    def test_failure_mid_burst_and_clean_up(self):
        PySpin.setup([{"serial": "21012345", "incomplete_at": 2}])
        result = flir_probe.burst("21012345", self._tmp.name, count=4, interval_ms=0)
        self.assertEqual((result["status"], result["saved"]), (probe.FAILED, 1))
        for done in ("end", "deinit", "system released"):
            self.assertIn(done, PySpin.LOG)

    def test_in_use(self):
        PySpin.setup([{"serial": "21012346", "access": "Busy"}])
        self.assertEqual(probe.burst("21012346", self._tmp.name, driver="flir")["status"], probe.IN_USE)


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
            burst_dir = os.path.join(folder, "burst")
            code, text = self.run_probe("burst", cameras[0]["serial"], burst_dir, "--count", "4",
                                        "--interval-ms", "100", "--json")
            result = json.loads(text)
            self.assertEqual((code, result["saved"], len(os.listdir(burst_dir))), (0, 4, 4))


if __name__ == "__main__":
    unittest.main()
