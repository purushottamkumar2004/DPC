"""Tests for Supervisor V2 (STATUS item 3, D-019, D-020, D-034, D-048, D-049).

Run from the repo root (needs PyYAML only; MQTT is replaced by a recorder):
    python3 -m unittest tests.unit.test_supervisor_v2 -v

Every "done when" letter of STATUS item 3 has a test class below:
    (a) oneshot / failing oneshot     OneshotMode
    (b) periodic                      PeriodicMode
    (c) queue_lag                     QueueLagHealth
    (d) start_after order             StartOrder
    (e) old + extended heartbeat      Heartbeats
    (f) restart_cost: high            RestartCost
and the env bridge (done when: a worker with env_from_site sees the DPC_* variables and no
secret shows up in a log or an MQTT message) is EnvBridge.

The tests start real dummy workers (supervisor/workers/dummy_worker.py) with times of a
fraction of a second, so the whole file takes about 30 seconds.
"""

import importlib.util
import io
import json
import logging
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SUPERVISOR_DIR = os.path.join(REPO_ROOT, "supervisor")
for path in (REPO_ROOT, SUPERVISOR_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from core.config import ConfigError, ConfigurationManager                 # noqa: E402
from core.process_manager import WorkerStatus                              # noqa: E402
from core.site_env import SiteEnv                                          # noqa: E402
from health.heartbeat import parse_heartbeat                               # noqa: E402

_spec = importlib.util.spec_from_file_location("dpc_supervisor_main", os.path.join(SUPERVISOR_DIR, "supervisor.py"))
supervisor_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(supervisor_main)

DUMMY = f"{sys.executable} {os.path.join(SUPERVISOR_DIR, 'workers', 'dummy_worker.py')}"
EXAMPLE_SITE_YAML = os.path.join(REPO_ROOT, "config", "examples", "site.example.yaml")

# Fast settings so a test needs seconds, not minutes.
FAST_MONITORING = {"check_interval_seconds": 0.2, "worker_timeout_seconds": 0.6,
                   "high_cost_timeout_seconds": 2.4, "startup_stagger_seconds": 0,
                   "start_after_timeout_seconds": 3, "status_publish_interval_seconds": 1}
NO_RETRY_WAIT = {"max_retries": 2, "retry_delay_seconds": 0.2, "reset_after_seconds": 60}


class FakeMqtt:
    """Stands in for MqttClient: records everything the Supervisor would publish."""
    prefix = "dpc"

    def __init__(self):
        self.events = []        # (worker_id, event, data)
        self.statuses = []      # (worker_id, status dict)
        self.raw = []           # every payload as the JSON text that would go on the wire

    def start(self): pass
    def stop(self): pass
    def subscribe(self, topic_filter, callback): pass
    def worker_topic(self, worker_id, kind): return f"dpc/worker/{worker_id}/{kind}"

    def publish_event(self, worker_id, event, **data):
        self.events.append((worker_id, event, data))
        self.raw.append(json.dumps({"worker_id": worker_id, "event": event, **data}, default=str))

    def publish_status(self, worker_id, status):
        self.statuses.append((worker_id, status))
        self.raw.append(json.dumps(status, default=str))

    def event_names(self, worker_id):
        return [e for w, e, _ in self.events if w == worker_id]


class _LogCapture(logging.Handler):
    """Collects every log line (with a traceback, if any) so a test can search it for secrets."""
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))

    def emit(self, record):
        self.lines.append(self.format(record))


class SupervisorTest(unittest.TestCase):
    """Builds a Supervisor from a dict in a temp folder and drives its loop by hand."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.mqtt = FakeMqtt()
        self.sup = None
        self.logs = _LogCapture()
        logging.getLogger().addHandler(self.logs)
        logging.getLogger().setLevel(logging.DEBUG)

    def tearDown(self):
        if self.sup is not None:
            self.sup.shutdown()
        logging.getLogger().removeHandler(self.logs)
        self._tmp.cleanup()

    # ---- helpers ----
    def path(self, name):
        return os.path.join(self.tmp, name)

    def config(self, workers, monitoring=None, **extra):
        data = {"monitoring": {**FAST_MONITORING, **(monitoring or {})}, "mqtt": {"enabled": False},
                "restart_policy": NO_RETRY_WAIT, "runtime": {"root": self.path("runtime")},
                "log_dir": self.path("logs"), "workers": workers, **extra}
        cfg_path = self.path("workers.yaml")
        with open(cfg_path, "w") as f:
            yaml.safe_dump(data, f)
        manager = ConfigurationManager(cfg_path, self.tmp)
        manager.load()
        return manager

    def build(self, workers, monitoring=None, site_env=None):
        self.sup = supervisor_main.Supervisor(self.config(workers, monitoring), mqtt=self.mqtt, site_env=site_env)
        return self.sup

    def run_until(self, condition, timeout=10.0, message="condition not reached"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.sup.tick()
            if condition():
                return
            time.sleep(0.05)
        self.fail(f"{message} within {timeout}s; statuses: "
                  f"{ {w.id: (w.status.value, w.message) for w in self.sup.workers.values()} }")

    def run_for(self, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.sup.tick()
            time.sleep(0.05)

    def worker(self, worker_id):
        return self.sup.workers[worker_id]

    def start_lines(self, name):
        """Lines written by the dummy workers; [] until the first child has written its line."""
        try:
            with open(self.path(name)) as f:
                return [line.split() for line in f.read().splitlines()]
        except FileNotFoundError:
            return []


def dummy(worker_id, args="", **extra):
    """A workers.yaml entry that runs the dummy worker."""
    return {"id": worker_id, "type": "dummy", "command": f"{DUMMY} --interval 0.1 {args}".strip(), **extra}


# ----------------------------------------------------------------------
# Config validation
# ----------------------------------------------------------------------
class ConfigKeys(SupervisorTest):
    def load_problem(self, workers, monitoring=None):
        with self.assertRaises(ConfigError) as ctx:
            self.config(workers, monitoring)
        return str(ctx.exception)

    def test_old_files_load_unchanged_with_service_defaults(self):
        manager = self.config([dummy("a")])
        w = manager.workers[0]
        self.assertEqual((w.mode, w.restart_cost, w.start_after, w.env_from_site, w.interval_s),
                         ("service", "normal", [], False, None))

    def test_mode_and_interval(self):
        self.assertIn("mode must be one of", self.load_problem([dummy("a", mode="daemon")]))
        self.assertIn("interval_s (required for mode periodic)", self.load_problem([dummy("a", mode="periodic")]))
        self.assertIn("interval_s only applies to mode periodic", self.load_problem([dummy("a", interval_s=5)]))
        self.assertIn("greater than 0", self.load_problem([dummy("a", mode="periodic", interval_s=0)]))
        self.assertIn("greater than 0", self.load_problem([dummy("a", mode="periodic", interval_s=True)]))
        self.config([dummy("a", mode="periodic", interval_s=30)])

    def test_restart_cost_and_env_from_site(self):
        self.assertIn("restart_cost must be one of", self.load_problem([dummy("a", restart_cost="huge")]))
        self.assertIn("env_from_site must be true or false", self.load_problem([dummy("a", env_from_site="yes")]))

    def test_high_cost_timeout_must_not_be_shorter(self):
        problem = self.load_problem([dummy("a")], {"worker_timeout_seconds": 30, "high_cost_timeout_seconds": 10})
        self.assertIn("high_cost_timeout_seconds must not be shorter", problem)

    def test_queue_lag_needs_path_and_limit(self):
        self.assertIn("health.path", self.load_problem([dummy("a", health={"type": "queue_lag", "max_oldest_age_seconds": 5})]))
        self.assertIn("max_oldest_age_seconds", self.load_problem([dummy("a", health={"type": "queue_lag", "path": "x"})]))
        self.assertIn("only apply to health.type queue_lag",
                      self.load_problem([dummy("a", health={"type": "process", "max_oldest_age_seconds": 5})]))

    def test_start_after_errors(self):
        self.assertIn("unknown worker 'nope'", self.load_problem([dummy("a", start_after=["nope"])]))
        self.assertIn("itself", self.load_problem([dummy("a", start_after=["a"])]))
        loop = self.load_problem([dummy("a", start_after=["b"]), dummy("b", start_after=["a"])])
        self.assertIn("start_after loop: a -> b -> a", loop)
        self.assertIn("must be a worker id or a list", self.load_problem([dummy("a", start_after=[1])]))

    def test_start_order_respects_start_after_and_keeps_file_order_otherwise(self):
        manager = self.config([dummy("algo", start_after=["capture", "light"]), dummy("mail"),
                               dummy("capture", start_after="light"), dummy("light")])
        self.assertEqual([w.id for w in manager.start_order], ["light", "capture", "algo", "mail"])
        self.assertEqual([w.id for w in manager.workers], ["algo", "mail", "capture", "light"])   # file order kept

    def test_start_after_naming_a_disabled_worker_is_ignored(self):
        manager = self.config([dummy("a", start_after=["off"]), dummy("off", enabled=False)])
        self.assertEqual(manager.workers[0].start_after, [])


# ----------------------------------------------------------------------
# (a) oneshot
# ----------------------------------------------------------------------
class OneshotMode(SupervisorTest):
    def test_exit_0_ends_as_stopped_and_is_not_restarted(self):
        self.build([dummy("light", "--exit-after 0.3 --log-start starts.txt".replace("starts.txt", self.path("starts.txt")),
                          mode="oneshot")])
        self.sup.start()
        self.run_until(lambda: self.worker("light").status == WorkerStatus.STOPPED and self.worker("light").completed_runs == 1,
                       message="oneshot did not end as STOPPED")
        self.run_for(1.5)                                          # long enough for a wrong restart to show
        light = self.worker("light")
        self.assertEqual((light.status, light.exit_code, light.completed_runs, light.restart_count),
                         (WorkerStatus.STOPPED, 0, 1, 0))
        self.assertEqual(len(self.start_lines("starts.txt")), 1, "a finished oneshot must not run again")
        self.assertEqual(self.mqtt.event_names("light"), ["worker_started", "worker_stopped"])
        self.assertEqual(self.mqtt.events[-1][2]["reason"], "completed")
        self.assertNotIn("worker_failure", self.mqtt.event_names("light"))

    def test_non_zero_exit_counts_as_a_failure_and_follows_the_retry_policy(self):
        self.build([dummy("light", "--exit-after 0.2 --exit-code 3", mode="oneshot",
                          restart={"max_retries": 2, "retry_delay_seconds": 0.2})])
        self.sup.start()
        self.run_until(lambda: self.worker("light").status == WorkerStatus.FAILED, message="oneshot did not reach FAILED")
        names = self.mqtt.event_names("light")
        self.assertEqual(names.count("worker_failure"), 3)         # first run + 2 retries
        self.assertEqual(names.count("worker_restarted"), 2)
        self.assertEqual(names[-1], "worker_failed")
        self.assertEqual(self.worker("light").completed_runs, 0)

    def test_oneshot_that_succeeds_on_a_retry_is_stopped_not_failed(self):
        marker = self.path("second_try")
        script = (f"{sys.executable} -c \"import os,sys; "
                  f"m='{marker}'; ok=os.path.exists(m); open(m,'w').close(); sys.exit(0 if ok else 5)\"")
        self.build([{"id": "light", "type": "dummy", "command": script, "mode": "oneshot"}])
        self.sup.start()
        self.run_until(lambda: self.worker("light").completed_runs == 1, message="retry did not complete")
        self.assertEqual(self.worker("light").status, WorkerStatus.STOPPED)
        self.assertEqual(self.worker("light").retry_count, 0)


# ----------------------------------------------------------------------
# (b) periodic
# ----------------------------------------------------------------------
class PeriodicMode(SupervisorTest):
    def test_runs_every_interval_without_overlap_or_restart_counting(self):
        self.build([dummy("tick", f"--exit-after 0.2 --log-start {self.path('ticks.txt')}", mode="periodic", interval_s=1.0)])
        self.sup.start()
        self.run_until(lambda: self.worker("tick").completed_runs >= 3, timeout=15, message="fewer than 3 runs")
        times = [float(t) for _, t in self.start_lines("ticks.txt")]
        gaps = [b - a for a, b in zip(times, times[1:])]
        for gap in gaps[:2]:
            self.assertAlmostEqual(gap, 1.0, delta=0.5, msg=f"runs not spaced by interval_s: {gaps}")
        tick = self.worker("tick")
        self.assertEqual(tick.status, WorkerStatus.STOPPED)        # between runs: idle, not an error
        self.assertIsNotNone(tick.next_run_at)
        self.assertEqual(tick.restart_count, 0)
        self.assertNotIn("worker_failure", self.mqtt.event_names("tick"))
        status = [s for w, s in self.mqtt.statuses if w == "tick"][-1]
        self.assertEqual(status["mode"], "periodic")

    def test_failed_runs_are_events_and_the_next_run_is_the_retry(self):
        self.build([dummy("tick", f"--exit-after 0.1 --exit-code 2 --log-start {self.path('ticks.txt')}",
                          mode="periodic", interval_s=0.6)])
        self.sup.start()
        self.run_until(lambda: len(self.start_lines("ticks.txt")) >= 5, timeout=15, message="fewer than 5 runs")
        tick = self.worker("tick")
        self.assertNotEqual(tick.status, WorkerStatus.FAILED, "a periodic worker must keep trying on a 24x7 site")
        self.assertGreaterEqual(tick.consecutive_failures, 3)
        self.assertGreaterEqual(self.mqtt.event_names("tick").count("worker_failure"), 3)
        self.assertNotIn("worker_failed", self.mqtt.event_names("tick"))

    def test_a_run_that_cannot_start_does_not_spin(self):
        self.build([{"id": "tick", "type": "dummy", "command": "definitely_not_a_program_xyz",
                     "mode": "periodic", "interval_s": 5}])
        self.sup.start()
        self.run_for(1.5)
        self.assertEqual(self.mqtt.event_names("tick").count("worker_failure"), 1)    # one try, then wait interval_s
        self.assertGreater(self.worker("tick").next_run_at, time.time() + 3)

    def test_stop_command_stops_the_schedule_and_start_runs_it_now(self):
        self.build([dummy("tick", f"--exit-after 0.1 --log-start {self.path('ticks.txt')}", mode="periodic", interval_s=0.5)])
        self.sup.start()
        self.run_until(lambda: self.worker("tick").completed_runs >= 1, message="no first run")
        self.sup._run_command(self.worker("tick"), "stop", "test")
        runs = len(self.start_lines("ticks.txt"))
        self.run_for(1.5)
        self.assertEqual(len(self.start_lines("ticks.txt")), runs, "a stopped periodic worker must not run")
        self.sup._run_command(self.worker("tick"), "start", "test")
        self.run_until(lambda: len(self.start_lines("ticks.txt")) > runs, message="start command did not run it")
        self.run_until(lambda: self.worker("tick").next_run_at is not None, message="schedule did not resume")


# ----------------------------------------------------------------------
# (c) queue_lag
# ----------------------------------------------------------------------
class QueueLagHealth(SupervisorTest):
    def make_inf(self):
        self.inf = self.path("INF")
        os.makedirs(self.inf)

    def add_frame(self, name, age_seconds):
        path = os.path.join(self.inf, name)
        open(path, "w").close()
        old = time.time() - age_seconds
        os.utime(path, (old, old))
        return path

    def algo(self, **health):
        return dummy("algo", "", health={"type": "queue_lag", "path": self.inf, "max_oldest_age_seconds": 5,
                                         "startup_grace_seconds": 0, **health})

    def test_unhealthy_when_the_oldest_file_is_too_old_and_healthy_again_when_drained(self):
        self.make_inf()
        self.build([self.algo()])
        check = self.worker("algo").health_check
        self.assertTrue(check.check(self.worker("algo")).healthy, "an empty INF is an idle algorithm, not a broken one")
        frame = self.add_frame("2026_10_18_10_00_00_000001.jpg", age_seconds=2)
        self.add_frame("2026_10_18_10_00_01_000001.jpg", age_seconds=1)
        self.assertTrue(check.check(self.worker("algo")).healthy, "lag below the limit")
        os.utime(frame, (time.time() - 60, time.time() - 60))
        result = check.check(self.worker("algo"))
        self.assertFalse(result.healthy)
        self.assertIn("queue lag", result.message)
        self.assertIn("2 file(s) waiting", result.message)
        for name in os.listdir(self.inf):
            os.remove(os.path.join(self.inf, name))
        self.assertTrue(check.check(self.worker("algo")).healthy, "drained folder recovers")

    def test_missing_folder_and_non_frame_files(self):
        self.make_inf()
        self.build([self.algo()])
        check = self.worker("algo").health_check
        open(os.path.join(self.inf, "notes.txt"), "w").close()          # not a frame: ignored
        self.assertTrue(check.check(self.worker("algo")).healthy)
        os.remove(os.path.join(self.inf, "notes.txt"))
        os.rmdir(self.inf)
        self.assertFalse(check.check(self.worker("algo")).healthy)

    def test_monitor_restarts_only_when_the_lag_lasts_and_recovers_without_restart_when_it_drains(self):
        self.make_inf()
        stuck = self.add_frame("2026_10_18_10_00_00_000001.jpg", age_seconds=60)
        self.build([dummy("algo", "", health={"type": "queue_lag", "path": self.inf, "max_oldest_age_seconds": 5,
                                              "startup_grace_seconds": 0})],
                   monitoring={"worker_timeout_seconds": 1.0, "high_cost_timeout_seconds": 1.0})
        self.sup.start()
        self.run_until(lambda: self.worker("algo").failing_since is not None, message="lag not noticed")
        self.assertIn("queue lag", self.worker("algo").message)
        os.remove(stuck)                                                  # the algorithm caught up
        self.run_until(lambda: self.worker("algo").failing_since is None and self.worker("algo").status == WorkerStatus.RUNNING,
                       message="did not recover")
        self.assertEqual(self.worker("algo").restart_count, 0)
        self.assertNotIn("worker_failure", self.mqtt.event_names("algo"))

        self.add_frame("2026_10_18_10_05_00_000001.jpg", age_seconds=60)  # stuck again, and stays stuck
        self.run_until(lambda: "worker_failure" in self.mqtt.event_names("algo"), message="lag did not cause a restart")
        failure = [d for w, e, d in self.mqtt.events if e == "worker_failure"][0]
        self.assertIn("queue lag", failure["reason"])

    def test_queue_lag_and_heartbeat_both_needed_with_require_heartbeat(self):
        self.make_inf()
        self.build([self.algo(require_heartbeat=True, max_age_seconds=30)])
        algo = self.worker("algo")
        algo.started_at = time.time() - 10
        no_beat = algo.health_check.check(algo)
        self.assertFalse(no_beat.healthy)
        self.assertIn("no heartbeat", no_beat.message)
        self.sup._on_health("dpc/worker/algo/health", {"worker_id": "algo", "healthy": True})
        self.assertTrue(algo.health_check.check(algo).healthy, "fresh heartbeat and empty queue")
        self.add_frame("2026_10_18_10_00_00_000001.jpg", age_seconds=60)
        both = algo.health_check.check(algo)
        self.assertFalse(both.healthy)
        self.assertIn("queue lag", both.message)
        self.sup._on_health("dpc/worker/algo/health", {"worker_id": "algo", "healthy": False})
        self.assertIn("unhealthy", algo.health_check.check(algo).message)

    def test_without_require_heartbeat_a_legacy_algorithm_needs_no_heartbeat(self):
        self.make_inf()
        self.build([self.algo()])
        algo = self.worker("algo")
        algo.started_at = time.time() - 10
        self.assertTrue(algo.health_check.check(algo).healthy)


# ----------------------------------------------------------------------
# (d) start_after
# ----------------------------------------------------------------------
class StartOrder(SupervisorTest):
    def started_order(self):
        return [w for w, e, _ in self.mqtt.events if e == "worker_started"]

    def test_workers_start_in_start_after_order_and_wait_for_ready(self):
        log = self.path("order.txt")
        self.build([dummy("algo", f"--log-start {log}", start_after=["capture"]),
                    dummy("capture", f"--log-start {log}", start_after=["light"]),
                    dummy("light", f"--exit-after 0.5 --log-start {log}", mode="oneshot")])
        self.sup.start()
        self.run_until(lambda: len(self.started_order()) == 3, message="not all workers started")
        self.assertEqual(self.started_order(), ["light", "capture", "algo"])
        self.run_until(lambda: len(self.start_lines("order.txt")) == 3, message="algo did not write its line")
        self.assertEqual([name for name, _ in self.start_lines("order.txt")], ["light", "capture", "algo"])
        light, capture, algo = self.worker("light"), self.worker("capture"), self.worker("algo")
        self.assertGreaterEqual(capture.started_at, light.started_at + 0.5, "capture must wait for light to finish")
        self.assertEqual(light.exit_code, 0)
        self.run_until(lambda: capture.status == WorkerStatus.RUNNING, message="capture not running")
        self.assertLessEqual(capture.started_at, algo.started_at)
        self.assertEqual(list(self.sup.workers), ["light", "capture", "algo"])       # shutdown walks this backwards

    def test_a_waiting_worker_shows_why_and_independent_workers_are_not_held_back(self):
        self.build([dummy("late", "", start_after=["slow"]), dummy("slow", "--exit-after 1.5", mode="oneshot"),
                    dummy("other", "")])
        self.sup.start()
        self.run_until(lambda: "other" in self.started_order(), message="independent worker held back")
        self.assertNotIn("late", self.started_order())
        self.assertIn("waiting for slow", self.worker("late").message)

    def test_dependency_that_never_gets_ready_is_waited_for_then_started_anyway(self):
        self.build([dummy("b", "", start_after=["a"]),
                    dummy("a", "", health={"type": "heartbeat", "max_age_seconds": 5, "startup_grace_seconds": 60})],
                   monitoring={"start_after_timeout_seconds": 1.0})
        self.sup.start()
        began = time.time()
        self.run_until(lambda: "b" in self.started_order(), message="b never started")
        self.assertGreaterEqual(time.time() - began, 0.9, "b must wait the full start_after timeout")
        unmet = [d for w, e, d in self.mqtt.events if w == "b" and e == "worker_started"][0]
        self.assertIn("start_after not ready", unmet["start_after_unmet"])

    def test_failed_dependency_does_not_keep_the_site_blind(self):
        self.build([dummy("light", "--exit-after 0.1 --exit-code 1", mode="oneshot",
                          restart={"max_retries": 0}),
                    dummy("capture", "", start_after=["light"])])
        self.sup.start()
        self.run_until(lambda: "capture" in self.started_order(), message="capture never started")
        self.assertEqual(self.worker("light").status, WorkerStatus.FAILED)
        unmet = [d for w, e, d in self.mqtt.events if w == "capture" and e == "worker_started"][0]
        self.assertIn("FAILED: light", unmet["start_after_unmet"])

    def test_start_command_overrules_a_waiting_start_after(self):
        self.build([dummy("late", "", start_after=["slow"]), dummy("slow", "--exit-after 30", mode="oneshot")])
        self.sup.start()
        self.run_for(0.5)
        self.assertNotIn("late", self.started_order())
        self.sup._run_command(self.worker("late"), "start", "operator")
        self.assertIn("late", self.started_order())


# ----------------------------------------------------------------------
# (e) heartbeats
# ----------------------------------------------------------------------
class Heartbeats(SupervisorTest):
    OLD = {"worker_id": "algo", "healthy": True}
    EXTENDED = {"worker_id": "algo", "healthy": True, "pid": 4242, "version": "v0.4", "config_hash": "ab12cd",
                "fps": 3.9, "last_frame_age_s": 0.21, "inf_queue_depth": 2, "something_new": {"x": 1}}

    def test_parse_old_and_extended(self):
        old = parse_heartbeat(self.OLD)
        self.assertTrue(old.healthy)
        self.assertEqual(old.info, {})
        ext = parse_heartbeat(self.EXTENDED)
        self.assertEqual(ext.info, {"pid": 4242, "version": "v0.4", "config_hash": "ab12cd", "fps": 3.9,
                                    "last_frame_age_s": 0.21, "inf_queue_depth": 2})     # unknown field dropped
        self.assertTrue(parse_heartbeat({}).healthy, "an empty payload counts as healthy, as before")

    def test_parse_rejects_junk_and_clips_text(self):
        self.assertIsNone(parse_heartbeat("hello"))
        self.assertIsNone(parse_heartbeat({"healthy": "yes"}))
        ext = parse_heartbeat({"healthy": True, "pid": True, "fps": "fast", "version": "v" * 500})
        self.assertEqual(list(ext.info), ["version"])
        self.assertEqual(len(ext.info["version"]), 64)

    def test_the_supervisor_accepts_both_payloads_for_a_heartbeat_worker(self):
        self.build([dummy("algo", "", health={"type": "heartbeat", "max_age_seconds": 30, "startup_grace_seconds": 0})])
        algo = self.worker("algo")
        algo.started_at = time.time() - 5
        self.assertFalse(algo.health_check.check(algo).healthy)
        for payload in (self.OLD, self.EXTENDED):
            self.sup.heartbeats.clear()
            self.sup._on_health("dpc/worker/algo/health", payload)
            self.assertTrue(algo.health_check.check(algo).healthy, f"not accepted: {payload}")
        status = self.sup.process_manager.get_status(algo)
        self.assertEqual(status["heartbeat"]["fps"], 3.9)
        self.assertNotIn("something_new", status["heartbeat"])

    def test_a_heartbeat_from_before_the_restart_does_not_count(self):
        self.build([dummy("algo", "", health={"type": "heartbeat", "max_age_seconds": 30, "startup_grace_seconds": 0})])
        algo = self.worker("algo")
        self.sup._on_health("dpc/worker/algo/health", self.OLD)
        algo.started_at = time.time() + 1                        # the worker was (re)started after that heartbeat
        self.assertFalse(algo.health_check.check(algo).healthy)

    def test_not_a_heartbeat_is_ignored_without_an_error(self):
        self.build([dummy("algo", "")])
        self.sup._on_health("dpc/worker/algo/health", ["not", "a", "dict"])
        self.assertNotIn("algo", self.sup.heartbeats)


# ----------------------------------------------------------------------
# (f) restart_cost
# ----------------------------------------------------------------------
class RestartCost(SupervisorTest):
    def stale_heartbeat_worker(self, worker_id, **extra):
        return dummy(worker_id, "", health={"type": "heartbeat", "max_age_seconds": 5, "startup_grace_seconds": 0}, **extra)

    def test_high_cost_worker_must_fail_longer_than_a_normal_one(self):
        self.build([self.stale_heartbeat_worker("normal"), self.stale_heartbeat_worker("algo", restart_cost="high")])
        self.sup.start()
        began = time.time()
        self.run_until(lambda: "worker_failure" in self.mqtt.event_names("normal"), message="normal never restarted")
        normal_after = time.time() - began
        self.assertLess(normal_after, 2.0)                                        # worker_timeout_seconds = 0.6
        self.assertNotIn("worker_failure", self.mqtt.event_names("algo"), "high cost must still be waiting")
        self.assertIsNotNone(self.worker("algo").failing_since)
        self.run_until(lambda: "worker_failure" in self.mqtt.event_names("algo"), timeout=10, message="algo never restarted")
        self.assertGreaterEqual(time.time() - began, 2.3)                         # high_cost_timeout_seconds = 2.4

    def test_a_dead_high_cost_worker_is_still_restarted_at_once(self):
        self.build([dummy("algo", "--crash-after 0.2", restart_cost="high")])
        self.sup.start()
        began = time.time()
        self.run_until(lambda: "worker_failure" in self.mqtt.event_names("algo"), message="crash not handled")
        self.assertLess(time.time() - began, 2.0, "a dead process is certain; only the health timeout is longer")


# ----------------------------------------------------------------------
# env bridge
# ----------------------------------------------------------------------
SECRET_VALUES = {
    "db_user": "dpc_user_Zq81", "db_password": "dbpass-Kd93-secret", "tenant": "tenant-Lm52-secret",
    "client_id": "client-Pw07-secret", "client_secret": "graph-Xv64-secret",
    "mqtt_user": "mqttuser-Hn18", "mqtt_password": "mqttpass-Ty46-secret",
}


class EnvBridge(SupervisorTest):
    def write_site_yaml(self, mode=0o600, **overrides):
        with open(EXAMPLE_SITE_YAML) as f:
            data = yaml.safe_load(f)
        data["site"] = {"id": "unit_test_site", "name": "Unit Test DPC"}
        data["paths"]["root"] = self.path("INSIGHTZZ")
        data["db"]["user"], data["db"]["password"] = SECRET_VALUES["db_user"], SECRET_VALUES["db_password"]
        data["mqtt"]["user"], data["mqtt"]["password"] = SECRET_VALUES["mqtt_user"], SECRET_VALUES["mqtt_password"]
        data["graph"] = {"tenant_id": SECRET_VALUES["tenant"], "client_id": SECRET_VALUES["client_id"],
                         "client_secret": SECRET_VALUES["client_secret"]}      # v3: shared graph (D-055)
        data.update(overrides)
        path = self.path("site.yaml")
        with open(path, "w") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        os.chmod(path, mode)
        return path

    def everything_that_left_the_supervisor(self):
        """All logs, all MQTT payloads, the Supervisor's own log files and the worker log files."""
        text = "\n".join(self.logs.lines) + "\n".join(self.mqtt.raw)
        for folder, _, files in os.walk(self.path("logs")):
            for name in files:
                with open(os.path.join(folder, name), errors="replace") as f:
                    text += f.read()
        return text

    def assert_no_secret_anywhere(self):
        text = self.everything_that_left_the_supervisor()
        for label, value in SECRET_VALUES.items():
            self.assertNotIn(value, text, f"secret '{label}' leaked")

    def test_worker_with_env_from_site_sees_the_dpc_variables(self):
        dump = self.path("env_cleanup.txt")
        dump_other = self.path("env_other.txt")
        self.build([dummy("cleanup", f"--exit-after 0.3 --dump-env {dump} --print-env-names", mode="oneshot", env_from_site=True),
                    dummy("other", f"--exit-after 0.3 --dump-env {dump_other}", mode="oneshot")],
                   site_env=SiteEnv(self.write_site_yaml()))
        self.sup.start()
        self.run_until(lambda: self.worker("cleanup").completed_runs and self.worker("other").completed_runs,
                       message="workers did not finish")
        seen = dict(line.split("=", 1) for line in open(dump).read().splitlines())
        self.assertEqual(seen["DPC_DB_USER"], SECRET_VALUES["db_user"])
        self.assertEqual(seen["DPC_DB_PASS"], SECRET_VALUES["db_password"])
        self.assertEqual(seen["DPC_MS_TENANT_ID"], SECRET_VALUES["tenant"])
        self.assertEqual(seen["DPC_MS_CLIENT_ID"], SECRET_VALUES["client_id"])
        self.assertEqual(seen["DPC_MS_CLIENT_SECRET"], SECRET_VALUES["client_secret"])
        self.assertEqual(seen["DPC_MQTT_USER"], SECRET_VALUES["mqtt_user"])
        self.assertEqual(seen["DPC_MQTT_PASS"], SECRET_VALUES["mqtt_password"])
        self.assertEqual(seen["DPC_MAIL_SENDER"], "reports@example.com")
        self.assertEqual(seen["DPC_DISK_REPORT_TO"], "alerts@example.com")
        self.assertEqual(seen["DPC_SITE_NAME"], "Unit Test DPC")
        self.assertNotIn("DPC_SMTP_PASSWORD", seen, "a Graph site has no SMTP password")
        self.assertEqual(seen["DPC_WORKER_ID"], "cleanup")                          # the Supervisor's own variables stay
        self.assertEqual(seen["DPC_MQTT_HOST"], "localhost")
        other = dict(line.split("=", 1) for line in open(dump_other).read().splitlines())
        self.assertNotIn("DPC_DB_PASS", other, "a worker that did not ask must not get the secrets")
        self.assertNotIn("DPC_MS_CLIENT_SECRET", other)
        self.assert_no_secret_anywhere()

    def test_env_in_workers_yaml_wins_over_site_yaml(self):
        dump = self.path("env.txt")
        self.build([dummy("cleanup", f"--exit-after 0.3 --dump-env {dump}", mode="oneshot", env_from_site=True,
                          env={"DPC_SITE_NAME": "from workers.yaml"})], site_env=SiteEnv(self.write_site_yaml()))
        self.sup.start()
        self.run_until(lambda: self.worker("cleanup").completed_runs, message="did not finish")
        seen = dict(line.split("=", 1) for line in open(dump).read().splitlines())
        self.assertEqual(seen["DPC_SITE_NAME"], "from workers.yaml")

    def test_site_yaml_is_read_again_at_every_start_so_a_rotated_password_needs_only_a_worker_restart(self):
        dump = self.path("env.txt")
        site_path = self.write_site_yaml()
        self.build([dummy("cleanup", f"--exit-after 30 --dump-env {dump}", env_from_site=True)], site_env=SiteEnv(site_path))
        self.sup.start()
        self.run_until(lambda: os.path.exists(dump) and "DPC_DB_PASS" in open(dump).read(), message="first start")
        self.assertIn(SECRET_VALUES["db_password"], open(dump).read())
        os.remove(dump)
        self.write_site_yaml(db={"host": "localhost", "port": 3306, "name": "example_db", "health_table": "health_status",
                                 "user": SECRET_VALUES["db_user"], "password": "rotated-Rr29-secret"})
        self.sup._run_command(self.worker("cleanup"), "restart", "password rotated")
        self.run_until(lambda: os.path.exists(dump) and "DPC_DB_PASS" in open(dump).read(), message="after restart")
        self.assertIn("DPC_DB_PASS=rotated-Rr29-secret", open(dump).read())
        self.assertNotIn("rotated-Rr29-secret", self.everything_that_left_the_supervisor())

    def test_unreadable_site_yaml_fails_that_worker_only_and_leaks_nothing(self):
        bad = self.write_site_yaml(mode=0o644)                                       # loader refuses: not chmod 600
        self.build([dummy("cleanup", "", env_from_site=True, restart={"max_retries": 0}), dummy("other", "")],
                   site_env=SiteEnv(bad))
        self.sup.start()
        self.run_until(lambda: self.worker("cleanup").status == WorkerStatus.FAILED and "other" in
                       [w for w, e, _ in self.mqtt.events if e == "worker_started"], message="expected FAILED + other started")
        self.assertIn("chmod 600", self.worker("cleanup").message)
        self.assertIn(self.worker("other").status, (WorkerStatus.STARTING, WorkerStatus.RUNNING),
                      "one worker's bad site.yaml must not stop the others")
        self.assertIsNone(self.worker("cleanup").pid, "the worker must not run without its environment")
        self.assert_no_secret_anywhere()

    def test_broken_yaml_with_a_secret_on_the_bad_line_does_not_leak_it(self):
        path = self.write_site_yaml()
        with open(path, "a") as f:
            f.write(f"\n  password: {SECRET_VALUES['db_password']}: [broken\n")
        self.build([dummy("cleanup", "", env_from_site=True, restart={"max_retries": 0})], site_env=SiteEnv(path))
        self.sup.start()
        self.run_until(lambda: self.worker("cleanup").status == WorkerStatus.FAILED, message="expected FAILED")
        self.assertIn("not valid YAML", self.worker("cleanup").message)
        self.assert_no_secret_anywhere()

    def test_unexpected_exception_is_reported_by_type_only(self):
        class Exploding(SiteEnv):
            def _import_loader(self):
                def boom(path):
                    raise ValueError(f"surprise containing {SECRET_VALUES['db_password']}")
                return boom, None, type("Never", (Exception,), {})
        self.build([dummy("cleanup", "", env_from_site=True, restart={"max_retries": 0})], site_env=Exploding())
        self.sup.start()
        self.run_until(lambda: self.worker("cleanup").status == WorkerStatus.FAILED, message="expected FAILED")
        self.assertIn("ValueError", self.worker("cleanup").message)
        self.assert_no_secret_anywhere()

    def test_check_config_reports_site_yaml_problems_without_values(self):
        cfg = self.config([dummy("cleanup", "", env_from_site=True)])
        bad = self.write_site_yaml(mode=0o644)
        out = io.StringIO()
        argv = ["supervisor.py", "check-config", "-c", self.path("workers.yaml"), "--site-config", bad]
        with redirect_stdout(out):
            old, sys.argv = sys.argv, argv
            try:
                code = supervisor_main.main()
            finally:
                sys.argv = old
        self.assertEqual(code, 2)
        self.assertIn("Site config error (needed by cleanup)", out.getvalue())
        for value in SECRET_VALUES.values():
            self.assertNotIn(value, out.getvalue())

        good = self.write_site_yaml()
        out = io.StringIO()
        with redirect_stdout(out):
            old, sys.argv = sys.argv, argv[:-1] + [good]
            try:
                code = supervisor_main.main()
            finally:
                sys.argv = old
        self.assertEqual(code, 0)
        self.assertIn("DPC_* variables are exported to: cleanup", out.getvalue())
        self.assertNotIn(SECRET_VALUES["db_password"], out.getvalue())


if __name__ == "__main__":
    unittest.main()
