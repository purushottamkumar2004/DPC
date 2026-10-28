"""Tests for STATUS F28: only the running Supervisor writes dpc/supervisor/status.

Run from the repo root:  python3 -m unittest tests.unit.test_supervisor_f28 -v

  RealBroker     a real mosquitto (skipped when it is not installed): `supervisor.py run`, then the CLI
                 `status` / `restart`; dpc/supervisor/status must still say online. Also: SIGTERM and
                 kill -9 of the Supervisor still give "offline", and a CLI call never says "online".
  PresenceRules  the same rules with a fake paho, so they are checked on every machine.
  Republish      the Supervisor re-publishes "online" every status_publish_interval_seconds.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import yaml

from tests.unit.test_supervisor_v2 import SUPERVISOR_DIR, SupervisorTest, dummy, supervisor_main   # sets sys.path
import communication.mqtt_client as mqtt_module                                                   # noqa: E402
from core.config import MqttSettings                                                              # noqa: E402

try:
    import paho.mqtt.client as paho
except ImportError:
    paho = None

MOSQUITTO = shutil.which("mosquitto")
STATUS_TOPIC = "dpc/supervisor/status"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(MOSQUITTO and paho, "needs mosquitto and paho-mqtt")
class RealBroker(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.port = free_port()
        conf = os.path.join(self.tmp, "mosquitto.conf")
        with open(conf, "w") as f:
            f.write(f"listener {self.port} 127.0.0.1\nallow_anonymous true\npersistence false\n")
        self.broker = subprocess.Popen([MOSQUITTO, "-c", conf], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait_for_port()
        self.workers_yaml = os.path.join(self.tmp, "workers.yaml")
        data = {"mqtt": {"enabled": True, "host": "127.0.0.1", "port": self.port},
                "monitoring": {"check_interval_seconds": 0.5, "startup_stagger_seconds": 0,
                               "status_publish_interval_seconds": 2},
                "runtime": {"root": os.path.join(self.tmp, "run")}, "log_dir": os.path.join(self.tmp, "logs"),
                "workers": [dummy("dummy_a", "")]}
        with open(self.workers_yaml, "w") as f:
            yaml.safe_dump(data, f)
        self.sup = None
        self.orphans = []                     # workers left behind by a kill -9 of the Supervisor

    def tearDown(self):
        if self.sup and self.sup.poll() is None:
            self.sup.terminate()
            self.sup.wait(20)
        for pid in self.orphans:              # on a site, systemd KillMode=mixed does this
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.broker.terminate()
        self.broker.wait(10)
        self._tmp.cleanup()

    def _wait_for_port(self):
        deadline = time.time() + 5
        while time.time() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.05)
        self.fail("mosquitto did not start")

    def cmd(self, *args):
        return [sys.executable, os.path.join(SUPERVISOR_DIR, "supervisor.py"), *args,
                "-c", self.workers_yaml, "--site-config", os.path.join(self.tmp, "no-site.yaml")]

    def retained(self, topic=STATUS_TOPIC, timeout=3.0):
        """The retained message on `topic`, or None."""
        got = {}
        client = (paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=f"t-{time.time()}")
                  if hasattr(paho, "CallbackAPIVersion") else paho.Client(client_id=f"t-{time.time()}"))
        client.on_message = lambda c, u, m: got.setdefault("payload", json.loads(m.payload or b"{}"))
        client.connect("127.0.0.1", self.port)
        client.subscribe(topic)
        client.loop_start()
        deadline = time.time() + timeout
        while time.time() < deadline and "payload" not in got:
            time.sleep(0.05)
        client.loop_stop()
        client.disconnect()
        return got.get("payload")

    def wait_status(self, value, timeout=10.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.retained(timeout=1.0)
            if last and last.get("status") == value:
                return
        self.fail(f"dpc/supervisor/status never became {value!r} (last: {last})")

    def start_supervisor(self):
        self.sup = subprocess.Popen(self.cmd("run"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait_status("online")
        deadline = time.time() + 10                    # wait until the worker's retained status is there
        while time.time() < deadline and not self.retained("dpc/worker/dummy_a/status", timeout=0.5):
            pass

    def wait_worker_running(self):
        deadline = time.time() + 10
        while time.time() < deadline:
            status = self.retained("dpc/worker/dummy_a/status", timeout=0.5)
            if status and status.get("status") == "RUNNING" and status.get("pid"):
                return
        self.fail("dummy_a never RUNNING")

    def test_cli_status_and_restart_leave_the_supervisor_online(self):
        self.start_supervisor()
        result = subprocess.run(self.cmd("status"), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("dummy_a", result.stdout)
        self.assertEqual(self.retained()["status"], "online", "F28: `supervisor.py status` marked it offline")
        subprocess.run(self.cmd("restart", "dummy_a"), capture_output=True, text=True, timeout=30)
        self.assertEqual(self.retained()["status"], "online", "F28: `supervisor.py restart` marked it offline")

    def test_supervisor_stop_and_crash_still_say_offline(self):
        self.start_supervisor()
        self.sup.send_signal(signal.SIGTERM)
        self.sup.wait(30)
        self.wait_status("offline")                    # its own stop()
        self.start_supervisor()
        self.wait_worker_running()
        self.orphans.append(self.retained("dpc/worker/dummy_a/status")["pid"])
        self.sup.kill()                                # kill -9: no stop(), the broker sends the last-will
        self.sup.wait(10)
        self.wait_status("offline")

    def test_cli_call_while_the_supervisor_is_down_does_not_say_online(self):
        self.start_supervisor()
        self.sup.send_signal(signal.SIGTERM)
        self.sup.wait(30)
        self.wait_status("offline")
        subprocess.run(self.cmd("status"), capture_output=True, text=True, timeout=30)
        self.assertEqual(self.retained()["status"], "offline", "a CLI connect must not announce the Supervisor")

    def test_a_stale_offline_is_corrected_by_the_periodic_online(self):
        self.start_supervisor()
        client = (paho.Client(paho.CallbackAPIVersion.VERSION2) if hasattr(paho, "CallbackAPIVersion") else paho.Client())
        client.connect("127.0.0.1", self.port)
        client.loop_start()
        client.publish(STATUS_TOPIC, json.dumps({"status": "offline"}), retain=True).wait_for_publish()
        client.loop_stop()
        client.disconnect()
        self.assertEqual(self.retained()["status"], "offline")
        self.wait_status("online", timeout=6)          # status_publish_interval_seconds = 2


class _FakePahoClient:
    instances = []

    def __init__(self, *args, **kwargs):
        self.will = None
        self.published = []
        _FakePahoClient.instances.append(self)

    def will_set(self, topic, payload, retain=False):
        self.will = (topic, payload, retain)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload), retain))

    def username_pw_set(self, *a): pass
    def connect_async(self, *a, **k): pass
    def loop_start(self): pass
    def loop_stop(self): pass
    def disconnect(self): pass
    def subscribe(self, *a, **k): pass


class _FakePaho:
    Client = _FakePahoClient


class PresenceRules(unittest.TestCase):
    def client(self, **kwargs):
        with mock.patch.object(mqtt_module, "mqtt", _FakePaho):
            c = mqtt_module.MqttClient(MqttSettings(enabled=True), **kwargs)
        return c, _FakePahoClient.instances[-1]

    def status_messages(self, paho_client):
        return [p for t, p, _ in paho_client.published if t == STATUS_TOPIC]

    def connect(self, client):
        client._on_connect(None, None, None, 0)

    def test_default_client_never_touches_the_supervisor_status(self):
        client, fake = self.client()
        self.assertIsNone(fake.will, "no last-will for a CLI client")
        self.connect(client)
        client.publish_online()
        client.stop()
        self.assertEqual(self.status_messages(fake), [])

    def test_announcing_client_has_will_online_and_offline(self):
        client, fake = self.client(announce_presence=True)
        self.assertEqual((fake.will[0], json.loads(fake.will[1]), fake.will[2]), (STATUS_TOPIC, {"status": "offline"}, True))
        self.connect(client)
        client.stop()
        self.assertEqual([m["status"] for m in self.status_messages(fake)], ["online", "offline"])
        self.assertTrue(all(retain for t, _, retain in fake.published if t == STATUS_TOPIC))

    def test_supervisor_announces_and_the_cli_does_not(self):
        with mock.patch.object(mqtt_module, "mqtt", _FakePaho):
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "workers.yaml")
                with open(path, "w") as f:
                    yaml.safe_dump({"mqtt": {"enabled": True}, "log_dir": os.path.join(tmp, "logs"),
                                    "runtime": {"root": os.path.join(tmp, "run")}, "workers": [dummy("a", "")]}, f)
                cfg = supervisor_main.ConfigurationManager(path, SUPERVISOR_DIR)
                cfg.load()
                site_env = supervisor_main.SiteEnv(os.path.join(tmp, "none.yaml"))
                supervisor_main.Supervisor(cfg, site_env=site_env)
                self.assertIsNotNone(_FakePahoClient.instances[-1].will, "the Supervisor sets the last-will")
                supervisor_main._cli_mqtt(cfg, site_env)
                self.assertIsNone(_FakePahoClient.instances[-1].will, "the CLI does not")


class Republish(SupervisorTest):
    def test_online_is_republished_every_status_interval(self):
        self.build([dummy("a", "")], {"status_publish_interval_seconds": 0.5})
        self.sup.start()
        self.run_for(1.8)
        onlines = [p for t, p in self.mqtt.published if t == STATUS_TOPIC and p["status"] == "online"]
        self.assertGreaterEqual(len(onlines), 3)
        self.assertLessEqual(len(onlines), 5)


if __name__ == "__main__":
    unittest.main()
