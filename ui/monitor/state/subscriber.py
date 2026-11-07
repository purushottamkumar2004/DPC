"""One MQTT subscriber on <prefix>/# for the whole UI process (CONTRACTS 2).

paho runs its own network thread and reconnects by itself; every message goes to
StateCache.handle(). The same client publishes the UI's heartbeat on
<prefix>/worker/<worker_id>/health every HEARTBEAT_S seconds, so the Supervisor can watch
the UI with `health: heartbeat` (F59). A broker that is down is logged once, never a crash.
"""
import json
import logging
import os
import threading

log = logging.getLogger("dpc.ui.mqtt")

HEARTBEAT_S = 10


class Subscriber:
    def __init__(self, cache, mqtt_cfg: dict, worker_id: str = "ui"):
        self.cache = cache
        self.cfg = mqtt_cfg
        self.worker_id = worker_id
        self.prefix = (mqtt_cfg.get("topic_prefix") or "dpc").strip("/")
        self.client = None
        self._stop = threading.Event()

    def start(self):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            log.error("paho-mqtt is not installed: the UI shows no live status (pip install paho-mqtt)")
            return
        client_id = f"dpc-ui-{os.getpid()}"
        try:   # paho 2.x needs a callback API version, 1.x does not accept it
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        except AttributeError:
            client = mqtt.Client(client_id=client_id)
        if self.cfg.get("user"):
            client.username_pw_set(self.cfg["user"], self.cfg.get("password"))
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.connect_async(self.cfg.get("host") or "localhost", int(self.cfg.get("port") or 1883), keepalive=30)
        client.loop_start()
        self.client = client
        threading.Thread(target=self._heartbeat_loop, name="ui-heartbeat", daemon=True).start()
        log.info("MQTT subscriber on %s/# at %s:%s", self.prefix, self.cfg.get("host"), self.cfg.get("port"))

    def stop(self):
        self._stop.set()
        if self.client is not None:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass

    # paho callbacks (paho 1.x and 2.x signatures)
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        failed = reason_code.is_failure if hasattr(reason_code, "is_failure") else reason_code != 0
        if failed:
            log.warning("MQTT connect refused: %s", reason_code)
            return
        log.info("MQTT connected")
        client.subscribe(f"{self.prefix}/#")      # retained statuses arrive right after this
        self.cache.set_connected(True)

    def _on_disconnect(self, client, userdata, *args):
        if self._stop.is_set():                    # our own shutdown: not an alert
            return
        if self.cache.connected:
            log.warning("MQTT disconnected; paho keeps retrying")
        self.cache.set_connected(False)

    def _on_message(self, client, userdata, msg):
        try:
            text = msg.payload.decode("utf-8") if msg.payload else ""
            try:
                payload = json.loads(text) if text else {}
            except ValueError:
                payload = text                     # e.g. a plain "online"
            self.cache.handle(msg.topic, payload)
        except Exception:
            log.exception("MQTT message on %s not handled", msg.topic)

    def _heartbeat_loop(self):
        topic = f"{self.prefix}/worker/{self.worker_id}/health"
        while not self._stop.wait(HEARTBEAT_S):
            if self.client is None or not self.cache.connected:
                continue
            try:
                self.client.publish(topic, json.dumps({"worker_id": self.worker_id, "healthy": True,
                                                       "pid": os.getpid()}), qos=0)
            except Exception as e:
                log.debug("heartbeat not published: %s", e)
