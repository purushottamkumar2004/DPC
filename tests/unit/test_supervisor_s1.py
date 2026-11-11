"""Tests for STATUS S1: setup mode (D-103).

Run from the repo root:  python3 -m unittest tests.unit.test_supervisor_s1 -v

End to end with a real `supervisor.py run` on a fresh temporary <root>: the built-in setup list
starts <root>/code/ui/run_ui.py, which here is a fake UI that logs each start (pid and
DPC_WORKER_CONFIG). Real site.yaml (from the example), real renderer, real re-exec.
"""

import json
import os
import signal
import subprocess
import sys
import time
import unittest

import yaml

from tests.unit.test_supervisor_v2 import DUMMY, EXAMPLE_SITE_YAML, SUPERVISOR_DIR, SupervisorTest, dummy  # sys.path
from core import setup_mode                                                  # noqa: E402
from core.setup_mode import Paths                                            # noqa: E402
from core.site_env import SiteEnv                                            # noqa: E402

FAKE_UI = '''
import os, sys, time
with open({starts!r}, "a") as f:
    f.write("ui %d %s\\n" % (os.getpid(), os.environ.get("DPC_WORKER_CONFIG", "-")))
while True:
    time.sleep(0.2)
'''


class SetupModeTest(SupervisorTest):
    def setUp(self):
        super().setUp()
        self.root = self.path("INSIGHTZZ")
        self.config_dir = os.path.join(self.root, "config")
        self.site_yaml = os.path.join(self.config_dir, "site.yaml")
        self.starts_file = self.path("starts.txt")
        for folder in ("config", "run", "logs", "code/ui"):
            os.makedirs(os.path.join(self.root, folder), exist_ok=True)
        with open(os.path.join(self.root, "code", "ui", "run_ui.py"), "w") as f:
            f.write(FAKE_UI.format(starts=self.starts_file))
        self.proc = None

    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(30)
        super().tearDown()

    # ---- site files ----
    def write_site(self, transfer=True, mode=0o600):
        with open(EXAMPLE_SITE_YAML) as f:
            data = yaml.safe_load(f)
        data["paths"]["root"] = self.root
        if not transfer:
            data.pop("transfer", None)
        with open(self.site_yaml, "w") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        os.chmod(self.site_yaml, mode)

    def write_workers(self, with_ui=True):
        ui = {"id": "ui", "type": "ui", "command": "python3 run_ui.py",
              "working_directory": os.path.join(self.root, "code", "ui"), "health": {"type": "process"}}
        workers = [dummy("frame_capture_cam1", f"--log-start {self.starts_file}"),
                   dummy("transfer", f"--log-start {self.starts_file}")] + ([ui] if with_ui else [])
        data = {"mqtt": {"enabled": False}, "runtime": {"root": os.path.join(self.root, "run")},
                "log_dir": os.path.join(self.root, "logs"),
                "monitoring": {"check_interval_seconds": 0.5, "startup_stagger_seconds": 0}, "workers": workers}
        with open(os.path.join(self.config_dir, "workers.yaml"), "w") as f:
            yaml.safe_dump(data, f)

    # ---- running ----
    def cli(self, *args, timeout=90):
        cmd = [sys.executable, os.path.join(SUPERVISOR_DIR, "supervisor.py"), *args, "--site-config", self.site_yaml]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def run_supervisor(self):
        self.proc = subprocess.Popen([sys.executable, os.path.join(SUPERVISOR_DIR, "supervisor.py"), "run",
                                      "--site-config", self.site_yaml],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait_for(lambda: os.path.exists(os.path.join(self.root, "run", "supervisor.pid")), "no pid file")

    def starts(self):
        try:
            with open(self.starts_file) as f:
                return [line.split() for line in f.read().splitlines()]
        except FileNotFoundError:
            return []

    def started_names(self):
        return [line[0] for line in self.starts()]

    def mode(self):
        return setup_mode.read_mode_file(os.path.join(self.root, "run")) or {}

    def wait_for(self, condition, what, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if condition():
                return
            time.sleep(0.2)
        log = os.path.join(self.root, "logs", "supervisor.log")
        tail = open(log).read()[-2500:] if os.path.exists(log) else ""
        self.fail(f"{what}\n--- supervisor.log ---\n{tail}")

    def marker(self):
        return os.path.exists(os.path.join(self.config_dir, "SETUP_MODE"))


# ----------------------------------------------------------------------
class FreshMachine(SetupModeTest):
    def test_fresh_root_without_site_yaml_runs_only_ui(self):
        self.write_workers()                               # even a workers.yaml lying there is not used
        self.run_supervisor()
        self.wait_for(lambda: self.started_names() == ["ui"], "ui did not start")
        time.sleep(2.0)
        self.assertEqual(self.started_names(), ["ui"], "only the ui worker in setup mode")
        self.assertEqual(self.starts()[0][2], "-", "no rendered ui.json yet: the UI uses its defaults")
        self.assertEqual(self.mode()["mode"], "setup")
        self.assertIn("no site.yaml", self.mode()["reason"])
        self.assertTrue(self.marker(), "the decision survives a reboot until setup-finish")

    def test_check_config_and_status_report_setup_mode(self):
        out = self.cli("check-config")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("Setup mode (no site.yaml yet", out.stdout)
        self.assertIn("setup-finish would refuse", out.stdout)
        self.assertFalse(self.marker(), "check-config only looks; only `run` writes the marker")
        self.run_supervisor()
        self.wait_for(lambda: self.mode().get("mode") == "setup", "no mode file")
        status = self.cli("status", "--json")             # no broker in this test: mode from mode.json
        data = json.loads(status.stdout)
        self.assertEqual(data["mode"], "setup")


class SetupFinish(SetupModeTest):
    def test_setup_finish_with_a_valid_pair_starts_every_program(self):
        self.run_supervisor()
        self.wait_for(lambda: self.started_names() == ["ui"], "ui did not start")
        setup_pid, ui_pid = self.proc.pid, self.starts()[0][1]
        self.write_site(transfer=True)
        self.write_workers()
        out = self.cli("setup-finish")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("Normal mode", out.stdout)
        self.wait_for(lambda: sorted(self.started_names()) == ["frame_capture_cam1", "transfer", "ui", "ui"],
                      "programs did not start after setup-finish")
        self.assertEqual(self.proc.pid, setup_pid, "re-exec: same process for systemd and the pid file")
        self.assertIsNone(self.proc.poll())
        self.assertFalse(self.marker())
        self.wait_for(lambda: self.mode().get("mode") == "normal", "mode file not normal")
        last_ui = [line for line in self.starts() if line[0] == "ui"][-1]
        self.assertNotEqual(last_ui[1], ui_pid, "the UI restarted once ...")
        self.assertEqual(last_ui[2], os.path.join(self.config_dir, "rendered", "ui.json"), "... with its rendered ui.json")

    def test_setup_finish_with_an_invalid_pair_stays_in_setup_mode(self):
        self.run_supervisor()
        self.wait_for(lambda: self.started_names() == ["ui"], "ui did not start")
        self.write_site(mode=0o644)                         # the loader refuses: not chmod 600
        self.write_workers(with_ui=False)                   # and no ui worker
        out = self.cli("setup-finish")
        self.assertEqual(out.returncode, 1)
        self.assertIn("Refused, staying in setup mode", out.stdout)
        self.assertIn("chmod 600", out.stdout)
        self.assertIn("no enabled `ui` worker", out.stdout)
        time.sleep(2.0)
        self.assertEqual(self.started_names(), ["ui"], "nothing else started, the UI kept running")
        self.assertTrue(self.marker())
        self.assertEqual(self.mode()["mode"], "setup")
        result = json.load(open(os.path.join(self.root, "run", "setup_finish.result.json")))
        self.assertFalse(result["ok"])

    def test_the_ui_request_file_works_like_the_command(self):
        self.run_supervisor()
        self.wait_for(lambda: self.started_names() == ["ui"], "ui did not start")
        self.write_site()
        self.write_workers()
        open(os.path.join(self.root, "run", "setup_finish"), "w").close()    # what the UI writes (no id)
        self.wait_for(lambda: "frame_capture_cam1" in self.started_names(), "UI request not served")
        result = json.load(open(os.path.join(self.root, "run", "setup_finish.result.json")))
        self.assertEqual((result["ok"], result["mode"], result["id"]), (True, "normal", None))

    def test_site_without_a_transfer_section_does_not_start_transfer(self):
        self.write_site(transfer=False)
        self.write_workers()
        self.run_supervisor()                               # no marker, valid pair: normal mode at start
        self.wait_for(lambda: sorted(self.started_names()) == ["frame_capture_cam1", "ui"], "normal start")
        time.sleep(1.5)
        self.assertNotIn("transfer", self.started_names())
        self.assertEqual(self.mode()["mode"], "normal")

    def test_setup_finish_without_a_running_supervisor(self):
        out = self.cli("setup-finish")
        self.assertEqual(out.returncode, 1)
        self.assertTrue(self.marker(), "refused: stays in setup mode across a reboot")
        self.write_site()
        self.write_workers()
        out = self.cli("setup-finish")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("starts in normal mode", out.stdout)
        self.assertFalse(self.marker())

    def test_in_normal_mode_setup_finish_says_so(self):
        self.write_site()
        self.write_workers()
        self.run_supervisor()
        self.wait_for(lambda: "frame_capture_cam1" in self.started_names(), "normal start")
        out = self.cli("setup-finish", "--json")
        self.assertEqual(json.loads(out.stdout)["note"], "already in normal mode")


class ModeRules(SetupModeTest):
    def decide(self, record=False):
        site_env = SiteEnv(self.site_yaml)
        return setup_mode.decide_mode(Paths.from_site_env(site_env), site_env, record=record)

    def test_rules(self):
        self.assertEqual(self.decide()[0], "setup")                       # no site.yaml
        self.assertFalse(self.marker(), "only record=True (run) writes the marker")
        self.write_site()
        self.assertEqual(self.decide()[0], "setup")                       # no workers.yaml
        self.write_workers()
        self.assertEqual(self.decide()[0], "normal")
        open(os.path.join(self.config_dir, "SETUP_MODE"), "w").close()
        self.assertEqual(self.decide()[0], "setup")                       # marker wins
        os.remove(os.path.join(self.config_dir, "SETUP_MODE"))
        with open(os.path.join(self.config_dir, "workers.yaml"), "a") as f:
            f.write("nonsense_key: 1\n")
        self.assertIn("workers.yaml", self.decide()[1])                   # broken workers.yaml: setup

    def test_finished_site_with_a_broken_site_yaml_keeps_inspecting(self):
        # no marker = setup was finished; a site.yaml typo must not stop production after a reboot
        self.write_site(mode=0o644)
        self.write_workers()
        self.assertEqual(self.decide()[0], "normal")
        self.run_supervisor()
        self.wait_for(lambda: "frame_capture_cam1" in self.started_names(), "normal start")

    def test_the_built_in_list_is_only_ui_with_process_health(self):
        config = setup_mode.setup_workers_config(Paths.from_site_env(SiteEnv(self.site_yaml)))
        self.assertEqual([w.id for w in config.workers], ["ui"])
        ui = config.workers[0]
        self.assertEqual((ui.command, ui.health.type), ("python3 run_ui.py", "process"))
        self.assertEqual(ui.working_directory, os.path.join(self.root, "code", "ui"))
        self.assertEqual(config.settings.runtime_root, os.path.join(self.root, "run"))


if __name__ == "__main__":
    unittest.main()
