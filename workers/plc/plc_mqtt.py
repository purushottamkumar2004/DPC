"""MQTT for the PLC service (CONTRACTS 2; the plc topics are PROPOSED in the W-plc handoff).

Publishes:
  dpc/plc/status              retained, every status interval: connected, conveyor_status,
                              light_value, light_ok, cleaning_active, last_pulse_at, auto_cleaning
  dpc/worker/<id>/health      heartbeat {"worker_id", "healthy": true, "pid", "version"}; healthy
                              stays true while the PLC is unreachable (a restart would not help)
  dpc/worker/<id>/event       {worker_id, event, timestamp, ...}: light_on_written,
                              cleaning_pulse_started, cleaning_pulse_ended
Subscribes:
  dpc/plc/command             {"action": "clean_pulse", "source": "...", "reason": "..."}
  dpc/worker/+/event          only with auto_cleaning: events with "event": "poor_view"
The broker being down never stops the service (paho reconnects in the background).
"""

import datetime
import json
import os
import threading

STATUS_TOPIC = "dpc/plc/status"
COMMAND_TOPIC = "dpc/plc/command"
EVENTS_TOPIC = "dpc/worker/+/event"


def parse_command(payload: bytes):
    """(source, reason) for a valid clean_pulse command, else None."""
    try:
        msg = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(msg, dict) or msg.get("action") != "clean_pulse":
        return None
    return str(msg.get("source") or "command")[:64], str(msg.get("reason") or "manual")[:200]


def parse_poor_view(topic: str, payload: bytes):
    """(source, reason) for a poor_view event from another worker, else None."""
    try:
        msg = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(msg, dict) or msg.get("event") != "poor_view":
        return None
    source = str(msg.get("worker_id") or topic.split("/")[2])[:64]
    return source, f"poor_view side {msg.get('side', '?')}"


class PlcMqtt:
    def __init__(self, settings, service, version: str, log):
        self.s, self.service, self.version, self.log = settings, service, version, log
        self.client = None
        self._lock = threading.Lock()

    def start(self):
        if self.s.mqtt is None:
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.log.warning("MQTT off: paho-mqtt is not installed (pip install paho-mqtt)")
            return
        try:   # paho 2.x needs a callback API version, 1.x does not accept it
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.s.worker_id)
        except AttributeError:
            client = mqtt.Client(client_id=self.s.worker_id)
        if self.s.mqtt.user:
            client.username_pw_set(self.s.mqtt.user, self.s.mqtt.password)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.connect_async(self.s.mqtt.host, self.s.mqtt.port, keepalive=30)
        client.loop_start()
        self.client = client
        self.log.info(f"MQTT to {self.s.mqtt.host}:{self.s.mqtt.port}; commands on {COMMAND_TOPIC}"
                      + (f", poor_view from {EVENTS_TOPIC}" if self.s.auto_cleaning else ""))

    def _on_connect(self, client, userdata, flags, rc, *extra):
        client.subscribe(COMMAND_TOPIC)
        if self.s.auto_cleaning:
            client.subscribe(EVENTS_TOPIC)

    def _on_message(self, client, userdata, msg):
        self.handle_message(msg.topic, msg.payload)

    def handle_message(self, topic: str, payload: bytes):
        """Turns a message into a pulse request (the service decides whether to act)."""
        if topic == COMMAND_TOPIC:
            parsed = parse_command(payload)
            if parsed is None:
                self.log.warning(f"ignored invalid message on {COMMAND_TOPIC}")
                return
            self.service.request_pulse(parsed[0], parsed[1], automatic=False)
        elif topic.startswith("dpc/worker/") and topic.endswith("/event"):
            if topic == f"dpc/worker/{self.s.worker_id}/event":
                return                                  # our own events
            parsed = parse_poor_view(topic, payload)
            if parsed is not None:
                self.service.request_pulse(parsed[0], parsed[1], automatic=True)

    def _publish(self, topic, payload, retain=False):
        if self.client is None:
            return
        try:
            with self._lock:
                self.client.publish(topic, json.dumps(payload), qos=0, retain=retain)
        except Exception as e:
            self.log.debug(f"MQTT publish to {topic} failed: {e}")

    def publish_status(self, snapshot: dict):
        self._publish(STATUS_TOPIC, snapshot, retain=True)

    def publish_heartbeat(self):
        self._publish(f"dpc/worker/{self.s.worker_id}/health",
                      {"worker_id": self.s.worker_id, "healthy": True, "pid": os.getpid(),
                       "version": self.version})

    def publish_event(self, name: str, payload: dict):
        body = {"worker_id": self.s.worker_id, "event": name,
                "timestamp": datetime.datetime.now().isoformat(timespec="seconds")}
        body.update(payload)
        self._publish(f"dpc/worker/{self.s.worker_id}/event", body)

    def send_command_once(self, source: str, reason: str) -> bool:
        """For the CLI --clean-pulse: publish one command and disconnect."""
        import paho.mqtt.publish as publish
        auth = {"username": self.s.mqtt.user, "password": self.s.mqtt.password} if self.s.mqtt.user else None
        publish.single(COMMAND_TOPIC, json.dumps({"action": "clean_pulse", "source": source,
                                                  "reason": reason}),
                       hostname=self.s.mqtt.host, port=self.s.mqtt.port, auth=auth)
        return True

    def stop(self):
        if self.client is not None:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
