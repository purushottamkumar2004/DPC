"""Tests for common/heartbeat.py (STATUS F67, D-034) with a fake MQTT client.

Run from the repo root:
    python3 -m unittest tests.unit.test_heartbeat -v
"""

import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)

from common import heartbeat as hbm  # noqa: E402

MQTT = {"host": "broker.local", "port": 1884, "user": "dpc_broker", "password": "s3cret-mqtt-Hb7"}


class FakeClient:
    """Records what the heartbeat does; can be told to fail."""

    def __init__(self, fail_publish=False):
        self.calls, self.published = [], []
        self.fail_publish = fail_publish
        self.event = threading.Event()

    def username_pw_set(self, user, password):
        self.calls.append(("login", user, password))

    def reconnect_delay_set(self, min_delay, max_delay):
        self.calls.append(("reconnect_delay", min_delay, max_delay))

    def connect_async(self, host, port, keepalive):
        self.calls.append(("connect", host, port))

    def loop_start(self):
        self.calls.append(("loop_start",))

    def loop_stop(self):
        self.calls.append(("loop_stop",))

    def disconnect(self):
        self.calls.append(("disconnect",))

    def publish(self, topic, body, qos=0):
        if self.fail_publish:
            raise OSError("broker gone")
        self.published.append((topic, json.loads(body), qos))
        self.event.set()


def factory_for(client):
    return lambda worker_id: client


class Payload(unittest.TestCase):
    def test_base_fields(self):
        hb = hbm.Heartbeat("plc", MQTT, version="v0.7-3-gabc", client_factory=factory_for(FakeClient()))
        body = hb.payload()
        self.assertEqual({k: body[k] for k in ("worker_id", "healthy", "pid", "version")},
                         {"worker_id": "plc", "healthy": True, "pid": os.getpid(), "version": "v0.7-3-gabc"})
        self.assertRegex(body["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")

    def test_extra_fields_and_base_keys_win(self):
        hb = hbm.Heartbeat("inspection_left", MQTT, version="v1",
                           extra=lambda: {"fps": 3.9, "worker_id": "spoof", "healthy": False},
                           client_factory=factory_for(FakeClient()))
        body = hb.payload()
        self.assertEqual((body["fps"], body["worker_id"], body["healthy"]), (3.9, "inspection_left", True))

    def test_healthy_callable(self):
        state = {"ok": False}
        hb = hbm.Heartbeat("x", MQTT, version="v1", healthy=lambda: state["ok"])
        self.assertFalse(hb.payload()["healthy"])
        state["ok"] = True
        self.assertTrue(hb.payload()["healthy"])

    def test_failing_callables_never_raise(self):
        def boom():
            raise RuntimeError("bug in the program")
        hb = hbm.Heartbeat("x", MQTT, version="v1", extra=boom, healthy=boom)
        body = hb.payload()
        self.assertFalse(body["healthy"])                  # healthy() failed: report unhealthy
        self.assertEqual(body["worker_id"], "x")

    def test_version_defaults_to_git_describe(self):
        hb = hbm.Heartbeat("x", MQTT)
        self.assertIsInstance(hb.version, str)
        self.assertTrue(hb.version)


class Rendered(unittest.TestCase):
    def test_login_from_the_rendered_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plc.json")
            with open(path, "w") as f:
                json.dump({"worker_id": "plc", "mqtt": MQTT, "plc": {}}, f)
            client = FakeClient()
            hb = hbm.Heartbeat.from_rendered_config(path, version="v1", client_factory=factory_for(client))
        self.assertTrue(hb.publish_now())
        self.assertEqual(client.calls[:3], [("login", "dpc_broker", "s3cret-mqtt-Hb7"),
                                            ("reconnect_delay", 1, 60), ("connect", "broker.local", 1884)])
        topic, body, qos = client.published[0]
        self.assertEqual((topic, body["worker_id"], qos), ("dpc/worker/plc/health", "plc", 0))
        self.assertNotIn("s3cret", json.dumps(body))

    def test_no_login_when_user_is_null(self):
        client = FakeClient()
        hb = hbm.Heartbeat.from_rendered_config({"worker_id": "ui", "mqtt": {"host": "localhost", "port": 1883,
                                                                              "user": None, "password": None}},
                                                version="v1", client_factory=factory_for(client))
        hb.publish_now()
        self.assertNotIn("login", [c[0] for c in client.calls])

    def test_settings_object_works_too(self):
        class MqttSettings:
            host, port, user, password = "h", 1883, None, None
        client = FakeClient()
        hb = hbm.Heartbeat("inspection_right", MqttSettings(), version="v1", client_factory=factory_for(client))
        hb.publish_now()
        self.assertIn(("connect", "h", 1883), client.calls)

    def test_missing_worker_id(self):
        with self.assertRaises(ValueError):
            hbm.Heartbeat.from_rendered_config({"mqtt": MQTT})


class NeverCrashes(unittest.TestCase):
    def test_no_mqtt_block_means_off_with_one_warning(self):
        hb = hbm.Heartbeat.from_rendered_config({"worker_id": "cleanup"}, version="v1")
        with self.assertLogs("dpc.heartbeat", level="WARNING") as logs:
            self.assertFalse(hb.start())
        self.assertIn("no 'mqtt' block", logs.output[0])
        self.assertFalse(hb.publish_event("x"))

    def test_broker_down_is_retried_quietly(self):
        attempts = {"n": 0}
        client = FakeClient()

        def flaky(worker_id):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionRefusedError("broker down")
            return client
        hb = hbm.Heartbeat("plc", MQTT, version="v1", client_factory=flaky)
        with self.assertLogs("dpc.heartbeat", level="WARNING") as logs:
            self.assertFalse(hb.publish_now())
            self.assertFalse(hb.publish_now())
            self.assertTrue(hb.publish_now())
        self.assertEqual(len(logs.output), 1)                # one warning per failure streak, not per try
        self.assertEqual(len(client.published), 1)

    def test_publish_failure_never_raises(self):
        hb = hbm.Heartbeat("plc", MQTT, version="v1", client_factory=factory_for(FakeClient(fail_publish=True)))
        self.assertFalse(hb.publish_now())
        self.assertFalse(hb.publish_event("clean_pulse_done", {"n": 1}))

    def test_no_paho_means_off(self):
        hb = hbm.Heartbeat("plc", MQTT, version="v1", client_factory=lambda worker_id: None)
        self.assertFalse(hb.publish_now())


class Thread(unittest.TestCase):
    def test_publishes_at_once_then_every_interval_and_stops_cleanly(self):
        client = FakeClient()
        hb = hbm.Heartbeat("inspection_left", MQTT, version="v1", interval_s=0.05,
                           extra=lambda: {"fps": 4.0}, client_factory=factory_for(client))
        logging.getLogger("dpc.heartbeat").setLevel(logging.ERROR)
        self.assertTrue(hb.start())
        self.assertTrue(client.event.wait(1))                # the first one right away
        time.sleep(0.3)
        hb.stop()
        count = len(client.published)
        self.assertGreaterEqual(count, 3)
        self.assertTrue(all(t == "dpc/worker/inspection_left/health" and b["fps"] == 4.0
                            for t, b, _ in client.published))
        self.assertEqual(client.calls[-2:], [("loop_stop",), ("disconnect",)])
        time.sleep(0.15)
        self.assertEqual(len(client.published), count)       # nothing after stop

    def test_event(self):
        client = FakeClient()
        hb = hbm.Heartbeat("plc", MQTT, version="v1", client_factory=factory_for(client))
        self.assertTrue(hb.publish_event("clean_pulse_done", {"source": "ui"}))
        topic, body, qos = client.published[0]
        self.assertEqual((topic, qos), ("dpc/worker/plc/event", 1))
        self.assertEqual((body["event"], body["worker_id"], body["source"]), ("clean_pulse_done", "plc", "ui"))
        self.assertIn("timestamp", body)


if __name__ == "__main__":
    unittest.main()
