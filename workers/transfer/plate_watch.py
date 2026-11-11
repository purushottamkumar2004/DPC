"""Inspection's plate signal, read from its heartbeat (STATUS F76, asked from W-inspection):

    dpc/worker/inspection_<side>/health  {"pid": .., "plates_seen": 12,
                                         "last_plate_frame": "2026_11_10_09_15_02_123456.jpg",
                                         "last_plate_at": "...", "last_cycle_wheels": 190}

plates_seen = new plates since that inspection process started; last_plate_frame = the frame
that opened the current cycle. The heartbeat repeats every 10 s, so a lost message or a
restart of this program loses nothing; a new pid (or plates_seen going back) means inspection
restarted, i.e. a gap in the frames.

This module only keeps the newest heartbeat per side; cycle_capture.py decides what it means.
"""

import json
import logging
import threading
import time
from typing import Callable, Dict, Optional

from common.heartbeat import _paho_client

TOPIC = "dpc/worker/{worker_id}/health"


class PlateWatch:
    def __init__(self, side_workers: Dict[str, str], mqtt: Optional[dict], log: logging.Logger,
                 client_factory: Callable = _paho_client, clock=time.time):
        self.side_of_topic = {TOPIC.format(worker_id=w): side for side, w in side_workers.items()}
        self.mqtt = mqtt
        self.log = log
        self.client_factory = client_factory
        self.clock = clock
        self.client = None
        self._latest: Dict[str, dict] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- data
    def on_heartbeat(self, topic: str, payload: bytes) -> None:
        side = self.side_of_topic.get(topic)
        if side is None:
            return
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict) or not isinstance(data.get("plates_seen"), int):
            # inspection without F76: keep the pid so the capture can say why it waits
            data = {"pid": data.get("pid") if isinstance(data, dict) else None, "no_plate_signal": True}
        data["received_at"] = self.clock()
        with self._lock:
            self._latest[side] = data

    def snapshot(self, side: str) -> Optional[dict]:
        with self._lock:
            value = self._latest.get(side)
            return dict(value) if value else None

    # ---------------------------------------------------------------- MQTT
    def start(self) -> bool:
        if not self.mqtt:
            self.log.warning("Plate signal off: transfer.json has no 'mqtt' block; cycle uploads will wait")
            return False
        client = self.client_factory("transfer-plates")
        if client is None:
            self.log.warning("Plate signal off: paho-mqtt is not installed (pip install paho-mqtt)")
            return False
        if self.mqtt.get("user"):
            client.username_pw_set(self.mqtt["user"], self.mqtt.get("password"))

        def on_connect(c, *_args):                 # paho 1.x and 2.x pass different extra arguments
            for topic in self.side_of_topic:
                c.subscribe(topic, qos=0)

        client.on_connect = on_connect
        client.on_message = lambda _c, _u, msg: self.on_heartbeat(msg.topic, msg.payload)
        if hasattr(client, "reconnect_delay_set"):
            client.reconnect_delay_set(min_delay=1, max_delay=60)
        try:
            client.connect_async(self.mqtt.get("host", "localhost"), int(self.mqtt.get("port", 1883)), keepalive=30)
            client.loop_start()                    # paho reconnects (and re-subscribes) by itself
        except Exception as exc:
            self.log.warning(f"Plate signal: MQTT broker not reachable ({type(exc).__name__}); paho retries")
        self.client = client
        return True

    def stop(self) -> None:
        if self.client is not None:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
