"""Live overlay for the UI (STATUS F60, D-098 item 6) and the live frame + detections pair (F65).

Every OVERLAY_INTERVAL_S seconds inspection writes `<root>/data/state/<worker_id>/live_overlay.jpg`
(atomic): the frame it just processed, downscaled to OVERLAY_WIDTH, with
  - every detection box in its class colour (the same colour for a class on every image),
  - the auto-ROI band (top and bottom line) of each class present in the frame,
  - off-path detections in red: removed in `filter` mode, only marked in `observe` mode,
  - a small legend: side, wheel number, auto-ROI mode, counts, waiting for plate, time.
F65: at the same moment, as a matching pair, so the UI can draw only the classes a person selects:
  live_frame.jpg        the same frame downscaled to OVERLAY_WIDTH, no drawing
  live_detections.json  {frame, time, seq, size [w, h] of live_frame.jpg, frame_mtime_ns,
                         boxes [{class, box [x1, y1, x2, y2] in live_frame pixels, score, off_path}],
                         bands {class: [[x, y_min, y_max], ...] in live_frame pixels}, auto_roi_mode}
  Boxes are the detections BEFORE auto-ROI filtering; off_path marks the ones filter mode removes.
  Bands: every learned class over the full width (D-094), when a roi.json for this frame size exists.
  Order: live_frame.jpg is replaced first, live_detections.json last (each atomically). The JSON
  holds the image file's mtime_ns: a reader that sees a different mtime on live_frame.jpg has caught
  the next pair halfway and simply reads again.
Read-only for inspection: it draws on a copy and never changes a detection or a defect. Any error
is logged and skipped; inspection continues.
"""

import datetime
import os
import time

import cv2
import numpy as np

from auto_roi import atomic_write_jpg, atomic_write_json

OVERLAY_FILE = "live_overlay.jpg"
FRAME_FILE, DETECTIONS_FILE = "live_frame.jpg", "live_detections.json"      # F65
OVERLAY_INTERVAL_S = 3.0          # "every few seconds" (F60); the UI refreshes the image on its own
OVERLAY_WIDTH = 960               # px; height keeps the camera's aspect ratio
RED = (60, 60, 240)               # off-path, BGR (D-098 red family)
# class colours: no red, so red always means "off path"
CLASS_COLOURS = [(0, 230, 255), (255, 0, 255), (255, 200, 0), (0, 210, 0), (255, 255, 255),
                 (0, 160, 255), (200, 120, 255), (160, 255, 120), (255, 160, 120), (120, 200, 200)]


# fixed, clearly different colours for the classes the checks use (BGR); others by name hash
KNOWN_COLOURS = {
    "wheel": (255, 255, 255), "half_wheel": (190, 190, 190),
    "bucket_clip": (0, 230, 255), "half_bucket_clip": (0, 160, 255),
    "nut_bolt": (255, 0, 255), "high_dusty_bucket_clip": (255, 200, 0),
    "wheel_magnet": (0, 210, 0), "wheel_bolt": (160, 255, 120),
    "plate": (200, 120, 255), "wheel_stand": (255, 160, 120),
}


def class_colour(name):
    """Stable colour per class name (same on every frame and both sides); never red."""
    return KNOWN_COLOURS.get(name) or CLASS_COLOURS[sum(name.encode()) % len(CLASS_COLOURS)]


def draw_overlay(image, raw_by_class, outside, roi_paths, info):
    """Annotated, downscaled copy. raw_by_class: {class: [detections]} before auto-ROI;
    outside: [(class, detection)] off-path in this frame; roi_paths: auto_roi.RoiPaths or None;
    info: {side, mode, wheel, waiting_for_plate}."""
    h, w = image.shape[:2]
    scale = OVERLAY_WIDTH / w
    out = cv2.resize(image, (OVERLAY_WIDTH, max(1, int(round(h * scale)))), interpolation=cv2.INTER_AREA)
    off_ids = {id(det) for _, det in outside}

    if roi_paths is not None:                         # bands of the classes present in this frame
        for cls in sorted(raw_by_class):
            if not raw_by_class[cls] or not roi_paths.learned(cls):
                continue
            xs = np.linspace(0, w, 25)
            band = [roi_paths.band(cls, x) for x in xs]
            for edge in (0, 1):
                line = np.array([[x * scale, b[edge] * scale] for x, b in zip(xs, band)], np.int32)
                cv2.polylines(out, [line], False, class_colour(cls), 1, cv2.LINE_AA)

    detections = 0
    for cls in sorted(raw_by_class):
        for det in raw_by_class[cls]:
            detections += 1
            off = id(det) in off_ids
            colour = RED if off else class_colour(cls)
            x1, y1, x2, y2 = (int(round(v * scale)) for v in (det[3], det[1], det[4], det[2]))
            cv2.rectangle(out, (x1, y1), (x2, y2), colour, 2 if off else 1, cv2.LINE_AA)
            cv2.putText(out, cls, (x1, max(10, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, colour, 1, cv2.LINE_AA)

    mode = info.get("mode", "off")
    red_meaning = {"filter": "red = off path, removed", "observe": "red = off path (would be removed in filter)"}
    lines = [f"{info.get('side', '')}  wheel {info.get('wheel', '-')}  auto-ROI {mode}  "
             f"{datetime.datetime.now():%H:%M:%S}",
             f"{detections} detections, {len(outside)} off path"
             + ("  |  WAITING FOR PLATE" if info.get("waiting_for_plate") else "")]
    if outside or mode in red_meaning:
        lines.append(red_meaning.get(mode, "auto-ROI off: no path check"))
    cv2.rectangle(out, (0, 0), (min(OVERLAY_WIDTH, 470), 8 + 18 * len(lines)), (0, 0, 0), cv2.FILLED)
    for k, text in enumerate(lines):
        colour = (120, 200, 255) if "WAITING" in text else (230, 232, 235)
        cv2.putText(out, text, (6, 20 + 18 * k), cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, 1, cv2.LINE_AA)
    return out


def downscale(image):
    h, w = image.shape[:2]
    scale = OVERLAY_WIDTH / w
    return cv2.resize(image, (OVERLAY_WIDTH, max(1, int(round(h * scale)))), interpolation=cv2.INTER_AREA), scale


def live_detections(raw_by_class, outside, roi_paths, full_size, small_size, info):
    """The F65 JSON content (without frame_mtime_ns / seq). full_size, small_size: (w, h)."""
    sx, sy = small_size[0] / full_size[0], small_size[1] / full_size[1]
    off_ids = {id(det) for _, det in outside}
    boxes = []
    for cls in sorted(raw_by_class):
        for det in raw_by_class[cls]:
            boxes.append({"class": cls,
                          "box": [round(det[3] * sx, 1), round(det[1] * sy, 1), round(det[4] * sx, 1), round(det[2] * sy, 1)],
                          "score": round(float(det[0]), 3), "off_path": id(det) in off_ids})
    bands = {}
    if roi_paths is not None:
        for cls in sorted(roi_paths.paths):
            xs = sorted({0.0, float(full_size[0])} | {float(x) for x in roi_paths.paths[cls][0]})
            bands[cls] = [[round(x * sx, 1)] + [round(v * sy, 1) for v in roi_paths.band(cls, x)] for x in xs]
    return {"frame": info.get("frame"), "time": datetime.datetime.now().isoformat(timespec="milliseconds"),
            "size": list(small_size), "boxes": boxes, "bands": bands, "auto_roi_mode": info.get("mode", "off")}


class LiveOverlay:
    def __init__(self, folder, logger, interval_s=OVERLAY_INTERVAL_S, clock=time.monotonic):
        self.path = os.path.join(folder, OVERLAY_FILE)
        self.frame_path = os.path.join(folder, FRAME_FILE)
        self.detections_path = os.path.join(folder, DETECTIONS_FILE)
        self.seq = 0
        self.log, self.interval, self.clock = logger, interval_s, clock
        self.last = None
        os.makedirs(folder, exist_ok=True)

    def maybe_write(self, image, raw_by_class, outside, roi_paths, info) -> bool:
        now = self.clock()
        if self.last is not None and now - self.last < self.interval:
            return False
        self.last = now
        try:
            atomic_write_jpg(self.path, draw_overlay(image, raw_by_class, outside, roi_paths, info))
            self.write_pair(image, raw_by_class, outside, roi_paths, info)
            return True
        except Exception:
            self.log.exception("live overlay: not written (inspection continues)")
            return False

    def write_pair(self, image, raw_by_class, outside, roi_paths, info):
        """F65: live_frame.jpg first, then live_detections.json naming its mtime (module doc)."""
        small, _ = downscale(image)
        atomic_write_jpg(self.frame_path, small)
        self.seq += 1
        data = live_detections(raw_by_class, outside, roi_paths, (image.shape[1], image.shape[0]),
                               (small.shape[1], small.shape[0]), info)
        data.update(seq=self.seq, frame_mtime_ns=os.stat(self.frame_path).st_mtime_ns)
        atomic_write_json(self.detections_path, data)
