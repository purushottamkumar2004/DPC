"""Detections on demand for the camera page (F66), from the pair inspection writes (F65) in
<data>/state/<inspection worker>/:
  live_frame.jpg        the processed frame, downscaled, no drawing
  live_detections.json  {frame, time, seq, size [w, h], frame_mtime_ns, boxes [{class, box, score,
                        off_path}], bands {class: [[x, y_min, y_max], ...]}, auto_roi_mode}
Inspection replaces the image first and the JSON last; the JSON names the image's mtime_ns. A
pair is used only when the image on disk has exactly that mtime (read before and after), so the
image and the boxes are always the same frame. Matched pairs are kept in memory by seq: the
browser asks for the JSON, then for /frame/<seq>.jpg, and gets that very image even if the next
pair has been written meanwhile.

Colours (D-098): every class has its own colour, never red; red means off path. The text for an
off-path box follows the auto-ROI mode: "removed" (filter) or "would be removed" (observe).
"""
import collections
import datetime
import hashlib
import json
import os
import threading
import time

FRAME_FILE, DETECTIONS_FILE = "live_frame.jpg", "live_detections.json"
OFF_PATH_COLOUR = "#f0605d"                       # D-098 red
# class colours: distinct, none of them red / amber / green (those carry status meaning)
CLASS_COLOURS = ["#5ec8f2", "#c792ea", "#e6e8eb", "#7fdbca", "#8fa8ff", "#f2a6d5", "#b8c4ce", "#9be0ff",
                 "#d7b8ff", "#a0e7e5"]
KEEP_PAIRS = 4
NOT_WRITTEN = ("Inspection has not written detections yet; it needs the F65 version "
               "(restart inspection).")
STALE_S = 30                                     # older detections: say so on the page
TRIES = 4


def class_colour(name: str) -> str:
    """Same colour for a class everywhere (both sides, every frame)."""
    digest = hashlib.md5(name.encode("utf-8")).digest()
    return CLASS_COLOURS[digest[0] % len(CLASS_COLOURS)]


def off_path_note(mode: str) -> str:
    return {"filter": "removed", "observe": "would be removed"}.get((mode or "").lower(), "off path")


class PairReader:
    """One per process; thread-safe; keeps the last KEEP_PAIRS matched pairs per folder."""

    def __init__(self, sleep=time.sleep, now=time.time):
        self._pairs = collections.defaultdict(collections.OrderedDict)    # folder -> seq -> (data, jpeg)
        self._lock = threading.Lock()
        self._sleep, self._now = sleep, now

    def _read_pair(self, folder):
        frame = os.path.join(folder, FRAME_FILE)
        for _ in range(TRIES):
            try:
                with open(os.path.join(folder, DETECTIONS_FILE), "r", encoding="utf-8") as f:
                    data = json.load(f)
                want = int(data.get("frame_mtime_ns"))
                if os.stat(frame).st_mtime_ns == want:
                    with open(frame, "rb") as f:            # read-only
                        jpeg = f.read()
                    if os.stat(frame).st_mtime_ns == want:   # not replaced while reading
                        return data, jpeg
            except (OSError, ValueError, TypeError):
                pass
            self._sleep(0.05)                                 # caught a pair halfway: read again
        return None, None

    def latest(self, folder: str) -> dict:
        """What the page draws: {"available": False, "reason"} or the pair's data plus seq/age."""
        detections = os.path.join(folder, DETECTIONS_FILE)
        if not os.path.exists(detections) and not os.path.exists(os.path.join(folder, FRAME_FILE)):
            return {"available": False, "reason": NOT_WRITTEN}
        data, jpeg = self._read_pair(folder)
        with self._lock:
            pairs = self._pairs[folder]
            if data is not None:
                seq = int(data.get("seq") or 0)
                pairs[seq] = (data, jpeg)
                pairs.move_to_end(seq)
                while len(pairs) > KEEP_PAIRS:
                    pairs.popitem(last=False)
            elif pairs:                                       # still being written: keep the last pair
                data = next(reversed(pairs.values()))[0]
        if data is None:
            return {"available": False, "reason": "Detections are being updated; trying again."}
        return self._describe(data)

    def image(self, folder: str, seq: int):
        with self._lock:
            pair = self._pairs.get(folder, {}).get(seq)
        return pair[1] if pair else None

    def _describe(self, data: dict) -> dict:
        mode = (data.get("auto_roi_mode") or "off").lower()
        note = off_path_note(mode)
        boxes, counts = [], collections.Counter()
        off = collections.Counter()
        for b in data.get("boxes") or []:
            cls, box = str(b.get("class")), b.get("box") or []
            if len(box) != 4:
                continue
            is_off = bool(b.get("off_path"))
            boxes.append({"class": cls, "box": [float(v) for v in box], "score": b.get("score"),
                          "off_path": is_off, "colour": OFF_PATH_COLOUR if is_off else class_colour(cls),
                          "note": note if is_off else ""})
            counts[cls] += 1
            off[cls] += int(is_off)
        bands = {cls: pts for cls, pts in (data.get("bands") or {}).items() if isinstance(pts, list)}
        names = sorted(set(counts) | set(bands))
        try:
            age = max(0.0, self._now() - datetime.datetime.fromisoformat(str(data.get("time"))).timestamp())
        except ValueError:
            age = None
        return {"available": True, "seq": int(data.get("seq") or 0), "frame": data.get("frame"),
                "time": data.get("time"), "age_s": round(age, 1) if age is not None else None,
                "stale": age is not None and age > STALE_S, "size": data.get("size") or [960, 540],
                "auto_roi_mode": mode, "off_note": note, "off_colour": OFF_PATH_COLOUR, "boxes": boxes,
                "bands": bands, "classes": [{"name": n, "colour": class_colour(n), "count": counts[n],
                                             "off": off[n], "band": n in bands} for n in names]}


_reader = PairReader()


def get_reader() -> PairReader:
    return _reader


def set_reader(reader: PairReader):
    global _reader
    _reader = reader
