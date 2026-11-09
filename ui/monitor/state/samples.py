"""The UI's own samples for the sparklines (D-098): one value per series every SAMPLE_EVERY_S,
the last hour kept in memory (lost on a UI restart; a sparkline then starts empty).

Series (all from sources UI-1 already reads, no new data):
  fps:<CAM>        camera frames per second (frame meter on live/IMG_1.jpg)
  ips:<SIDE>       inspection images per second (heartbeat `fps`)
  ms:<SIDE>        inspection ms per image (heartbeat `ms_per_image`, once inspection publishes it)
  gpu_temp         GPU temperature, first GPU (nvidia-smi)
  disk             data disk percent used
"""
import collections
import threading
import time

SAMPLE_EVERY_S = 10
KEEP_S = 3600


class Samples:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._series = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def add(self, name: str, value):
        if value is None:
            return
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        now = self._clock()
        with self._lock:
            series = self._series[name]
            series.append((now, value))
            while series and now - series[0][0] > KEEP_S:
                series.popleft()

    def values(self, name: str):
        with self._lock:
            return [v for _, v in self._series.get(name, ())]


_samples = Samples()


def get_samples() -> Samples:
    return _samples


def set_samples(samples: Samples):
    global _samples
    _samples = samples


def take(cfg, snapshot, meter, gpu, disk, samples=None):
    """One sample of every series (called by the watcher thread)."""
    samples = samples or _samples
    for cam in cfg["cameras"]:
        if meter is not None:
            samples.add(f"fps:{cam['id']}", meter.reading(cam["id"]).get("fps"))
    for side in cfg["sides"]:
        hb = (snapshot["heartbeats"].get(side["inspection_worker"]) or {}).get("payload") or {}
        samples.add(f"ips:{side['side']}", hb.get("fps"))
        samples.add(f"ms:{side['side']}", hb.get("ms_per_image"))
    if gpu.get("ok") and gpu.get("gpus"):
        samples.add("gpu_temp", gpu["gpus"][0].get("temperature_c"))
    if disk.get("ok"):
        samples.add("disk", disk.get("percent"))
