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

    def increase(self, name: str, window_s: float = KEEP_S):
        """How much a counter rose within the window; a drop (program restarted, counter reset)
        restarts the count from the new value. None without two samples."""
        now = self._clock()
        with self._lock:
            points = [v for t, v in self._series.get(name, ()) if now - t <= window_s]
        if len(points) < 2:
            return None
        rise = 0.0
        for before, after in zip(points, points[1:]):
            rise += (after - before) if after >= before else after
        return rise

    def values(self, name: str):
        with self._lock:
            return [v for _, v in self._series.get(name, ())]


class PlateClock:
    """When the last plate passed, seen from the UI: the live wheel number drops back when a new
    cycle starts (D-101 top bar). No heartbeat publishes this time yet; unknown until the first
    drop after the UI started."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self.last_value = None
        self.last_plate_at = None
        self.since = clock()

    def observe(self, wheel):
        try:
            value = int(str(wheel).strip())
        except (TypeError, ValueError):
            return
        if self.last_value is not None and value < self.last_value:
            self.last_plate_at = self._clock()
        self.last_value = value


_samples = Samples()
_plate = PlateClock()


def get_plate_clock() -> PlateClock:
    return _plate


def set_plate_clock(clock: PlateClock):
    global _plate
    _plate = clock


def get_samples() -> Samples:
    return _samples


def set_samples(samples: Samples):
    global _samples
    _samples = samples


def take(cfg, snapshot, meter, gpu, disk, samples=None, wheel=None, roi_outside=None):
    """One sample of every series (called by the watcher thread). roi_outside: {side: total
    outside count from roi_status.json}; wheel: the live wheel number (plate clock)."""
    samples = samples or _samples
    for side, total in (roi_outside or {}).items():
        samples.add(f"roi_out:{side}", total)
    if wheel is not None:
        _plate.observe(wheel)
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
