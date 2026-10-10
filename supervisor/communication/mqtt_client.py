"""
MqttClient - a thin wrapper around paho-mqtt.

Design rules:
  * MQTT problems must NEVER stop the supervisor. If the broker is down or
    paho is missing, we log a warning and carry on (publish becomes a no-op).
  * paho reconnects by itself in the background.
  * Topic names are built here, in one place.

Topics (prefix defaults to "dpc"):
  dpc/worker/<id>/status    supervisor -> world   (retained)
  dpc/worker/<id>/event     supervisor -> world   (failure, restart, ...)
  dpc/worker/<id>/health    worker -> supervisor  (heartbeat)
  dpc/worker/<id>/command   anyone  -> supervisor (start/stop/restart)
  dpc/supervisor/status     online/offline        (retained, last-will)
"""

import json
import logging
import threading
from datetime import datetime

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

log = logging.getLogger("supervisor.mqtt")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class MqttClient:
    def __init__(self, settings, client_id="dpc-supervisor"):
        self.prefix = settings.topic_prefix
        self.host = settings.host
        self.port = settings.port
        self.enabled = settings.enabled and mqtt is not None
        self.connected = threading.Event()
        self._subscriptions = []         # [(topic_filter, callback)]
        self._client = None

        if settings.enabled and mqtt is None:
            log.warning("paho-mqtt not installed - MQTT disabled (pip install paho-mqtt)")
        if not self.enabled:
            return

        # paho 2.x needs a callback API version, paho 1.x does not accept it
        if hasattr(mqtt, "CallbackAPIVersion"):
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        else:
            self._client = mqtt.Client(client_id=client_id)
        if settings.username:
            self._client.username_pw_set(settings.username, settings.password)
        self._client.will_set(self.topic("supervisor", "status"),
                              json.dumps({"status": "offline"}), retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    # ---------------- topics ----------------
    def topic(self, *parts) -> str:
        return "/".join([self.prefix, *parts])

    def worker_topic(self, worker_id: str, kind: str) -> str:
        return self.topic("worker", worker_id, kind)

    # ---------------- lifecycle ----------------
    def start(self):
        if not self.enabled:
            return
        try:
            self._client.connect_async(self.host, self.port, keepalive=30)
            self._client.loop_start()      # background thread, auto-reconnects
        except Exception as e:
            log.warning("MQTT start failed: %s", e)

    def stop(self):
        if not self.enabled:
            return
        try:
            self.publish(self.topic("supervisor", "status"), {"status": "offline"}, retain=True)
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass

    def wait_connected(self, timeout=3.0) -> bool:
        return self.enabled and self.connected.wait(timeout)

    # ---------------- publish / subscribe ----------------
    def publish(self, topic: str, payload: dict, retain=False):
        if not self.enabled:
            return
        try:
            self._client.publish(topic, json.dumps(payload), qos=0, retain=retain)
        except Exception as e:
            log.warning("MQTT publish failed (%s): %s", topic, e)

    def publish_status(self, worker_id: str, status: dict):
        self.publish(self.worker_topic(worker_id, "status"), status, retain=True)

    def publish_event(self, worker_id: str, event: str, **data):
        self.publish(self.worker_topic(worker_id, "event"),
                     {"worker_id": worker_id, "event": event, "timestamp": now_iso(), **data})

    def subscribe(self, topic_filter: str, callback):
        """callback(topic: str, payload: dict) - runs in the MQTT thread, keep it short."""
        self._subscriptions.append((topic_filter, callback))
        if self.enabled and self.connected.is_set():
            self._client.subscribe(topic_filter)

    # ---------------- paho callbacks ----------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        failed = reason_code.is_failure if hasattr(reason_code, "is_failure") else reason_code != 0
        if failed:
            log.warning("MQTT connect refused: %s", reason_code)
            return
        log.info("MQTT connected to %s:%s", self.host, self.port)
        self.connected.set()
        for topic_filter, _ in self._subscriptions:      # (re)subscribe after every reconnect
            client.subscribe(topic_filter)
        self.publish(self.topic("supervisor", "status"),
                     {"status": "online", "timestamp": now_iso()}, retain=True)

    def _on_disconnect(self, client, userdata, *args):
        self.connected.clear()
        log.warning("MQTT disconnected (will keep retrying in background)")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8")) if msg.payload else {}
        except ValueError:
            log.warning("Ignoring non-JSON MQTT message on %s", msg.topic)
            return
        for topic_filter, callback in self._subscriptions:
            if mqtt.topic_matches_sub(topic_filter, msg.topic):
                try:
                    callback(msg.topic, payload)
                except Exception:
                    log.exception("MQTT callback failed for %s", msg.topic)
