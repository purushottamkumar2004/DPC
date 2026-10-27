"""Tests for STATUS F21 (last_error.json in events, status and supervisor.log) and F19
(the command line finds <root>/config/workers.yaml and <root>/run/ without -c).

Run from the repo root:  python3 -m unittest tests.unit.test_supervisor_f19_f21 -v

F21 uses the REAL writer of the inspection program (workers/inspection/last_error.py), so the
Supervisor is tested against the file format a worker really produces.
"""

import json
import os
import subprocess
import sys
import time
import unittest

import yaml

from tests.unit.test_supervisor_p5 import SECRETS, SiteYamlTest                  # noqa: F401 (sets sys.path)
from tests.unit.test_supervisor_v2 import REPO_ROOT, SUPERVISOR_DIR, SupervisorTest, dummy, supervisor_main
from core.last_error import read_last_error, summary                            # noqa: E402
from core.process_manager import WorkerStatus                                   # noqa: E402
from core.site_env import SiteEnv                                               # noqa: E402

INSPECTION_DIR = os.path.join(REPO_ROOT, "workers", "inspection")

FAILING_WORKER = '''
import os, sys, time
sys.path.insert(0, {inspection_dir!r})
from last_error import write_last_error          # the inspection program's own writer

root, worker_id, message_file, delay = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
time.sleep(delay)
with open(message_file) as f:                     # not on the command line: that goes into the worker log
    message = f.read()


def open_camera_table():
    raise ValueError(message)                     # FORCED ERROR


try:
    open_camera_table()
except Exception as exc:
    write_last_error(root, worker_id, exc)        # no scrubbing here: the Supervisor scrubs too
    sys.exit(1)
'''


class _FailingWorkerTest(SiteYamlTest):
    """Helpers only: a worker that dies with the inspection program's own last_error writer."""

    def setUp(self):
        super().setUp()
        self.root = self.path("INSIGHTZZ")
        self.run_dir = os.path.join(self.root, "run")
        self.script = self.path("failing_worker.py")
        with open(self.script, "w") as f:
            f.write(FAILING_WORKER.format(inspection_dir=INSPECTION_DIR))

    def failing(self, worker_id, message="camera table missing", delay=0.3, **extra):
        message_file = self.path(f"message_{worker_id}.txt")
        with open(message_file, "w") as f:
            f.write(message)
        return {"id": worker_id, "type": "test",
                "command": f"{sys.executable} {self.script} {self.root} {worker_id} {message_file} {delay}",
                "restart": {"max_retries": 1, "retry_delay_seconds": 0.3}, **extra}

    def build_with_run_dir(self, workers, site_env=None):
        cfg = self.config(workers, runtime={"root": self.run_dir})
        self.sup = supervisor_main.Supervisor(cfg, mqtt=self.mqtt, site_env=site_env or SiteEnv(self.path("none.yaml")))
        self.sup.start()

    def forced_error_line(self):
        with open(self.script) as f:
            return next(n for n, line in enumerate(f, start=1) if "FORCED ERROR" in line)

    def events(self, name):
        return [d for w, e, d in self.mqtt.events if e == name]


class LastErrorF21(_FailingWorkerTest):
    def test_forced_error_shows_type_and_line_in_event_status_and_log(self):
        self.build_with_run_dir([self.failing("inspection_left")])
        self.run_until(lambda: self.events("worker_restarted"), message="no restart")
        failure = self.events("worker_failure")[0]["last_error"]
        self.assertEqual(failure["type"], "ValueError")
        self.assertEqual(failure["message"], "camera table missing")
        where = f"failing_worker.py:{self.forced_error_line()}"
        self.assertEqual(failure["where"], where)
        self.assertTrue(failure["traceback"][-1].startswith("ValueError"))
        self.assertEqual(self.events("worker_restarted")[0]["last_error"], failure, "the restart says why")
        status = self.sup.process_manager.get_status(self.worker("inspection_left"))
        self.assertEqual(status["last_error"]["where"], where)
        self.assertTrue(any(f"last error: ValueError: camera table missing ({where})" in line
                            for line in self.logs.lines), "supervisor.log gets the same line (root logger)")

    def test_a_stale_file_from_an_earlier_run_is_not_reported(self):
        os.makedirs(self.run_dir)
        stale = os.path.join(self.run_dir, "cleanup.last_error.json")
        with open(stale, "w") as f:
            json.dump({"type": "OldError", "message": "yesterday", "where": "x.py:1"}, f)
        old = time.time() - 3600
        os.utime(stale, (old, old))
        self.build_with_run_dir([dummy("cleanup", "--crash-after 0.3", restart={"max_retries": 0})])
        self.run_until(lambda: self.events("worker_failure"), message="no failure")
        self.assertNotIn("last_error", self.events("worker_failure")[0])
        self.assertIsNone(self.worker("cleanup").last_error)

    def test_a_crash_without_a_file_reports_as_before(self):
        self.build_with_run_dir([dummy("cleanup", "--crash-after 0.3", restart={"max_retries": 0})])
        self.run_until(lambda: self.events("worker_failure"), message="no failure")
        self.assertNotIn("last_error", self.events("worker_failure")[0])
        self.assertIn("exit code 1", self.events("worker_failure")[0]["reason"])

    def test_health_restart_does_not_reuse_an_old_error(self):
        self.build_with_run_dir([self.failing("inspection_left", restart={"max_retries": 3, "retry_delay_seconds": 0.3})])
        self.run_until(lambda: self.events("worker_restarted"), message="no restart")
        worker = self.worker("inspection_left")
        worker.restart_error = None
        self.sup.recovery.handle_failure(worker, "unhealthy for 120s: queue lag")   # a health failure: no file read
        self.assertNotIn("last_error", self.events("worker_failure")[-1])

    def test_periodic_failed_run_carries_last_error(self):
        self.build_with_run_dir([self.failing("tick", delay=0.1, mode="periodic", interval_s=5)])
        self.run_until(lambda: self.events("worker_failure"), message="no failure")
        self.assertEqual(self.events("worker_failure")[0]["last_error"]["type"], "ValueError")

    def test_secret_values_in_the_file_are_scrubbed_before_mqtt_and_log(self):
        site = self.write_site_yaml()
        message = f"login failed for password {SECRETS['db_password']}"
        self.build_with_run_dir([self.failing("cleanup", message=message, restart={"max_retries": 0})],
                                site_env=SiteEnv(site))
        self.run_until(lambda: self.events("worker_failure"), message="no failure")
        self.assertEqual(self.events("worker_failure")[0]["last_error"]["message"], "login failed for password ***")
        self.assert_no_secret_anywhere()

    def test_checked_fields_only(self):
        os.makedirs(self.run_dir)
        path = os.path.join(self.run_dir, "w.last_error.json")
        with open(path, "w") as f:
            json.dump({"type": "E" * 500, "message": "m" * 5000, "where": 12, "extra": "dropped",
                       "traceback": ["line"] * 50 + [7]}, f)
        error = read_last_error(self.run_dir, "w", since=None)
        self.assertEqual(set(error), {"type", "message", "traceback"})
        self.assertEqual((len(error["type"]), len(error["message"]), len(error["traceback"])), (100, 500, 12))
        with open(path, "w") as f:
            f.write("{not json")
        self.assertEqual(read_last_error(self.run_dir, "w", since=None)["type"], "unknown")
        self.assertEqual(summary({"type": "KeyError", "message": "'x'", "where": "a.py:3"}), "KeyError: 'x' (a.py:3)")


class LastErrorEndToEnd(_FailingWorkerTest):
    """STATUS F21 "done when", with the real program: `supervisor.py run` (no -c: F19 finds
    <root>/config/workers.yaml), a worker with a forced error, then supervisor.log on disk."""

    def test_forced_error_in_supervisor_log_file(self):
        os.makedirs(os.path.join(self.root, "config"))
        data = {"mqtt": {"enabled": False}, "runtime": {"root": self.run_dir},
                "log_dir": os.path.join(self.root, "logs"),
                "monitoring": {"check_interval_seconds": 0.5, "startup_stagger_seconds": 0},
                "workers": [self.failing("inspection_left", restart={"max_retries": 0})]}
        with open(os.path.join(self.root, "config", "workers.yaml"), "w") as f:
            yaml.safe_dump(data, f)
        env = dict(os.environ, DPC_SITE_CONFIG=os.path.join(self.root, "config", "site.yaml"))
        proc = subprocess.Popen([sys.executable, os.path.join(SUPERVISOR_DIR, "supervisor.py"), "run"],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log_file = os.path.join(self.root, "logs", "supervisor.log")
        expected = f"last error: ValueError: camera table missing (failing_worker.py:{self.forced_error_line()})"
        try:
            deadline = time.time() + 20
            text = ""
            while time.time() < deadline and expected not in text:
                time.sleep(0.2)
                if os.path.exists(log_file):
                    with open(log_file) as f:
                        text = f.read()
            self.assertIn(expected, text)
            self.assertIn("ValueError: camera table missing", text.split(expected, 1)[1], "traceback follows")
        finally:
            proc.terminate()
            proc.wait(20)


class FindWorkersFileF19(SupervisorTest):
    """`supervisor.py <command>` without -c, run in a subprocess with HOME and DPC_SITE_CONFIG set."""

    def cli(self, *args, env_extra=None):
        env = dict(os.environ, HOME=self.path("home"))
        env.pop("DPC_SITE_CONFIG", None)
        env.update(env_extra or {})
        return subprocess.run([sys.executable, os.path.join(SUPERVISOR_DIR, "supervisor.py"), *args],
                              capture_output=True, text=True, timeout=60, env=env)

    def site_root(self, root):
        os.makedirs(os.path.join(root, "config"), exist_ok=True)
        data = {"mqtt": {"enabled": False}, "runtime": {"root": os.path.join(root, "run")},
                "log_dir": os.path.join(root, "logs"), "workers": [dummy("site_worker", "")]}
        with open(os.path.join(root, "config", "workers.yaml"), "w") as f:
            yaml.safe_dump(data, f)

    def test_default_site_root_in_home(self):
        self.site_root(self.path("home/INSIGHTZZ"))
        result = self.cli("check-config")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(os.path.join(self.path("home/INSIGHTZZ"), "config", "workers.yaml"), result.stdout)
        self.assertIn("site_worker", result.stdout)

    def test_dpc_site_config_points_to_another_root(self):
        root = self.path("elsewhere/INSIGHTZZ")
        self.site_root(root)
        result = self.cli("check-config", env_extra={"DPC_SITE_CONFIG": os.path.join(root, "config", "site.yaml")})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("site_worker", result.stdout)

    def test_reload_without_c_finds_the_run_folder_of_the_site(self):
        self.site_root(self.path("home/INSIGHTZZ"))
        result = self.cli("reload")
        self.assertEqual(result.returncode, 3)
        self.assertIn(os.path.join(self.path("home/INSIGHTZZ"), "run", "supervisor.pid"), result.stdout,
                      "the pid is looked for in <root>/run, not in the code folder")

    def test_site_folder_without_workers_yaml_is_an_error_never_the_example(self):
        os.makedirs(self.path("home/INSIGHTZZ/config"))
        result = self.cli("check-config")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no workers.yaml in", result.stdout)
        self.assertNotIn("dummy_crasher", result.stdout)

    def test_development_machine_uses_the_example_with_a_note(self):
        result = self.cli("check-config")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("using the example", result.stderr)
        self.assertIn("dummy_crasher", result.stdout)

    def test_c_still_wins(self):
        self.site_root(self.path("home/INSIGHTZZ"))
        result = self.cli("check-config", "-c", os.path.join(SUPERVISOR_DIR, "config", "workers.basler-2cam.yaml"))
        self.assertIn("inspection_right", result.stdout)


if __name__ == "__main__":
    unittest.main()
