"""Files the UI reads, always read-only (CONTRACTS 4.1, D-031):

* live image  data/frames/<CAM>/live/IMG_1.jpg: opened "rb" and never deleted, renamed or
  modified (it is the Supervisor's capture health signal; deleting it restarts the capture,
  STATUS F45). FrameCapture replaces it with os.replace, so a read always sees a whole file.
* frame meter: stats the live file 10 times a second; each new inode/mtime is a new frame.
  Gives frames per second and frame age without a capture heartbeat (request R5).
* F66: the dashboard and the camera page "Live" tab show only this plain image; detections are
  drawn on demand from the F65 pair (sources/detections.py), never burnt into the live image.
* log tail: the last lines of a worker's log file, only inside paths.logs.
* defect image: the `view_defect` path, only inside paths.data, with the box drawn.
"""
import collections
import logging
import os
import threading
import time

from monitor.secrets import mask

log = logging.getLogger("dpc.ui.files")

try:
    import cv2
    import numpy as np
except ImportError:          # the UI still works; images are served unscaled, without boxes
    cv2 = None
    np = None

LIVE_WIDTH = 960
_live_cache = {}             # camera_id -> (key, width, jpeg bytes)
_live_lock = threading.Lock()


def live_path(frames_dir: str, camera_id: str) -> str:
    return os.path.join(frames_dir, camera_id, "live", "IMG_1.jpg")


def inside(path: str, folder: str) -> bool:
    """True if `path` (symlinks resolved) lies inside `folder`."""
    try:
        real, base = os.path.realpath(path), os.path.realpath(folder)
    except (OSError, ValueError):
        return False
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)


def _scaled_jpeg(data: bytes, width: int, box=None, quality=75) -> bytes:
    if cv2 is None:
        return data
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return data
    if box is not None:
        x1, y1, x2, y2 = (int(v) for v in box)
        thickness = max(3, image.shape[1] // 400)
        cv2.rectangle(image, (x1, y1), (x2, y2), (0, 0, 255), thickness)
    if width and image.shape[1] > width:
        height = int(image.shape[0] * width / image.shape[1])
        image = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return encoded.tobytes() if ok else data


def live_image(frames_dir: str, camera_id: str, width: int = LIVE_WIDTH):
    """Downscaled JPEG bytes of the current live frame, or None. Cached per frame."""
    path = live_path(frames_dir, camera_id)
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_ino, st.st_mtime_ns)
    with _live_lock:
        cached = _live_cache.get(camera_id)
        if cached and cached[0] == key and cached[1] == width:
            return cached[2]
    try:
        with open(path, "rb") as f:            # read-only; never delete or modify (D-031)
            data = f.read()
    except OSError:
        return None
    jpeg = _scaled_jpeg(data, width)
    with _live_lock:
        _live_cache[camera_id] = (key, width, jpeg)
    return jpeg


class FrameMeter:
    """Frames per second and frame age per camera, from the live file alone."""
    WINDOW_S = 10.0

    def __init__(self, frames_dir: str, camera_ids, clock=time.time, stat=os.stat):
        self.frames_dir = frames_dir
        self.camera_ids = list(camera_ids)
        self._clock, self._stat = clock, stat
        self._last_key = {}
        self._changes = {cam: collections.deque() for cam in self.camera_ids}
        self._mtime = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def sample(self):
        now = self._clock()
        for cam in self.camera_ids:
            try:
                st = self._stat(live_path(self.frames_dir, cam))
            except OSError:
                continue
            key = (st.st_ino, st.st_mtime_ns)
            with self._lock:
                if self._last_key.get(cam) not in (None, key):
                    self._changes[cam].append(now)
                self._last_key[cam] = key
                self._mtime[cam] = st.st_mtime
                while self._changes[cam] and now - self._changes[cam][0] > self.WINDOW_S:
                    self._changes[cam].popleft()

    def reading(self, cam: str) -> dict:
        now = self._clock()
        with self._lock:
            mtime = self._mtime.get(cam)
            changes = len(self._changes.get(cam, ()))
        return {"fps": round(changes / self.WINDOW_S, 1) if mtime is not None else None,
                "age_s": round(max(0.0, now - mtime), 1) if mtime is not None else None}

    def run(self, interval=0.1):
        while not self._stop.wait(interval):
            self.sample()

    def start(self):
        threading.Thread(target=self.run, name="frame-meter", daemon=True).start()

    def stop(self):
        self._stop.set()


_meter = None


def get_meter():
    return _meter


def set_meter(meter):
    global _meter
    _meter = meter


# ------------------------------------------------------------------------------- logs
def log_tail(path: str, logs_dir: str, lines: int = 200, text: str = "", level: str = "", max_bytes=512 * 1024):
    """Last `lines` lines of a log inside logs_dir, optionally filtered; secrets masked.
    Returns (lines, error)."""
    if not path:
        return [], "This program has no log file."
    if not inside(path, logs_dir):
        return [], "The log file is outside the logs folder; not shown."
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return [], "The log file does not exist yet."
    except OSError as e:
        return [], f"The log file cannot be read: {e}"
    found = data.splitlines()
    if size > max_bytes:
        found = found[1:]                   # first line is probably cut in half
    if level:
        wanted = {"ERROR": ("ERROR", "CRITICAL", "Traceback"),
                  "WARNING": ("WARNING", "ERROR", "CRITICAL", "Traceback")}.get(level.upper(), (level.upper(),))
        found = [ln for ln in found if any(w in ln for w in wanted)]
    if text:
        needle = text.lower()
        found = [ln for ln in found if needle in ln.lower()]
    return [mask(ln) for ln in found[-lines:]], ""


# ------------------------------------------------------------------------------- defects
def defect_image(path: str, data_dir: str, box=None, width: int = 1280):
    """JPEG bytes with the box drawn, or (None, reason)."""
    if not path:
        return None, "This defect has no image."
    if not inside(path, data_dir):
        return None, "The image is not in this machine's data folder (an old path?)."
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None, "The image file no longer exists (removed by the cleanup?)."
    return _scaled_jpeg(data, width, box=box, quality=85), ""
