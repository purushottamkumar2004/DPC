"""Tests for STATUS F82: with models.auto_update: install the Supervisor switches a model after the
transfer worker's model_downloaded event, by the D-087 switch (D-060, D-087, D-105).

Run from the repo root:  python3 -m unittest tests.unit.test_supervisor_f82 -v

Real: the Supervisor (in this process), site.yaml, packages in the real format (real verify_package),
the child `supervisor.py model switch ...` and the reload channel (pid file + SIGHUP back to this
process). Fake: the self-test (from the P11 tests) and MQTT (events are fed to the subscriber callback).
"""

import json
import os
import signal
import subprocess
import sys
import time
import unittest

import yaml

from tests.unit.test_supervisor_p11 import ModelTest                        # noqa: F401 (sys.path)
from tests.unit.test_supervisor_f28 import MOSQUITTO, free_port, paho
from tests.unit.test_supervisor_v2 import SUPERVISOR_DIR, dummy, supervisor_main
from common.config import model_package as mp                               # noqa: E402
from core.process_manager import WorkerStatus                               # noqa: E402
from core.reload_channel import ReloadChannel                               # noqa: E402
from core.site_env import SiteEnv                                           # noqa: E402

TOPIC = "dpc/worker/transfer/event"


class AutoInstallTest(ModelTest):
    def setUp(self):
        super().setUp()
        self._old_hup = signal.getsignal(signal.SIGHUP)

    def tearDown(self):
        if self.sup is not None:
            self.sup.auto_install.stop()
            if self.sup.reload_channel:
                self.sup.reload_channel.remove_pid()
        signal.signal(signal.SIGHUP, self._old_hup)
        super().tearDown()

    def start(self, auto_update="install", monitoring=None, right_model=None):
        site = self.site(right_model=right_model)
        workers = [dummy(w, "", restart_cost="high") for w in ("inspection_left", "inspection_right")]
        cfg = self.config(workers, monitoring, models={"auto_update": auto_update},
                          runtime={"root": os.path.join(self.root, "run")})
        self.sup = supervisor_main.Supervisor(cfg, mqtt=self.mqtt, site_env=SiteEnv(site))
        self.sup.auto_install.extra_args = ["--selftest-script", self.selftest]
        self.sup.reload_channel = ReloadChannel(cfg.settings.runtime_root)    # as cmd_run does
        self.sup.reload_channel.write_pid()
        signal.signal(signal.SIGHUP, self.sup.request_reload)
        self.sup.start()
        self.run_until(lambda: all(w.status == WorkerStatus.RUNNING for w in self.sup.workers.values()),
                       message="inspection not running")
        return {w.id: w.pid for w in self.sup.workers.values()}

    def downloaded(self, name, version):
        self.sup.auto_install.on_transfer_event(TOPIC, {"event": "model_downloaded", "name": name,
                                                        "version": version, "worker_id": "transfer"})

    def settle(self, timeout=60):
        self.run_until(lambda: not self.sup.auto_install.busy(), timeout=timeout, message="switch not finished")
        self.run_for(0.5)                                      # the restart request is served in a tick

    def skipped(self):
        return [p for t, p in self.mqtt.published if t == "dpc/supervisor/event"
                and p.get("event") == "model_auto_switch_skipped"]

    def log_text(self):
        return "\n".join(self.logs.lines)


class InstallMode(AutoInstallTest):
    def test_download_is_switched_by_the_d087_switch(self):
        self.install("v1", "v2", current="v1")
        pids = self.start()
        self.downloaded(self.MODEL, "v2")
        self.settle()
        self.assertEqual(mp.read_current(self.model_root()), "v2")
        self.assertEqual(self.manager(self.site()).status()[0]["previous"], "v1")
        self.assertEqual(len(self.selftest_calls()), 1, "the D-087 self-test ran")
        self.assertIn(f"[model switch {self.MODEL} v2]", self.log_text())
        self.assertIn(f"automatic switch of {self.MODEL} v2 done", self.log_text())

    def test_two_downloads_are_switched_one_after_the_other(self):
        self.install("v1", "v2", "v3", current="v1")
        self.start()
        self.downloaded(self.MODEL, "v2")
        self.downloaded(self.MODEL, "v3")
        self.settle(timeout=90)
        self.assertEqual(mp.read_current(self.model_root()), "v3")
        self.assertEqual(self.manager(self.site()).status()[0]["previous"], "v2")
        self.assertEqual([c[c.index("--version") + 1] for c in self.selftest_calls()], ["v2", "v3"])

    def test_broken_package_is_refused_and_nothing_restarts(self):
        self.install("v1", current="v1")
        self.package("v2", broken=True)
        pids = self.start()
        self.downloaded(self.MODEL, "v2")
        self.settle()
        self.assertEqual(mp.read_current(self.model_root()), "v1")
        self.assertEqual({w.id: w.pid for w in self.sup.workers.values()}, pids)
        self.assertFalse(any(w.config_pending for w in self.sup.workers.values()))
        self.assertIn("refused or failed (exit 1)", self.log_text())
        self.assertIn("checksum does not match", self.log_text())

    def test_failing_selftest_is_refused(self):
        self.install("v1", "v2", current="v1")
        open(self.fail_flag + "_v2", "w").close()
        self.start()
        self.downloaded(self.MODEL, "v2")
        self.settle()
        self.assertEqual(mp.read_current(self.model_root()), "v1")
        self.assertIn("self-test failed", self.log_text())


@unittest.skipUnless(MOSQUITTO and paho, "needs mosquitto and paho-mqtt")
class EndToEnd(ModelTest):
    """A real `supervisor.py run` on a real broker; model_downloaded published over MQTT the way the
    transfer worker does. The launcher is named supervisor.py (the reload channel only signals a
    `supervisor.py run`) and only adds --selftest-script <fake> to the automatic switch."""

    LAUNCHER = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("dpc_supervisor_main", {real!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
original = mod.Supervisor.__init__
def init(self, *args, **kwargs):
    original(self, *args, **kwargs)
    self.auto_install.extra_args = ["--selftest-script", {selftest!r}]
mod.Supervisor.__init__ = init
sys.exit(mod.main())
"""

    def setUp(self):
        super().setUp()
        self.port = free_port()
        conf = self.path("mosquitto.conf")
        with open(conf, "w") as f:
            f.write(f"listener {self.port} 127.0.0.1\nallow_anonymous true\npersistence false\n")
        self.broker = subprocess.Popen([MOSQUITTO, "-c", conf], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        launcher_dir = self.path("launcher")
        os.makedirs(launcher_dir)
        self.launcher = os.path.join(launcher_dir, "supervisor.py")
        with open(self.launcher, "w") as f:
            f.write(self.LAUNCHER.format(real=os.path.join(SUPERVISOR_DIR, "supervisor.py"), selftest=self.selftest))
        self.proc = None

    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(30)
        self.broker.terminate()
        self.broker.wait(10)
        super().tearDown()

    def run_site(self, quiet_window=None, right_model=None):
        site = self.site(right_model=right_model)
        monitoring = {"check_interval_seconds": 0.5, "startup_stagger_seconds": 0, "status_publish_interval_seconds": 1}
        if quiet_window:
            monitoring["reload_quiet_window"] = quiet_window
        starts = self.path("starts.txt")
        data = {"mqtt": {"enabled": True, "host": "127.0.0.1", "port": self.port},
                "models": {"auto_update": "install"}, "monitoring": monitoring,
                "runtime": {"root": os.path.join(self.root, "run")}, "log_dir": os.path.join(self.root, "logs"),
                "workers": [dict(dummy(w, f"--log-start {starts}"), restart_cost="high")
                            for w in ("inspection_left", "inspection_right")]}
        workers = self.path("workers.yaml")
        with open(workers, "w") as f:
            yaml.safe_dump(data, f)
        self.proc = subprocess.Popen([sys.executable, self.launcher, "run", "-c", workers, "--site-config", site],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait_for(lambda: len(self.starts()) == 2 and os.path.exists(os.path.join(self.root, "run", "supervisor.pid")),
                      "inspection not started")
        time.sleep(1.5)                                    # connected and subscribed

    def starts(self):
        try:
            with open(self.path("starts.txt")) as f:
                return [line.split()[0] for line in f.read().splitlines()]
        except FileNotFoundError:
            return []

    def wait_for(self, condition, what, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if condition():
                return
            time.sleep(0.2)
        log = os.path.join(self.root, "logs", "supervisor.log")
        tail = open(log).read()[-3000:] if os.path.exists(log) else ""
        self.fail(f"{what}\n--- supervisor.log ---\n{tail}")

    def publish_downloaded(self, name, version):
        client = (paho.Client(paho.CallbackAPIVersion.VERSION2) if hasattr(paho, "CallbackAPIVersion") else paho.Client())
        client.connect("127.0.0.1", self.port)
        client.loop_start()
        message = {"event": "model_downloaded", "name": name, "version": version, "worker_id": "transfer"}
        client.publish("dpc/worker/transfer/event", json.dumps(message), qos=1).wait_for_publish()
        client.loop_stop()
        client.disconnect()

    def log_text(self):
        with open(os.path.join(self.root, "logs", "supervisor.log")) as f:
            return f.read()

    def window_now(self):
        now = time.localtime()
        start = (now.tm_hour * 60 + now.tm_min - 1) % 1440
        end = (now.tm_hour * 60 + now.tm_min + 3) % 1440
        return f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"

    def test_install_without_quiet_window_switches_and_inspection_waits(self):
        self.install("v1", "v2", current="v1")
        self.run_site()
        self.publish_downloaded(self.MODEL, "v2")
        self.wait_for(lambda: "automatic switch of dpc_model v2 done" in self.log_text(), "no automatic switch")
        self.assertEqual(mp.read_current(self.model_root()), "v2")
        self.wait_for(lambda: "restart waiting for a quiet moment" in self.log_text() or
                      "waiting for the quiet window" in self.log_text(), "sides not marked pending")
        time.sleep(1.0)
        self.assertEqual(sorted(self.starts()), ["inspection_left", "inspection_right"], "no --confirm: no restart")

    def test_install_inside_the_quiet_window_restarts_both_sides_once(self):
        self.install("v1", "v2", current="v1")
        self.run_site(quiet_window=self.window_now())
        self.publish_downloaded(self.MODEL, "v2")
        self.wait_for(lambda: len(self.starts()) == 4, "sides not restarted")
        time.sleep(1.5)
        self.assertEqual(sorted(self.starts()), ["inspection_left"] * 2 + ["inspection_right"] * 2, "once each")
        self.assertEqual(mp.read_current(self.model_root()), "v2")

    def test_per_side_model_restarts_only_its_side(self):
        self.install("v1", current="v1")
        self.install("r1", "r2", current="r1", name="dpc_model_right")
        self.run_site(quiet_window=self.window_now(), right_model="dpc_model_right")
        self.publish_downloaded("dpc_model_right", "r2")
        self.wait_for(lambda: self.starts().count("inspection_right") == 2, "right side not restarted")
        time.sleep(1.5)
        self.assertEqual(self.starts().count("inspection_left"), 1, "D-086: the other side keeps running")
        self.assertEqual(mp.read_current(self.model_root("dpc_model_right")), "r2")


class NotSwitched(AutoInstallTest):
    def test_pinned_model_is_never_switched_automatically(self):
        self.install("v1", "v2", current="v1")
        mp_root = self.model_root()
        self.start()
        self.manager(self.site()).pin(self.MODEL, "v1")
        self.downloaded(self.MODEL, "v2")
        self.settle()
        self.assertEqual(mp.read_current(mp_root), "v1")
        self.assertEqual(self.selftest_calls(), [], "no child, no self-test")
        self.assertIn("pinned to v1", self.skipped()[-1]["reason"])

    def test_download_and_off_modes_only_log(self):
        for mode in ("download", "off"):
            with self.subTest(mode=mode):
                self.sup = None
                self.install("v1", "v2", current="v1") if not os.path.isdir(self.model_root()) else None
                self.start(auto_update=mode)
                self.downloaded(self.MODEL, "v2")
                self.settle()
                self.assertEqual(mp.read_current(self.model_root()), "v1")
                self.assertIn(f"models.auto_update is {mode}, so a person switches", self.log_text())
                self.sup.auto_install.stop()
                self.sup.reload_channel.remove_pid()
                self.sup.shutdown()
                self.sup = None

    def test_already_current_and_not_in_use_are_skipped_with_a_reason(self):
        self.install("v1", current="v1")
        self.start()
        self.downloaded(self.MODEL, "v1")
        self.downloaded("some_other_model", "v1")
        self.settle()
        reasons = [p["reason"] for p in self.skipped()]
        self.assertIn("already the current version", reasons[0])
        self.assertIn("not used on this site", reasons[1])

    def test_unusable_payloads_and_other_events_are_ignored(self):
        self.install("v1", current="v1")
        self.start()
        for payload in ({"event": "model_downloaded", "name": "../x", "version": "v2"},
                        {"event": "model_downloaded", "name": self.MODEL},
                        {"event": "cycle_uploaded", "name": self.MODEL, "version": "v2"},
                        "not a dict"):
            self.sup.auto_install.on_transfer_event(TOPIC, payload)
        self.settle()
        self.assertEqual(mp.read_current(self.model_root()), "v1")
        self.assertEqual(self.selftest_calls(), [])
        self.assertEqual(self.skipped(), [])


class Wiring(AutoInstallTest):
    def test_subscribed_to_the_transfer_event_topic(self):
        topics = []
        self.mqtt.subscribe = lambda topic, cb: topics.append((topic, cb))
        self.install("v1", current="v1")
        self.start()
        self.assertIn(TOPIC, [t for t, _ in topics])
        callback = dict(topics)[TOPIC]
        self.assertEqual(callback, self.sup.auto_install.on_transfer_event)

    def test_shutdown_stops_a_running_switch(self):
        slow = self.path("slow_selftest.py")
        with open(slow, "w") as f:
            f.write("import time\ntime.sleep(60)\n")
        self.install("v1", "v2", current="v1")
        self.start()
        self.sup.auto_install.extra_args = ["--selftest-script", slow]
        self.downloaded(self.MODEL, "v2")
        self.run_until(lambda: self.sup.auto_install._child is not None, message="child not started")
        began = time.time()
        self.sup.auto_install.stop()
        self.assertLess(time.time() - began, 20)
        self.assertEqual(mp.read_current(self.model_root()), "v1", "stopped before set_current: nothing changed")


if __name__ == "__main__":
    unittest.main()
