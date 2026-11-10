"""Heartbeat to the Supervisor, shared by every worker (D-034, CONTRACTS 2, STATUS F67).

    from common.heartbeat import Heartbeat

    hb = Heartbeat.from_rendered_config(rendered_json_path_or_dict,
                                        extra=lambda: {"fps": stats.fps()},     # optional, per program
                                        healthy=lambda: loop_is_turning())      # optional, default True
    hb.start()            # background thread: dpc/worker/<worker_id>/health every 10 s
    ...
    hb.publish_event("roi_learned", {...})   # optional: dpc/worker/<worker_id>/event
    hb.stop()

Payload (D-034, append-only): {"worker_id", "healthy", "pid", "version", "time", ...extra}.
`time` is ISO-8601 local time to the second. The base keys always win over an extra key of the same
name. `version` is `git describe` of the code checkout unless given.

The broker is never needed (D-034): without paho-mqtt or without an `mqtt` block the program runs
without a heartbeat and says so once; a broker that is down is retried quietly in the background
(paho reconnects; a failed first connect is retried every interval); a failing `extra` or
`healthy` callable or a failing publish never stops the program.
"""

import datetime
import importlib.util
import json
import logging
import os
import subprocess
import threading
from typing import Any, Callable, Dict, Optional, Union

DEFAULT_INTERVAL_S = 10
TOPIC = "dpc/worker/{worker_id}/health"
EVENT_TOPIC = "dpc/worker/{worker_id}/event"
BASE_KEYS = ("worker_id", "healthy", "pid", "version", "time")
CODE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # <root>/code

log = logging.getLogger("dpc.heartbeat")


def code_version(repo_dir: str = CODE_ROOT) -> str:
    """`git describe` of the deployed code, or 'unknown'."""
    try:
        out = subprocess.run(["git", "-C", repo_dir, "describe", "--tags", "--always", "--dirty"],
                             capture_output=True, text=True, timeout=3)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _mqtt_value(settings: Any, key: str, default=None):
    """The rendered `mqtt` block is a dict; a worker's own settings object may be used as well."""
    if isinstance(settings, dict):
        return settings.get(key, default)
    return getattr(settings, key, default)


def _paho_client(worker_id: str):
    """A paho client (1.x or 2.x), or None when paho-mqtt is not installed."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        return None
    try:   # paho 2.x needs a callback API version, 1.x does not accept it
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=worker_id)
    except AttributeError:
        return mqtt.Client(client_id=worker_id)


class Heartbeat:
    def __init__(self, worker_id: str, mqtt: Any = None, version: Optional[str] = None,
                 interval_s: float = DEFAULT_INTERVAL_S,
                 extra: Optional[Callable[[], Dict[str, Any]]] = None,
                 healthy: Optional[Callable[[], bool]] = None,
                 logger: Optional[logging.Logger] = None,
                 client_factory: Callable[[str], Any] = _paho_client):
        self.worker_id = worker_id
        self.mqtt = mqtt                      # {host, port, user, password} or None = off
        self.version = version if version is not None else code_version()
        self.interval_s = interval_s
        self.extra = extra
        self.healthy = healthy
        self.log = logger or log
        self.client_factory = client_factory
        self.client = None
        self.topic = TOPIC.format(worker_id=worker_id)
        self._stop = threading.Event()
        self._thread = None
        self._connect_failed = False          # log the first failure of a streak only

    @classmethod
    def from_rendered_config(cls, rendered: Union[str, Dict[str, Any]], **kwargs) -> "Heartbeat":
        """worker_id and the MQTT login from the worker's rendered JSON (CONTRACTS 13)."""
        if isinstance(rendered, str):
            with open(rendered, "r", encoding="utf-8") as f:
                rendered = json.load(f)
        worker_id = kwargs.pop("worker_id", None) or rendered.get("worker_id")
        if not worker_id:
            raise ValueError("the rendered config has no worker_id")
        return cls(worker_id, mqtt=rendered.get("mqtt"), **kwargs)

    # ---------------------------------------------------------------- payload

    def payload(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if self.extra is not None:
            try:
                body.update(self.extra() or {})
            except Exception as exc:          # a bug in the program's extra fields must not stop it
                self.log.debug(f"heartbeat extra fields failed: {exc!r}")
        try:
            healthy = True if self.healthy is None else bool(self.healthy())
        except Exception as exc:
            self.log.debug(f"heartbeat healthy() failed: {exc!r}")
            healthy = False
        body.update({"worker_id": self.worker_id, "healthy": healthy, "pid": os.getpid(),
                     "version": self.version,
                     "time": datetime.datetime.now().isoformat(timespec="seconds")})
        return body

    # ---------------------------------------------------------------- connection

    def _connect(self) -> bool:
        """Create and connect the client once; on failure try again next interval."""
        if self.client is not None:
            return True
        try:
            client = self.client_factory(self.worker_id)
            if client is None:
                return False
            user = _mqtt_value(self.mqtt, "user")
            if user:
                client.username_pw_set(user, _mqtt_value(self.mqtt, "password"))
            if hasattr(client, "reconnect_delay_set"):
                client.reconnect_delay_set(min_delay=1, max_delay=60)
            client.connect_async(_mqtt_value(self.mqtt, "host", "localhost"),
                                 int(_mqtt_value(self.mqtt, "port", 1883)), keepalive=30)
            client.loop_start()              # paho reconnects by itself from here on
        except Exception as exc:
            if not self._connect_failed:
                self.log.warning(f"Heartbeat: MQTT broker not reachable yet ({type(exc).__name__}); "
                                 f"retrying quietly")
            self._connect_failed = True
            return False
        self.client = client
        self._connect_failed = False
        return True

    def publish_now(self) -> bool:
        """One heartbeat now. False when it could not be handed to the client."""
        if not self._connect():
            return False
        try:
            self.client.publish(self.topic, json.dumps(self.payload()), qos=0)
            return True
        except Exception as exc:             # broker trouble must never stop the program
            self.log.debug(f"heartbeat publish failed: {exc!r}")
            return False

    def publish_event(self, name: str, payload: Optional[Dict[str, Any]] = None) -> bool:
        """One event on dpc/worker/<id>/event (CONTRACTS 2), best effort."""
        if self.mqtt is None or not self._connect():
            return False
        message = dict(payload or {}, event=name, worker_id=self.worker_id,
                       timestamp=datetime.datetime.now().isoformat(timespec="seconds"))
        try:
            self.client.publish(EVENT_TOPIC.format(worker_id=self.worker_id), json.dumps(message), qos=1)
            return True
        except Exception as exc:
            self.log.debug(f"event {name} not published: {exc!r}")
            return False

    # ---------------------------------------------------------------- thread

    def start(self) -> bool:
        """Start the background thread. False (and one warning) when the heartbeat is off."""
        if self.mqtt is None:
            self.log.warning("Heartbeat off: the rendered config has no 'mqtt' block")
            return False
        if self.client_factory is _paho_client and importlib.util.find_spec("paho") is None:
            self.log.warning("Heartbeat off: paho-mqtt is not installed (pip install paho-mqtt)")
            return False
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
        self._thread.start()
        self.log.info(f"Heartbeat every {self.interval_s:g} s to {self.topic}")
        return True

    def _run(self):
        self.publish_now()                   # one right away: the Supervisor sees the worker at once
        while not self._stop.wait(self.interval_s):
            self.publish_now()

    def stop(self, timeout: float = 2.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        if self.client is not None:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
