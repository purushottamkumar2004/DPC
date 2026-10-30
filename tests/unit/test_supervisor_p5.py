"""Tests for STATUS P5 (SUPERVISOR).

Run from the repo root:  python3 -m unittest tests.unit.test_supervisor_p5 -v

    Reload             6f   render at start, DPC_WORKER_CONFIG, `reload` restarts only changed workers,
                            restart_cost high waits (quiet window / --confirm), a site.yaml error changes nothing
    ReloadCommandLine  6f   `supervisor.py reload` against a running `supervisor.py run` (the STATUS P5
                            "done when": edit CAM2 exposure, reload, only CAM2 restarts)
    Follows            D-068 inspection follows its camera
    RunTimeout         F10  run_timeout_seconds for oneshot / periodic
    MqttLogin          F11  the Supervisor's broker login comes from site.yaml
    ConfigKeysP5            validation of the new keys
Real dummy processes, a real site.yaml (from the example, secrets replaced) and the real renderer.
"""

import json
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

import yaml

from tests.unit.test_supervisor_v2 import (DUMMY, EXAMPLE_SITE_YAML, REPO_ROOT, SUPERVISOR_DIR,  # noqa: F401
                                           SupervisorTest, dummy)
from core.config import ConfigError                                       # noqa: E402
from core.process_manager import WorkerStatus                             # noqa: E402
from core.site_env import SiteEnv                                         # noqa: E402
import communication.mqtt_client as mqtt_module                           # noqa: E402

SECRETS = {"db_password": "dbpass-P5q8-secret", "db_user": "dbuser-P5w2",
           "mqtt_user": "mqttuser-P5k4", "mqtt_password": "mqttpass-P5z7-secret",
           "graph_secret": "graph-P5m3-secret"}

FAKE_CAPTURE = '''
import os, sys, time
live, camera_ok, starts = sys.argv[1:4]
with open(starts, "a") as f:
    f.write(str(os.getpid()) + "\\n")
while True:                                  # frames only while the "cable" is in
    if os.path.exists(camera_ok):
        with open(live, "w") as f:
            f.write("frame")
    time.sleep(0.1)
'''


class SiteYamlTest(SupervisorTest):
    """A real site.yaml in the temp folder (root = <tmp>/INSIGHTZZ), chmod 600."""

    def write_site_yaml(self, mode=0o600, edit=None):
        with open(EXAMPLE_SITE_YAML) as f:
            data = yaml.safe_load(f)
        data["paths"]["root"] = self.path("INSIGHTZZ")
        data["db"]["user"], data["db"]["password"] = SECRETS["db_user"], SECRETS["db_password"]
        data["mqtt"]["user"], data["mqtt"]["password"] = SECRETS["mqtt_user"], SECRETS["mqtt_password"]
        if data.get("graph"):
            data["graph"]["client_secret"] = SECRETS["graph_secret"]
        if edit:
            edit(data)
        os.makedirs(self.path("INSIGHTZZ/config"), exist_ok=True)
        path = self.path("INSIGHTZZ/config/site.yaml")
        with open(path, "w") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        os.chmod(path, mode)
        return path

    @staticmethod
    def read(path):
        with open(path) as f:
            return f.read()

    def rendered(self, worker_id):
        return self.path(f"INSIGHTZZ/config/rendered/{worker_id}.json")

    def everything_that_left_the_supervisor(self):
        text = "\n".join(self.logs.lines) + "\n".join(self.mqtt.raw)
        for folder, _, files in os.walk(self.path("logs")):
            for name in files:
                with open(os.path.join(folder, name), errors="replace") as f:
                    text += f.read()
        return text

    def assert_no_secret_anywhere(self):
        text = self.everything_that_left_the_supervisor()
        for label, value in SECRETS.items():
            self.assertNotIn(value, text, f"secret '{label}' leaked")


def set_exposure(camera_id, value):
    def edit(data):
        for cam in data["capture"]["cameras"]:
            if cam["id"] == camera_id:
                cam["exposure_us"] = value
    return edit


def set_wheel_width(side, value):
    def edit(data):
        data["inspection"]["sides"][side]["object_sizes"]["wheel_w"] = value
    return edit


# ----------------------------------------------------------------------
# 6f: render at start, DPC_WORKER_CONFIG, reload
# ----------------------------------------------------------------------
class Reload(SiteYamlTest):
    def four_workers(self, monitoring=None):
        workers = [dummy(w, f"--dump-env {self.path('env_' + w + '.txt')}",
                         **({"restart_cost": "high"} if w.startswith("inspection") else {}))
                   for w in ("frame_capture_cam1", "frame_capture_cam2", "inspection_left", "inspection_right")]
        # a worker with no rendered file (mail has one since F36): the PLC light
        workers.append(dummy("light", f"--dump-env {self.path('env_light.txt')}"))
        self.build(workers, monitoring, site_env=SiteEnv(self.write_site_yaml()))
        self.sup.start()
        self.run_until(lambda: all(w.status == WorkerStatus.RUNNING for w in self.sup.workers.values()),
                       message="workers not running")
        return {w.id: w.pid for w in self.sup.workers.values()}

    def env_of(self, worker_id):
        """The DPC_* variables the dummy wrote. F24: a RUNNING worker may still be writing the
        file, so wait until it is complete (DPC_WORKER_ID sorts last of the names written)."""
        path = self.path(f"env_{worker_id}.txt")
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with open(path) as f:
                    text = f.read()
            except FileNotFoundError:
                text = ""
            if "DPC_WORKER_ID=" in text and text.endswith("\n"):
                return dict(line.split("=", 1) for line in text.splitlines())
            time.sleep(0.05)
        self.fail(f"{path} not complete within 10 s")

    def test_render_at_start_and_dpc_worker_config_per_worker(self):
        self.four_workers()
        for worker_id in ("frame_capture_cam1", "frame_capture_cam2", "inspection_left", "inspection_right"):
            self.assertTrue(os.path.isfile(self.rendered(worker_id)))
            self.assertEqual(self.env_of(worker_id)["DPC_WORKER_CONFIG"], self.rendered(worker_id))
        self.assertNotIn("DPC_WORKER_CONFIG", self.env_of("light"), "no rendered file -> no variable")
        self.assert_no_secret_anywhere()

    def test_edit_cam2_exposure_then_reload_restarts_only_cam2(self):
        pids = self.four_workers()
        self.write_site_yaml(edit=set_exposure("CAM2", 2500))
        report = self.sup.reload()
        self.assertTrue(report["ok"])
        self.assertEqual([w for w, s in report["changes"].items() if s == "written"], ["frame_capture_cam2"])
        self.assertEqual(report["restarted"], ["frame_capture_cam2"])
        self.run_until(lambda: self.worker("frame_capture_cam2").status == WorkerStatus.RUNNING, message="cam2 back")
        self.run_for(1.0)
        for worker_id, pid in pids.items():
            if worker_id != "frame_capture_cam2":
                self.assertEqual(self.worker(worker_id).pid, pid, f"{worker_id} must not restart")
        self.assertNotEqual(self.worker("frame_capture_cam2").pid, pids["frame_capture_cam2"])
        with open(self.rendered("frame_capture_cam2")) as f:
            self.assertEqual(json.load(f)["camera"]["exposure_us"], 2500)
        event = self.mqtt.supervisor_events()[-1]
        self.assertEqual((event["event"], event["restarted"]), ("config_reloaded", ["frame_capture_cam2"]))
        self.assert_no_secret_anywhere()

    def test_reload_without_change_restarts_nothing(self):
        pids = self.four_workers()
        report = self.sup.reload()
        self.assertEqual((report["restarted"], report["pending"]), ([], []))
        self.assertEqual({w.id: w.pid for w in self.sup.workers.values()}, pids)

    def test_high_cost_waits_then_confirm_restarts_it(self):
        pids = self.four_workers()
        self.write_site_yaml(edit=set_wheel_width("LEFT", 540))
        report = self.sup.reload()
        self.assertEqual((report["restarted"], report["pending"]), ([], ["inspection_left"]))
        left = self.worker("inspection_left")
        self.assertEqual(left.pid, pids["inspection_left"], "inspection must not lose its wheel state unasked")
        self.assertTrue(left.config_pending)
        self.assertTrue(self.sup.process_manager.get_status(left)["config_pending"])
        self.assertIn("reload --confirm", left.message)

        report = self.sup.reload(confirm=True)                         # file is "unchanged" now, still restarted
        self.assertEqual(report["restarted"], ["inspection_left"])
        self.assertNotEqual(self.worker("inspection_left").pid, pids["inspection_left"])
        self.assertFalse(self.worker("inspection_left").config_pending)
        self.assertEqual(self.worker("inspection_right").pid, pids["inspection_right"])

    def test_quiet_window_restarts_pending_high_cost_workers(self):
        pids = self.four_workers()
        self.write_site_yaml(edit=set_wheel_width("RIGHT", 545))
        self.sup.reload()
        self.assertTrue(self.worker("inspection_right").config_pending)
        now = time.localtime()
        start = (now.tm_hour * 60 + now.tm_min - 1) % 1440
        end = (now.tm_hour * 60 + now.tm_min + 2) % 1440
        self.sup.settings.monitoring.reload_quiet_window = f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"
        self.run_until(lambda: not self.worker("inspection_right").config_pending, message="not restarted in the window")
        self.assertNotEqual(self.worker("inspection_right").pid, pids["inspection_right"])

    def test_quiet_window_parsing_and_midnight(self):
        self.four_workers({"reload_quiet_window": "23:30-00:30"})
        at = lambda hh, mm: time.mktime((2026, 10, 24, hh, mm, 0, 0, 0, -1))  # noqa: E731
        self.assertTrue(self.sup._in_quiet_window(at(23, 45)))
        self.assertTrue(self.sup._in_quiet_window(at(0, 10)))
        self.assertFalse(self.sup._in_quiet_window(at(0, 30)))
        self.assertFalse(self.sup._in_quiet_window(at(12, 0)))

    def test_site_yaml_error_changes_nothing_and_is_reported(self):
        pids = self.four_workers()
        before = {w: self.read(self.rendered(w)) for w in ("frame_capture_cam2", "inspection_left")}
        self.write_site_yaml(edit=set_exposure("CAM2", 2600), mode=0o644)     # loader refuses chmod 644
        report = self.sup.reload()
        self.assertFalse(report["ok"])
        self.assertIn("chmod 600", report["error"])
        self.assertEqual(report["restarted"], [])
        self.assertEqual({w.id: w.pid for w in self.sup.workers.values()}, pids)
        self.assertEqual({w: self.read(self.rendered(w)) for w in before}, before, "nothing written")
        self.assertEqual(self.mqtt.supervisor_events()[-1]["event"], "config_reload_failed")
        self.assertTrue(any("nothing changed" in line for line in self.logs.lines))
        self.assert_no_secret_anywhere()

    def test_bad_site_yaml_at_start_keeps_the_last_rendered_files(self):
        path = self.write_site_yaml()
        SiteEnv(path).render()                                         # a good render from earlier
        self.write_site_yaml(edit=set_exposure("CAM1", 1800), mode=0o644)
        self.build([dummy("frame_capture_cam1", f"--dump-env {self.path('env_cam1.txt')}")], site_env=SiteEnv(path))
        self.sup.start()
        self.run_until(lambda: "DPC_WORKER_ID=" in (self.read(self.path("env_cam1.txt"))
                                                     if os.path.exists(self.path("env_cam1.txt")) else ""),
                       message="no start")
        self.assertIn(f"DPC_WORKER_CONFIG={self.rendered('frame_capture_cam1')}", self.read(self.path("env_cam1.txt")))
        with open(self.rendered("frame_capture_cam1")) as f:
            self.assertEqual(json.load(f)["camera"]["exposure_us"], 1500, "the old file stays")
        self.assertEqual(self.mqtt.supervisor_events()[0]["event"], "config_render_failed")

    def test_no_site_yaml_at_all_is_only_a_warning(self):
        self.build([dummy("frame_capture_cam1", "")], site_env=SiteEnv(self.path("missing/site.yaml")))
        self.sup.start()
        self.run_until(lambda: self.worker("frame_capture_cam1").status == WorkerStatus.RUNNING, message="no start")
        self.assertFalse(any(" ERROR " in line and "rendered" in line for line in self.logs.lines))

    def test_written_but_stopped_or_unsupervised_workers_are_reported_not_started(self):
        self.build([dummy("frame_capture_cam1", "")], site_env=SiteEnv(self.write_site_yaml()))
        self.sup.start()
        self.run_until(lambda: self.worker("frame_capture_cam1").status == WorkerStatus.RUNNING, message="no start")
        self.sup._run_command(self.worker("frame_capture_cam1"), "stop", "test")
        self.write_site_yaml(edit=lambda d: (set_exposure("CAM1", 1700)(d), set_exposure("CAM2", 2100)(d)))
        report = self.sup.reload()
        self.assertEqual(report["not_running"], ["frame_capture_cam1"])
        self.assertEqual(report["not_supervised"], ["frame_capture_cam2"])
        self.assertEqual(self.worker("frame_capture_cam1").status, WorkerStatus.STOPPED, "reload never starts a stopped worker")


class ReloadCommandLine(SiteYamlTest):
    """`supervisor.py run` in a subprocess, `supervisor.py reload` against it."""

    def setUp(self):
        super().setUp()
        self.site = self.write_site_yaml()
        workers = [dummy("frame_capture_cam1", f"--log-start {self.path('starts.txt')}"),
                   dummy("frame_capture_cam2", f"--log-start {self.path('starts.txt')}")]
        data = {"monitoring": {"check_interval_seconds": 0.5, "startup_stagger_seconds": 0},
                "mqtt": {"enabled": False}, "runtime": {"root": self.path("run")},
                "log_dir": self.path("logs"), "workers": workers}
        self.workers_yaml = self.path("workers.yaml")
        with open(self.workers_yaml, "w") as f:
            yaml.safe_dump(data, f)
        self.proc = None

    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(20)
        super().tearDown()

    def cli(self, *args):
        cmd = [sys.executable, os.path.join(SUPERVISOR_DIR, "supervisor.py"), *args,
               "-c", self.workers_yaml, "--site-config", self.site]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    def starts(self):
        try:
            with open(self.path("starts.txt")) as f:
                return [line.split()[0] for line in f.read().splitlines()]
        except FileNotFoundError:
            return []

    def test_reload_when_not_running(self):
        result = self.cli("reload")
        self.assertEqual(result.returncode, 3)
        self.assertIn("not running", result.stdout)

    def test_edit_cam2_exposure_reload_only_cam2_restarts(self):
        self.proc = subprocess.Popen([sys.executable, os.path.join(SUPERVISOR_DIR, "supervisor.py"), "run",
                                      "-c", self.workers_yaml, "--site-config", self.site],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 15
        while time.time() < deadline and len(self.starts()) < 2:
            time.sleep(0.1)
        self.assertEqual(sorted(self.starts()), ["frame_capture_cam1", "frame_capture_cam2"])

        self.write_site_yaml(edit=set_exposure("CAM2", 2400))
        result = self.cli("reload")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1 written, 5 unchanged", result.stdout)        # 6 rendered files: + cleanup.json (F29), mail.json (F36)
        self.assertIn("restarted: frame_capture_cam2", result.stdout)
        time.sleep(1.0)
        self.assertEqual(self.starts().count("frame_capture_cam1"), 1, "cam1 must not restart")
        self.assertEqual(self.starts().count("frame_capture_cam2"), 2)

        self.write_site_yaml(edit=set_exposure("CAM2", 2400), mode=0o644)
        result = self.cli("reload")
        self.assertEqual(result.returncode, 1)
        self.assertIn("nothing changed", result.stdout)
        for value in SECRETS.values():
            self.assertNotIn(value, result.stdout + result.stderr)
        with open(self.path("logs/supervisor.log")) as f:
            log_text = f.read()
        for value in SECRETS.values():
            self.assertNotIn(value, log_text)
        self.assertEqual(oct(os.stat(self.path("run/reload_result.json")).st_mode & 0o777), "0o600")


# ----------------------------------------------------------------------
# D-068: inspection follows its camera
# ----------------------------------------------------------------------
class Follows(SupervisorTest):
    def setUp(self):
        super().setUp()
        self.script = self.path("fake_capture.py")
        with open(self.script, "w") as f:
            f.write(FAKE_CAPTURE)

    def capture(self, cam):
        n = cam[-1]
        open(self.path(f"cable{n}"), "w").close()                      # camera connected
        return {"id": cam, "type": "capture",
                "command": f"{sys.executable} {self.script} {self.path('live' + n)} {self.path('cable' + n)} "
                           f"{self.path('starts_' + cam)}",
                "restart": {"max_retries": 50, "retry_delay_seconds": 0.2},
                "health": {"type": "file_activity", "path": self.path("live" + n), "max_age_seconds": 0.6,
                           "startup_grace_seconds": 0.5}}

    def pair_setup(self, both=True):
        workers = [self.capture("frame_capture_cam1"),
                   dummy("inspection_left", f"--log-start {self.path('starts_inspection')}",
                         follows="frame_capture_cam1", restart_cost="high")]
        if both:
            workers += [self.capture("frame_capture_cam2"),
                        dummy("inspection_right", f"--log-start {self.path('starts_inspection')}",
                              follows="frame_capture_cam2", restart_cost="high")]
        self.build(workers, {"worker_timeout_seconds": 0.4, "high_cost_timeout_seconds": 5})
        self.sup.start()
        self.run_until(lambda: all(w.status == WorkerStatus.RUNNING for w in self.sup.workers.values()),
                       message="not all running")

    def inspection_starts(self, worker_id):
        return [e for w, e, _ in self.mqtt.events if w == worker_id and e == "worker_started"]

    def pull_cable(self, n):
        os.remove(self.path(f"cable{n}"))

    def plug_cable(self, n):
        open(self.path(f"cable{n}"), "w").close()

    def test_inspection_starts_only_after_its_camera_is_healthy(self):
        self.build([dummy("inspection_left", "", follows="frame_capture_cam1"), self.capture("frame_capture_cam1")],
                   {"worker_timeout_seconds": 0.4})
        self.assertEqual(list(self.sup.workers), ["frame_capture_cam1", "inspection_left"], "follows orders the start")
        self.pull_cable(1)                                              # camera not there at boot
        self.sup.start()
        self.run_for(2.0)
        self.assertEqual(self.inspection_starts("inspection_left"), [])
        self.assertIn("follows frame_capture_cam1", self.worker("inspection_left").message)
        self.plug_cable(1)
        self.run_until(lambda: self.inspection_starts("inspection_left"), message="inspection never started")
        self.assertEqual(self.worker("frame_capture_cam1").status, WorkerStatus.RUNNING)

    def test_camera_down_pauses_its_side_only_no_loop_and_starts_once_after(self):
        self.pair_setup()
        right_pid = self.worker("inspection_right").pid
        self.pull_cable(1)
        self.run_until(lambda: self.worker("inspection_left").paused_by == "frame_capture_cam1",
                       message="left inspection not paused")
        left = self.worker("inspection_left")
        self.assertEqual((left.status, left.pid), (WorkerStatus.STOPPED, None))
        self.assertIn("paused: follows frame_capture_cam1", left.message)

        self.run_for(4.0)                                              # capture restarts again and again ...
        self.assertGreaterEqual(self.mqtt.event_names("frame_capture_cam1").count("worker_restarted"), 2)
        self.assertEqual(len(self.inspection_starts("inspection_left")), 1, "... inspection is not started in a loop")
        self.assertEqual(left.status, WorkerStatus.STOPPED)
        self.assertEqual(self.worker("inspection_right").pid, right_pid, "the other side is untouched")

        self.plug_cable(1)
        self.run_until(lambda: left.status == WorkerStatus.RUNNING, message="left inspection not back")
        self.run_for(1.5)
        self.assertEqual(len(self.inspection_starts("inspection_left")), 2, "started exactly once more")
        self.assertIsNone(left.paused_by)
        resumed = [d for w, e, d in self.mqtt.events if w == "inspection_left" and e == "worker_started"][-1]
        self.assertIn("healthy again", resumed["reason"])
        self.assertEqual(self.worker("inspection_right").pid, right_pid)

    def test_capture_killed_pauses_and_resumes_inspection(self):
        self.pair_setup(both=False)
        os.kill(self.worker("frame_capture_cam1").pid, signal.SIGKILL)
        self.run_until(lambda: self.worker("inspection_left").paused_by, message="not paused")
        self.run_until(lambda: self.worker("inspection_left").status == WorkerStatus.RUNNING, message="not resumed")

    def test_capture_failed_keeps_inspection_paused_until_a_person_starts_the_capture(self):
        self.build([dict(self.capture("frame_capture_cam1"), restart={"max_retries": 1, "retry_delay_seconds": 0.2}),
                    dummy("inspection_left", "", follows="frame_capture_cam1")], {"worker_timeout_seconds": 0.4})
        self.sup.start()
        self.run_until(lambda: self.worker("inspection_left").status == WorkerStatus.RUNNING, message="no start")
        self.pull_cable(1)
        self.run_until(lambda: self.worker("frame_capture_cam1").status == WorkerStatus.FAILED, timeout=15,
                       message="capture never FAILED")
        self.run_for(1.0)
        self.assertEqual(self.worker("inspection_left").status, WorkerStatus.STOPPED)
        self.plug_cable(1)
        self.sup._run_command(self.worker("frame_capture_cam1"), "start", "operator")
        self.run_until(lambda: self.worker("inspection_left").status == WorkerStatus.RUNNING, message="not resumed")

    def test_planned_capture_restart_does_not_touch_inspection(self):
        self.pair_setup(both=False)
        left_pid = self.worker("inspection_left").pid
        self.sup._run_command(self.worker("frame_capture_cam1"), "restart", "config reload")
        self.run_until(lambda: self.worker("frame_capture_cam1").status == WorkerStatus.RUNNING, message="capture back")
        self.run_for(1.0)
        self.assertEqual(self.worker("inspection_left").pid, left_pid)

    def test_operator_stop_of_a_paused_inspection_is_respected(self):
        self.pair_setup(both=False)
        self.pull_cable(1)
        self.run_until(lambda: self.worker("inspection_left").paused_by, message="not paused")
        self.sup._run_command(self.worker("inspection_left"), "stop", "maintenance")
        self.plug_cable(1)
        self.run_until(lambda: self.worker("frame_capture_cam1").status == WorkerStatus.RUNNING, message="capture back")
        self.run_for(1.5)
        self.assertEqual(self.worker("inspection_left").status, WorkerStatus.STOPPED, "operator stop wins")

    def test_operator_start_while_camera_down_is_not_undone(self):
        self.pair_setup(both=False)
        self.pull_cable(1)
        self.run_until(lambda: self.worker("inspection_left").paused_by, message="not paused")
        self.sup._run_command(self.worker("inspection_left"), "start", "operator")
        self.run_for(2.0)
        self.assertIsNotNone(self.worker("inspection_left").pid, "only the change into 'down' pauses")

    def test_status_shows_follows_and_paused_by(self):
        self.pair_setup(both=False)
        self.pull_cable(1)
        self.run_until(lambda: self.worker("inspection_left").paused_by, message="not paused")
        status = self.sup.process_manager.get_status(self.worker("inspection_left"))
        self.assertEqual((status["follows"], status["paused_by"]), ("frame_capture_cam1", "frame_capture_cam1"))


# ----------------------------------------------------------------------
# F10: run_timeout_seconds
# ----------------------------------------------------------------------
class RunTimeout(SupervisorTest):
    def test_oneshot_that_hangs_is_stopped_and_follows_the_retry_policy(self):
        self.build([dummy("light", "--exit-after 30", mode="oneshot", run_timeout_seconds=0.5,
                          restart={"max_retries": 1, "retry_delay_seconds": 0.2})])
        self.sup.start()
        self.run_until(lambda: self.worker("light").status == WorkerStatus.FAILED, message="never FAILED")
        failures = [d for w, e, d in self.mqtt.events if e == "worker_failure"]
        self.assertEqual(len(failures), 2)
        self.assertIn("run took longer than 0s", failures[0]["reason"].replace("0.5", "0"))
        self.assertIsNone(self.worker("light").pid)

    def test_periodic_run_that_hangs_is_a_failed_run_and_the_schedule_goes_on(self):
        self.build([dummy("tick", f"--exit-after 30 --log-start {self.path('ticks')}", mode="periodic",
                          interval_s=0.8, run_timeout_seconds=0.4)])
        self.sup.start()
        self.run_until(lambda: self.worker("tick").consecutive_failures >= 2, timeout=10, message="no failed runs")
        tick = self.worker("tick")
        self.assertNotEqual(tick.status, WorkerStatus.FAILED)
        self.assertIsNotNone(tick.next_run_at)
        self.assertIn("run took longer", tick.message)

    def test_a_quick_run_is_not_affected(self):
        self.build([dummy("light", "--exit-after 0.2", mode="oneshot", run_timeout_seconds=5)])
        self.sup.start()
        self.run_until(lambda: self.worker("light").completed_runs == 1, message="did not complete")
        self.assertNotIn("worker_failure", self.mqtt.event_names("light"))


# ----------------------------------------------------------------------
# F11: MQTT login from site.yaml
# ----------------------------------------------------------------------
class _FakePahoClient:
    instances = []

    def __init__(self, *args, **kwargs):
        self.login = None
        _FakePahoClient.instances.append(self)

    def username_pw_set(self, user, password):
        self.login = (user, password)

    def will_set(self, *a, **k): pass
    def connect_async(self, *a, **k): pass
    def loop_start(self): pass
    def loop_stop(self): pass
    def disconnect(self): pass
    def publish(self, *a, **k): pass
    def subscribe(self, *a, **k): pass


class _FakePaho:
    Client = _FakePahoClient


class MqttLogin(SiteYamlTest):
    def make(self, site_env):
        _FakePahoClient.instances.clear()
        with mock.patch.object(mqtt_module, "mqtt", _FakePaho):
            cfg = self.config([dummy("a", "")])
            cfg.settings.mqtt.enabled = True
            sup = supervisor_main_module().Supervisor(cfg, site_env=site_env)
        return sup, _FakePahoClient.instances[-1]

    def test_login_comes_from_site_yaml(self):
        _, client = self.make(SiteEnv(self.write_site_yaml()))
        self.assertEqual(client.login, (SECRETS["mqtt_user"], SECRETS["mqtt_password"]))
        self.assert_no_secret_anywhere()

    def test_no_login_in_site_yaml_means_no_login(self):
        def no_login(data):
            data["mqtt"]["user"], data["mqtt"]["password"] = "", ""
        _, client = self.make(SiteEnv(self.write_site_yaml(edit=no_login)))
        self.assertIsNone(client.login)

    def test_unreadable_site_yaml_connects_without_login_and_says_why(self):
        _, client = self.make(SiteEnv(self.write_site_yaml(mode=0o644)))
        self.assertIsNone(client.login)
        self.assertTrue(any("MQTT login not read" in line and "chmod 600" in line for line in self.logs.lines))
        self.assert_no_secret_anywhere()

    def test_workers_yaml_login_is_refused(self):
        for key in ("username", "password"):
            with self.assertRaises(ConfigError) as ctx:
                self.config([dummy("a", "")], mqtt={"enabled": False, key: "x"})
            self.assertIn("site.yaml (mqtt.user, mqtt.password)", str(ctx.exception))
        self.config([dummy("a", "")], mqtt={"enabled": False, "username": None, "password": None})

    def test_command_line_client_uses_the_site_yaml_login(self):
        _FakePahoClient.instances.clear()
        main = supervisor_main_module()
        with mock.patch.object(mqtt_module, "mqtt", _FakePaho):
            cfg = self.config([dummy("a", "")])
            cfg.settings.mqtt.enabled = True
            main._cli_mqtt(cfg, SiteEnv(self.write_site_yaml()))
        self.assertEqual(_FakePahoClient.instances[-1].login, (SECRETS["mqtt_user"], SECRETS["mqtt_password"]))


def supervisor_main_module():
    from tests.unit.test_supervisor_v2 import supervisor_main
    return supervisor_main


# ----------------------------------------------------------------------
# config validation of the new keys
# ----------------------------------------------------------------------
class ConfigKeysP5(SupervisorTest):
    def problem(self, workers, monitoring=None):
        with self.assertRaises(ConfigError) as ctx:
            self.config(workers, monitoring)
        return str(ctx.exception)

    def test_follows(self):
        self.assertIn("unknown worker 'nope'", self.problem([dummy("i", follows="nope")]))
        self.assertIn("itself", self.problem([dummy("i", follows="i")]))
        self.assertIn("disabled", self.problem([dummy("i", follows="c"), dummy("c", enabled=False)]))
        self.assertIn("two service workers", self.problem([dummy("i", follows="c"), dummy("c", mode="oneshot")]))
        self.assertIn("two service workers", self.problem([dummy("i", follows="c", mode="oneshot"), dummy("c")]))
        self.assertIn("one worker id", self.problem([dummy("i", follows=["c"]), dummy("c")]))
        self.assertIn("loop", self.problem([dummy("a", follows="b"), dummy("b", start_after=["a"])]))
        manager = self.config([dummy("i", follows="c"), dummy("c")])
        self.assertEqual([w.id for w in manager.start_order], ["c", "i"])

    def test_run_timeout(self):
        self.assertIn("only applies to mode oneshot or periodic", self.problem([dummy("a", run_timeout_seconds=5)]))
        self.assertIn("greater than 0", self.problem([dummy("a", mode="oneshot", run_timeout_seconds=0)]))
        self.config([dummy("a", mode="oneshot", run_timeout_seconds=60)])

    def test_quiet_window(self):
        for bad in ("2-3", "25:00-01:00", "02:00", "aa:bb-cc:dd"):
            self.assertIn("reload_quiet_window", self.problem([dummy("a")], {"reload_quiet_window": bad}))
        self.config([dummy("a")], {"reload_quiet_window": "02:00-03:30"})


if __name__ == "__main__":
    unittest.main()
