"""The UI's picture of the running system, built from MQTT (CONTRACTS 2).

One subscriber on dpc/# (state/subscriber.py) hands every message to StateCache.handle():
  dpc/worker/<id>/status    retained   latest status per worker (+ when that status began)
  dpc/worker/<id>/health    -          latest heartbeat per worker (+ when received)
  dpc/worker/<id>/event     -          stored as an Event (alert history)
  dpc/supervisor/status     retained   online / offline
  dpc/supervisor/event      -          stored as an Event
  dpc/plc/status            retained   PLC connected, light, conveyor
Changes the UI notices itself (a worker status changing, the Supervisor going offline, the
PLC disconnecting) are stored as synthetic events, once per change, via the recorder.

Everything here is in memory and thread-safe; the recorder (state/event_store.py) writes
SQLite. A recorder error is logged and never stops the MQTT thread.
"""
import copy
import logging
import threading
import time

from monitor.messages import effective_status

log = logging.getLogger("dpc.ui.cache")


class StateCache:
    def __init__(self, recorder=None, topic_prefix="dpc", clock=time.time):
        self._lock = threading.Lock()
        self._recorder = recorder
        self._prefix = topic_prefix.strip("/")
        self._clock = clock
        self.statuses = {}        # worker_id -> {"payload", "received_at", "since"}
        self.heartbeats = {}      # worker_id -> {"payload", "received_at"}
        self.supervisor = None    # {"payload", "received_at"}
        self.plc = None           # {"payload", "received_at"}
        self.connected = False
        self.connection_changed_at = clock()
        self.last_message_at = None

    # ------------------------------------------------------------------ MQTT side
    def set_connected(self, connected: bool):
        with self._lock:
            if connected == self.connected:
                return
            self.connected = connected
            self.connection_changed_at = self._clock()
        self._transition("mqtt", "connected" if connected else "lost",
                         "mqtt_back" if connected else "mqtt_lost", "mqtt", {}, quiet_first=True)

    def handle(self, topic: str, payload):
        """Route one message. `payload` is the decoded JSON (dict) or a plain string."""
        parts = topic.split("/")
        if not parts or parts[0] != self._prefix or len(parts) < 3:
            return
        now = self._clock()
        with self._lock:
            self.last_message_at = now
        if not isinstance(payload, dict):
            payload = {"status": str(payload)} if payload else {}
        kind = parts[1]
        if kind == "worker" and len(parts) == 4:
            self._worker_message(parts[2], parts[3], topic, payload, now)
        elif kind == "supervisor" and parts[2] == "status":
            self._supervisor_status(payload, now)
        elif kind == "supervisor" and parts[2] == "event":
            self._record(topic, "supervisor", payload.get("event") or "supervisor_event", payload)
        elif kind == "plc" and parts[2] == "status":
            self._plc_status(payload, now)
        # dpc/plc/command and anything else: not state, ignored

    def _worker_message(self, worker_id, what, topic, payload, now):
        if what == "status":
            value = effective_status(payload)
            since = self._transition(f"worker:{worker_id}", value, "status_changed", worker_id,
                                     dict(payload, worker_id=worker_id), topic=topic,
                                     quiet_first=value in ("RUNNING", "IDLE", "STARTING"))
            with self._lock:
                self.statuses[worker_id] = {"payload": payload, "received_at": now, "since": since or now}
        elif what == "health":
            with self._lock:
                self.heartbeats[worker_id] = {"payload": payload, "received_at": now}
        elif what == "event":
            self._record(topic, worker_id, payload.get("event") or "event", payload)

    def _supervisor_status(self, payload, now):
        value = str(payload.get("status") or "unknown").lower()
        self._transition("supervisor", value, f"supervisor_{value}", "supervisor", payload,
                         topic=f"{self._prefix}/supervisor/status", quiet_first=value == "online")
        with self._lock:
            self.supervisor = {"payload": payload, "received_at": now}

    def _plc_status(self, payload, now):
        connected = bool(payload.get("connected"))
        self._transition("plc", "connected" if connected else "disconnected",
                         "plc_connected" if connected else "plc_disconnected", "plc", payload,
                         topic=f"{self._prefix}/plc/status", quiet_first=connected)
        with self._lock:
            self.plc = {"payload": payload, "received_at": now}

    # ------------------------------------------------------------------ recorder
    def _record(self, topic, source, name, payload):
        if self._recorder is None:
            return
        try:
            self._recorder.record(topic, source, name, payload)
        except Exception:
            log.exception("Could not store event %s from %s", name, source)

    def _transition(self, key, value, event_name, source, payload, topic="", quiet_first=False):
        """Store an event when `key` changes value; return when the current value began
        (epoch seconds), or None without a recorder."""
        if self._recorder is None:
            return None
        try:
            changed, previous, since = self._recorder.transition(key, value)
            if changed and not (previous is None and quiet_first):
                self._recorder.record(topic or key, source, event_name, payload)
            return since
        except Exception:
            log.exception("Could not store the state change of %s", key)
            return None

    # ------------------------------------------------------------------ request side
    def snapshot(self) -> dict:
        """A consistent copy for one page render."""
        with self._lock:
            return {
                "now": self._clock(),
                "connected": self.connected,
                "connection_changed_at": self.connection_changed_at,
                "last_message_at": self.last_message_at,
                "statuses": copy.deepcopy(self.statuses),
                "heartbeats": copy.deepcopy(self.heartbeats),
                "supervisor": copy.deepcopy(self.supervisor),
                "plc": copy.deepcopy(self.plc),
            }
