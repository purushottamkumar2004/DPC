"""Tests for STATUS F59 (Supervisor support for the UI, D-095, D-096).

Run from the repo root:  python3 -m unittest tests.unit.test_supervisor_f59 -v

  StatusJson     `supervisor.py status --json` against a running Supervisor on a real mosquitto
                 (skipped without one): supervisor presence + every worker status, disabled ones too.
  Disabled       the Supervisor publishes a retained DISABLED status for `enabled: false` workers.
  JsonErrors     in --json mode an error is one JSON object, never plain text (the UI parses stdout).
(The ui worker entry itself is tested in test_supervisor_site_workers: UiWorkerF59 and
 EveryWorkerHasItsRenderedFile.)
"""

import json
import os
import subprocess
import sys
import unittest

import yaml

from tests.unit.test_supervisor_f28 import MOSQUITTO, RealBroker, paho          # noqa: F401 (sys.path)
from tests.unit.test_supervisor_v2 import SUPERVISOR_DIR, SupervisorTest, dummy


@unittest.skipUnless(MOSQUITTO and paho, "needs mosquitto and paho-mqtt")
class StatusJson(RealBroker):
    def setUp(self):
        super().setUp()
        with open(self.workers_yaml) as f:
            data = yaml.safe_load(f)
        data["workers"].append(dummy("mail", "", enabled=False))
        with open(self.workers_yaml, "w") as f:
            yaml.safe_dump(data, f)

    # the F28 test methods are not repeated here
    test_cli_status_and_restart_leave_the_supervisor_online = None
    test_supervisor_stop_and_crash_still_say_offline = None
    test_cli_call_while_the_supervisor_is_down_does_not_say_online = None
    test_a_stale_offline_is_corrected_by_the_periodic_online = None

    def status_json(self):
        result = subprocess.run(self.cmd("status", "--json"), capture_output=True, text=True, timeout=30)
        return result.returncode, json.loads(result.stdout)

    def test_status_json_has_supervisor_and_every_worker(self):
        self.start_supervisor()
        self.wait_worker_running()
        code, data = self.status_json()
        self.assertEqual(code, 0)
        self.assertEqual(data["supervisor"]["status"], "online")
        self.assertEqual(data["workers"]["dummy_a"]["status"], "RUNNING")
        self.assertEqual(data["workers"]["mail"]["status"], "DISABLED")
        self.assertIn("enabled: false", data["workers"]["mail"]["message"])
        for key in ("worker_id", "pid", "restart_count", "message", "mode", "restart_cost", "last_error"):
            self.assertIn(key, data["workers"]["dummy_a"], f"same keys as the MQTT status: {key}")

    def test_human_output_unchanged(self):
        self.start_supervisor()
        self.wait_worker_running()
        out = subprocess.run(self.cmd("status"), capture_output=True, text=True, timeout=30).stdout
        self.assertTrue(out.startswith("WORKER              STATUS           PID     RESTARTS  MESSAGE"))
        self.assertIn("dummy_a", out)

    def test_broker_down_is_a_json_error(self):
        self.broker.terminate()
        self.broker.wait(10)
        result = subprocess.run(self.cmd("status", "--json"), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("MQTT broker", json.loads(result.stdout)["error"])


class Disabled(SupervisorTest):
    def test_disabled_workers_are_published_as_disabled_with_the_presence(self):
        self.build([dummy("a", ""), dummy("mail", "", enabled=False), dummy("plc", "", enabled=False)],
                   {"status_publish_interval_seconds": 0.5})
        self.sup.start()
        self.run_for(1.2)
        statuses = {w: s for w, s in self.mqtt.statuses if s["status"] == "DISABLED"}
        self.assertEqual(sorted(statuses), ["mail", "plc"])
        self.assertEqual(statuses["mail"]["pid"], None)
        self.assertNotIn("mail", self.sup.workers, "a disabled worker is still never started")
        repeats = sum(1 for w, s in self.mqtt.statuses if w == "mail")
        self.assertGreaterEqual(repeats, 2, "repeated every status interval (after a broker restart too)")

    def test_not_published_before_the_connection_is_up(self):
        import threading
        self.mqtt.connected = threading.Event()                 # like the real client: not yet connected
        self.build([dummy("a", ""), dummy("mail", "", enabled=False)])
        self.sup.start()
        self.run_for(0.5)
        self.assertFalse([w for w, s in self.mqtt.statuses if w == "mail"], "would be dropped by paho")
        self.mqtt.connected.set()
        self.run_for(0.3)
        self.assertTrue([w for w, s in self.mqtt.statuses if w == "mail"])


class JsonErrors(SupervisorTest):
    def cli(self, *args):
        cmd = [sys.executable, os.path.join(SUPERVISOR_DIR, "supervisor.py"), *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    def test_status_json_with_a_bad_workers_file(self):
        result = self.cli("status", "--json", "-c", self.path("missing.yaml"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("Configuration error", json.loads(result.stdout)["error"])

    def test_model_status_json_with_a_missing_site_yaml(self):
        path = self.path("workers.yaml")
        with open(path, "w") as f:
            yaml.safe_dump({"mqtt": {"enabled": False}, "log_dir": self.path("logs"), "workers": []}, f)
        result = self.cli("model", "status", "--json", "-c", path, "--site-config", self.path("none/site.yaml"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("Site config error", json.loads(result.stdout)["error"])

    def test_human_errors_stay_plain_text(self):
        result = self.cli("status", "-c", self.path("missing.yaml"))
        self.assertTrue(result.stdout.startswith("Configuration error"))


if __name__ == "__main__":
    unittest.main()
