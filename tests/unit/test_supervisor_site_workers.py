"""Tests for STATUS item 4 (first real workers) and follow-up 12 (service template).

Run from the repo root:  python3 -m unittest tests.unit.test_supervisor_site_workers -v

What the site test at Tadipatri checks with real cameras (STATUS 4 a-d) is tested here with
the dummy worker and the SAME monitoring and restart numbers as
supervisor/config/workers.basler-2cam.yaml, so the timing claims in that file are checked:
    kill -9 -> restarted within 10 s, with a worker_restarted event          (4b)
    a capture that stops producing frames -> restarted, retries not used up  (4c, short version)
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import yaml

from tests.unit.test_supervisor_v2 import DUMMY, SUPERVISOR_DIR, SupervisorTest   # noqa: F401 (sets sys.path)
from core.config import ConfigurationManager, _expand_home                       # noqa: E402
from core.process_manager import WorkerStatus                                   # noqa: E402

SITE_FILE = os.path.join(SUPERVISOR_DIR, "config", "workers.basler-2cam.yaml")
TEMPLATE = os.path.join(SUPERVISOR_DIR, "dpc-supervisor.service")
INSTALLER = os.path.join(SUPERVISOR_DIR, "install_service.sh")


class EnvHomeExpansion(unittest.TestCase):
    def test_only_a_leading_tilde_is_expanded(self):
        home = os.path.expanduser("~")
        self.assertEqual(_expand_home("~/INSIGHTZZ/data/frames/CAM1/INF"),
                         os.path.join(home, "INSIGHTZZ/data/frames/CAM1/INF"))
        self.assertEqual(_expand_home("~"), home)
        for unchanged in ("~other/x", "a~/b", "plain", "", "$HOME/x", "100~/s"):
            self.assertEqual(_expand_home(unchanged), unchanged)


class TwoCameraFile(unittest.TestCase):
    def setUp(self):
        self.manager = ConfigurationManager(SITE_FILE, SUPERVISOR_DIR)
        self.manager.load()
        self.workers = {w.id: w for w in self.manager.workers}
        self.home = os.path.expanduser("~")

    def test_worker_ids_from_d033_and_order(self):
        self.assertEqual([w.id for w in self.manager.start_order],
                         ["frame_capture_cam1", "frame_capture_cam2", "inspection_left", "inspection_right"])

    def test_each_camera_has_its_own_live_file_and_no_xml(self):
        for n in (1, 2):
            w = self.workers[f"frame_capture_cam{n}"]
            self.assertEqual(w.env, {}, "F20: settings come from DPC_WORKER_CONFIG, not an XML")
            self.assertEqual(w.health.type, "file_activity")
            self.assertEqual(w.health.path, [os.path.join(self.home, f"INSIGHTZZ/data/frames/CAM{n}/live/IMG_1.jpg")])
            self.assertEqual(w.working_directory, os.path.join(self.home, "INSIGHTZZ/code/workers/frame_capture_basler"))
            self.assertEqual((w.mode, w.restart_cost, w.stop_signal), ("service", "normal", "SIGTERM"))

    def test_layout_paths_and_no_site_values(self):
        s = self.manager.settings
        self.assertEqual(s.log_dir, os.path.join(self.home, "INSIGHTZZ/logs"))
        self.assertEqual(s.runtime_root, os.path.join(self.home, "INSIGHTZZ/run"))
        with open(SITE_FILE) as f:
            text = f.read()
        for forbidden in ("/home/", "tadipatri4", "/media/", "password:", "username:", "XML", "xml",
                          "BASLER_CONFIG", "LeftAlgorithm", "left_algorithm"):
            self.assertNotIn(forbidden, text)
        self.assertNotRegex(text, r"\b2482\d{4}\b", "camera serials belong in site.yaml, not here")

    def test_numbers_meet_the_site_targets(self):
        m = self.manager.settings.monitoring
        for w in (self.workers["frame_capture_cam1"], self.workers["frame_capture_cam2"]):
            worst_restart = m.check_interval_seconds + w.restart.retry_delay_seconds + m.check_interval_seconds
            self.assertLess(worst_restart, 10, "STATUS 4b: kill -9 -> restart within 10 s")
            self.assertGreater(w.health.max_age_seconds, 30, "the capture's own reopen (~30 s) goes first")
            self.assertGreaterEqual(w.restart.max_retries, 20, "a long cable pull must not end in FAILED quickly")
            self.assertLess(w.stop_timeout_seconds * 2, 90, "both stops fit in TimeoutStopSec")


class InspectionWorkersF26(unittest.TestCase):
    """STATUS F26: both sides exactly as listed there."""

    def setUp(self):
        manager = ConfigurationManager(SITE_FILE, SUPERVISOR_DIR)
        manager.load()
        self.workers = {w.id: w for w in manager.workers}
        self.home = os.path.expanduser("~")

    def test_both_sides(self):
        for side, cam in (("left", 1), ("right", 2)):
            w = self.workers[f"inspection_{side}"]
            self.assertEqual(w.command, "python3 inspection.py")
            self.assertEqual(w.working_directory, os.path.join(self.home, "INSIGHTZZ/code/workers/inspection"))
            self.assertEqual((w.restart_cost, w.follows, w.mode), ("high", f"frame_capture_cam{cam}", "service"))
            self.assertEqual(w.stop_timeout_seconds, 20)
            h = w.health
            self.assertEqual(h.type, "queue_lag")
            self.assertEqual(h.path, [os.path.join(self.home, f"INSIGHTZZ/data/frames/CAM{cam}/INF")])
            self.assertEqual((h.max_oldest_age_seconds, h.require_heartbeat, h.max_age_seconds,
                              h.startup_grace_seconds), (120, False, 60, 180))
            self.assertEqual(w.env, {})


class SiteNumbersWithDummyCaptures(SupervisorTest):
    """The real file's monitoring + restart numbers, with dummies that touch a live file."""

    def site_like(self, args, max_age=None):
        with open(SITE_FILE) as f:
            data = yaml.safe_load(f)
        workers = []
        captures = [w for w in data["workers"] if w["id"].startswith("frame_capture")]
        for n, w in enumerate(captures, start=1):
            live = self.path(f"live{n}.jpg")
            w = dict(w, command=f"{DUMMY} --interval 0.25 --heartbeat-file {live} {args}",
                     working_directory=self.tmp, env={})
            w["health"] = dict(w["health"], path=live)
            if max_age is not None:
                w["health"].update(max_age_seconds=max_age, startup_grace_seconds=1)
            workers.append(w)
        return data["monitoring"], workers

    def build_site_like(self, args="", max_age=None, worker_timeout=None):
        monitoring, workers = self.site_like(args, max_age)
        if worker_timeout is not None:
            monitoring = dict(monitoring, worker_timeout_seconds=worker_timeout)
        return self.build(workers, monitoring)

    def test_kill_9_restarts_within_10_seconds_with_an_event(self):
        self.build_site_like()
        self.sup.start()
        self.run_until(lambda: all(w.status == WorkerStatus.RUNNING for w in self.sup.workers.values()),
                       message="captures not running")
        cam1 = self.worker("frame_capture_cam1")
        old_pid = cam1.pid
        os.kill(old_pid, signal.SIGKILL)
        killed_at = time.time()
        self.run_until(lambda: "worker_restarted" in self.mqtt.event_names("frame_capture_cam1"), timeout=12,
                       message="no restart")
        self.assertLess(time.time() - killed_at, 10)
        self.assertNotEqual(cam1.pid, old_pid)
        self.assertNotIn("worker_failure", self.mqtt.event_names("frame_capture_cam2"), "the other camera is untouched")

    def test_a_capture_without_frames_is_restarted_and_recovers_without_using_up_retries(self):
        # --hang-after: alive, but the live file stops changing (the cable-pull picture, shortened)
        self.build_site_like("--hang-after 1", max_age=1, worker_timeout=1)
        self.sup.start()
        self.run_until(lambda: "worker_restarted" in self.mqtt.event_names("frame_capture_cam1"), timeout=15,
                       message="stale live file did not lead to a restart")
        cam1 = self.worker("frame_capture_cam1")
        self.assertNotEqual(cam1.status, WorkerStatus.FAILED)
        self.assertLess(cam1.retry_count, cam1.config.restart.max_retries)
        failure = [d for w, e, d in self.mqtt.events if w == "frame_capture_cam1" and e == "worker_failure"][0]
        self.assertIn("not updated", failure["reason"])


@unittest.skipIf(os.geteuid() == 0, "install_service.sh refuses root; run the tests as a normal user")
class ServiceTemplate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "INSIGHTZZ")
        os.makedirs(os.path.join(self.root, "config"))
        repo = os.path.dirname(SUPERVISOR_DIR)
        os.symlink(repo, os.path.join(self.root, "code"))
        shutil.copy(SITE_FILE, os.path.join(self.root, "config", "workers.yaml"))

    def tearDown(self):
        self._tmp.cleanup()

    def dry_run(self, *extra):
        return subprocess.run(["bash", INSTALLER, "--root", self.root, "--python", sys.executable, "--dry-run", *extra],
                              capture_output=True, text=True)

    def test_dry_run_fills_every_placeholder(self):
        result = self.dry_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        unit = result.stdout
        self.assertTrue(unit.startswith("#"), "dry-run stdout must be the unit only (it can be piped to a file)")
        self.assertNotRegex(unit, r"@[A-Z_]+@")
        root = os.path.realpath(self.root)
        self.assertIn(f"User={os.environ.get('USER') or subprocess.check_output(['id', '-un'], text=True).strip()}", unit)
        self.assertIn(f"WorkingDirectory={root}/code/supervisor", unit)
        self.assertIn(f"ExecStart={sys.executable} supervisor.py run -c {root}/config/workers.yaml", unit)
        self.assertIn(f"Environment=PATH={os.path.dirname(sys.executable)}:", unit)
        self.assertIn("KillMode=mixed", unit)

    def test_missing_workers_file_is_refused(self):
        os.remove(os.path.join(self.root, "config", "workers.yaml"))
        result = self.dry_run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("workers.yaml missing", result.stderr)


class TemplateFile(unittest.TestCase):
    def test_template_has_placeholders_and_no_site_values(self):
        with open(TEMPLATE) as f:
            text = f.read()
        for placeholder in ("@USER@", "@ROOT@", "@PYTHON@", "@PYTHON_DIR@"):
            self.assertIn(placeholder, text)
        for forbidden in ("dpc_tadipatri", "/home/", "INSIGHTZZ/CODE"):
            self.assertNotIn(forbidden, text)
        # the Supervisor must be able to stop all workers before systemd kills them
        timeout = int(text.split("TimeoutStopSec=")[1].split()[0])
        self.assertGreater(timeout, 2 * 15 + 10)


if __name__ == "__main__":
    unittest.main()
