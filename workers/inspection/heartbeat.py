"""Heartbeat to the Supervisor (D-034, CONTRACTS 2): `dpc/worker/<id>/health` every
HEARTBEAT_INTERVAL_S from a background thread.

`healthy` means the main loop is still turning: it is false when the loop has not ticked
for LOOP_STALL_S (for example a hung GPU call). An idle loop (empty INF) still ticks.
The Supervisor combines this with `queue_lag` on INF (`require_heartbeat: true`, D-049).

Without paho-mqtt or without an `mqtt` block in the rendered config the program runs
without a heartbeat and says so once; inspection itself never depends on the broker (D-034).
"""

import json
import os
import subprocess
import threading
import time
from collections import deque

HEARTBEAT_INTERVAL_S = 10
LOOP_STALL_S = 60
FPS_WINDOW_S = 60


def code_version(repo_dir: str) -> str:
    """`git describe` of the deployed code, or 'unknown'."""
    try:
        out = subprocess.run(["git", "-C", repo_dir, "describe", "--tags", "--always", "--dirty"],
                             capture_output=True, text=True, timeout=3)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class LoopStats:
    """Filled by the main loop, read by the heartbeat thread (simple values, GIL-safe)."""

    def __init__(self):
        self.last_tick = time.monotonic()
        self.last_frame_done = None          # monotonic time the last frame finished
        self.inf_queue_depth = 0
        self._frame_times = deque()

    def tick(self, inf_queue_depth=None):
        self.last_tick = time.monotonic()
        if inf_queue_depth is not None:
            self.inf_queue_depth = inf_queue_depth

    def frame_done(self):
        now = time.monotonic()
        self.last_frame_done = now
        self._frame_times.append(now)
        self.tick()

    def fps(self) -> float:
        now = time.monotonic()
        while self._frame_times and now - self._frame_times[0] > FPS_WINDOW_S:
            self._frame_times.popleft()
        return round(len(self._frame_times) / FPS_WINDOW_S, 2)


def heartbeat_payload(worker_id, stats: LoopStats, version, config_hash, now=None):
    now = time.monotonic() if now is None else now
    payload = {"worker_id": worker_id,
               "healthy": now - stats.last_tick < LOOP_STALL_S,
               "pid": os.getpid(),
               "version": version,
               "config_hash": config_hash,
               "fps": stats.fps(),
               "inf_queue_depth": stats.inf_queue_depth}
    if stats.last_frame_done is not None:
        payload["last_frame_age_s"] = round(now - stats.last_frame_done, 1)
    return payload


class Heartbeat:
    def __init__(self, settings, stats: LoopStats, version: str, logger):
        self.s, self.stats, self.version, self.log = settings, stats, version, logger
        self.client = None
        self._stop = threading.Event()

    def start(self):
        if self.s.mqtt is None:
            self.log.warning("Heartbeat off: the rendered config has no 'mqtt' block")
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.log.warning("Heartbeat off: paho-mqtt is not installed (pip install paho-mqtt)")
            return
        try:   # paho 2.x needs a callback API version, 1.x does not accept it
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.s.worker_id)
        except AttributeError:
            client = mqtt.Client(client_id=self.s.worker_id)
        if self.s.mqtt.user:
            client.username_pw_set(self.s.mqtt.user, self.s.mqtt.password)
        client.connect_async(self.s.mqtt.host, self.s.mqtt.port, keepalive=30)
        client.loop_start()                   # paho reconnects by itself in the background
        self.client = client
        threading.Thread(target=self._run, name="heartbeat", daemon=True).start()
        self.log.info(f"Heartbeat every {HEARTBEAT_INTERVAL_S} s to dpc/worker/{self.s.worker_id}/health")

    def _run(self):
        topic = f"dpc/worker/{self.s.worker_id}/health"
        while not self._stop.wait(HEARTBEAT_INTERVAL_S):
            payload = heartbeat_payload(self.s.worker_id, self.stats, self.version, self.s.config_hash)
            try:
                self.client.publish(topic, json.dumps(payload), qos=0)
            except Exception as e:            # broker trouble must never stop inspection
                self.log.debug(f"heartbeat publish failed: {e}")

    def stop(self):
        self._stop.set()
        if self.client is not None:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
