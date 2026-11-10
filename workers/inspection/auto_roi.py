"""Auto region of interest per class and side (STATUS P13; D-054, D-090, D-091, D-092 wins).

Every object of a class travels along a fixed path on the conveyor: at a given x it is always
at about the same y. The region of a class is that path y(x) with a band around it.

LEARNING (only on request, D-092 item 1). A person (later the UI) creates
`<root>/run/<worker_id>.roi_learn`. From the next plate to the following plate (one cycle),
while inspection keeps running, every detection of a frame in which the conveyor moved is
kept. A frame counts as moving when the wheel advanced in the side's `motion` direction
since the previous frame (no per-object tracking, D-092 item 2a). At the second plate:
  1. per class, a box location that recurs within `static_px` in more than `static_share` of
     the used frames is a stationary false detection (reflection, stain) and is dropped (2b);
  2. robust straight line of the box centres, centre y over x (Theil-Sen); boxes whose
     residual is more than `mad_factor` x MAD are dropped (D-091 a);
  3. grid of `grid_steps` + 1 points over the frame width; per point the boxes whose centre
     is within half a step: y_min = `top_percentile` of the top edges - `margin_px`,
     y_max = `bottom_percentile` of the bottom edges + `margin_px` (D-091 b);
  4. a point with fewer than `min_per_point` boxes is interpolated from neighbours; a class
     without any such point is stored as not learned and is never filtered (D-091 c, d);
  5. x_range = 1st..99th percentile of the centres +- `margin_px` (information only, D-094).
`roi.json` + preview images (`roi_preview_<NN>_<classA>__<classB>.jpg`, F56) are written to
`<root>/data/state/<worker_id>/` (atomic; the previous roi.json kept as `roi.prev.json`; old previews
of the side removed first) and the request file is deleted. Never automatic.

USING (`auto_roi.mode`). Only y is restricted, never x (D-094): a detection is inside its class
path when, at its centre x, its box top >= y_min and bottom <= y_max. The band is linear between
the stored points and continues beyond the outermost point along the class's line slope with that
point's band height, so it covers the full frame width.
  off      nothing is checked.
  observe  every detection is checked, nothing is removed (defects identical to off).
  filter   off-path detections are removed before every check.
Without roi.json every mode behaves like off. Classes not learned are never checked.
In observe and filter every off-path detection goes to `roi_outside.jsonl` (+ a crop), and
`roi_status.json` holds per-class counts for the UI. Drift (too many outside among the last
`drift_window` MOVING detections of a class) is only reported: WARNING + event `roi_drift`;
nothing is ever acted on (D-092 item 3). Moving = learning's rule (F55): in a frame where the wheel
moved, and not a stationary repeat (a box location seen within `static_px` in more than
`static_share` of the class's recent moving frames). A stationary off-path detection is still
counted and listed as outside, but never raises drift.

Nothing here may stop inspection: every error in the ROI part is logged and skipped.
"""

import datetime
import json
import os
from collections import defaultdict, deque

import cv2
import numpy as np

SCHEMA_VERSION = 1
ROI_FILE, PREV_FILE = "roi.json", "roi.prev.json"
PREVIEW_PREFIX = "roi_preview_"          # roi_preview_<NN>_<classA>__<classB>.jpg (F56)
LEGACY_PREVIEW = "roi_preview.jpg"       # the single all-classes image before F56; removed with the old ones
STATIONARY_MIN_FRAMES = 10               # F55: history needed before a box can be judged stationary
OUTSIDE_FILE, OUTSIDE_DIR, STATUS_FILE = "roi_outside.jsonl", "roi_outside", "roi_status.json"
REQUEST_SUFFIX = ".roi_learn"
X_RANGE_PERCENTILES = (1.0, 99.0)
THEIL_SEN_MAX_PAIRS = 20000         # sampled pairs for large classes (fixed seed: same data = same line)
STATUS_EVERY_FRAMES = 100
TRIM_SLACK = 1.2                    # the outside list is trimmed back to outside_keep at 120 %


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def atomic_write_jpg(path, image):
    tmp = path[:-4] + ".tmp.jpg"
    if not cv2.imwrite(tmp, image):
        raise OSError(f"could not write {tmp}")
    os.replace(tmp, path)


def box_of(det):
    """Detection [score, ymin, ymax, xmin, xmax, cls] -> (xmin, ymin, xmax, ymax)."""
    return det[3], det[1], det[4], det[2]


# ======================================================================
# Conveyor movement, judged from the wheel (D-092 item 2a)
# ======================================================================
class MotionJudge:
    """Moving = the wheel edge the side's counter follows advanced by at least `min_step_px`
    in the travel direction since the previous frame, or a new wheel took over (the edge
    jumped back by more than `jump_px`, as in the wheel counter). Frames without a wheel are
    not judged as moving (no evidence)."""

    def __init__(self, motion, min_step_px, jump_px):
        self.motion, self.min_step, self.jump = motion, min_step_px, jump_px
        self.last_edge = None

    def moved(self, wheels) -> bool:
        if not wheels:
            self.last_edge = None
            return False
        edge = min(d[3] for d in wheels) if self.motion == "rtl" else max(d[4] for d in wheels)
        last, self.last_edge = self.last_edge, edge
        if last is None:
            return False
        step = (last - edge) if self.motion == "rtl" else (edge - last)
        return step >= self.min_step or step < -self.jump


class StationaryWatch:
    """F55: is a detection a stationary repeat? Per class, the box locations of the last `window`
    moving frames (buckets of static_px, as in learning); a box is stationary when its location
    (with the neighbouring buckets) was seen in more than `share` of those frames."""

    OFFSETS = [(a, b, c, d) for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1) for d in (-1, 0, 1)]

    def __init__(self, static_px, share, window):
        self.size, self.share, self.window = max(1, static_px), share, window
        self.history = defaultdict(deque)            # class -> deque of sets of bucket keys (one per frame)
        self.counts = defaultdict(lambda: defaultdict(int))

    def _key(self, box):
        return tuple(int(v) // self.size for v in box)

    def stationary(self, cls, box) -> bool:
        frames = len(self.history[cls])
        if frames < STATIONARY_MIN_FRAMES:
            return False
        k, counts = self._key(box), self.counts[cls]
        seen = sum(counts.get((k[0] + o[0], k[1] + o[1], k[2] + o[2], k[3] + o[3]), 0) for o in self.OFFSETS)
        return min(seen, frames) > self.share * frames

    def add_frame(self, cls, boxes):
        keys = {self._key(b) for b in boxes}
        history, counts = self.history[cls], self.counts[cls]
        history.append(keys)
        for k in keys:
            counts[k] += 1
        if len(history) > self.window:
            for k in history.popleft():
                counts[k] -= 1
                if counts[k] == 0:
                    del counts[k]


# ======================================================================
# The calculation (pure functions, tested directly)
# ======================================================================
def stationary_mask(boxes, frames, frames_used, static_px, static_share):
    """True for boxes at a location that recurs (within about static_px on every edge) in more
    than static_share of the used frames. Buckets of static_px with their neighbours."""
    n = len(boxes)
    if n == 0 or frames_used == 0:
        return np.zeros(n, bool)
    size = max(1, static_px)
    keys = [tuple(int(v) // size for v in b) for b in boxes]
    frames_at = defaultdict(set)
    for key, frame in zip(keys, frames):
        frames_at[key].add(frame)
    limit = static_share * frames_used
    offsets = [(a, b, c, d) for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1) for d in (-1, 0, 1)]
    repeated = {}
    for key in frames_at:
        seen = set()
        for o in offsets:
            seen |= frames_at.get((key[0] + o[0], key[1] + o[1], key[2] + o[2], key[3] + o[3]), set())
        repeated[key] = len(seen) > limit
    return np.array([repeated[k] for k in keys], bool)


def theil_sen(x, y):
    """Robust line y = slope * x + intercept (median of pair slopes; sampled for big n)."""
    n = len(x)
    if n < 2:
        return 0.0, float(np.median(y)) if n else 0.0
    if n * (n - 1) // 2 <= THEIL_SEN_MAX_PAIRS:
        i, j = np.triu_indices(n, 1)
    else:
        rng = np.random.default_rng(0)
        i = rng.integers(0, n, THEIL_SEN_MAX_PAIRS)
        j = rng.integers(0, n, THEIL_SEN_MAX_PAIRS)
    dx, dy = x[j] - x[i], y[j] - y[i]
    keep = np.abs(dx) >= 1.0
    slope = float(np.median(dy[keep] / dx[keep])) if keep.any() else 0.0
    intercept = float(np.median(y - slope * x))
    return slope, intercept


def band_at(xs, ymins, ymaxs, slope, x):
    """The band (y_min, y_max) of a class at x (D-094). Between the stored points: linear.
    Beyond the outermost point: continued along the class's robust line slope with that point's
    band height, so the band covers the full frame width (no flat clamp, no cut-off)."""
    if x < xs[0]:
        shift = slope * (x - xs[0])
        return ymins[0] + shift, ymaxs[0] + shift
    if x > xs[-1]:
        shift = slope * (x - xs[-1])
        return ymins[-1] + shift, ymaxs[-1] + shift
    return float(np.interp(x, xs, ymins)), float(np.interp(x, xs, ymaxs))


def learn_class(boxes, frames, frames_used, width, cfg):
    """One class. boxes: (n, 4) xmin, ymin, xmax, ymax; frames: frame index per box.
    Returns the roi.json entry for the class."""
    boxes = np.asarray(boxes, float).reshape(-1, 4)
    total = len(boxes)
    static = stationary_mask(boxes, frames, frames_used, cfg.static_px, cfg.static_share)
    kept = boxes[~static]
    entry = {"boxes": int(total), "dropped_static": int(static.sum())}
    if len(kept) < cfg.min_per_point:
        entry.update(learned=False, reason=f"only {len(kept)} usable boxes (min_per_point {cfg.min_per_point})")
        return entry

    cx, cy = (kept[:, 0] + kept[:, 2]) / 2, (kept[:, 1] + kept[:, 3]) / 2
    slope, intercept = theil_sen(cx, cy)
    residual = cy - (slope * cx + intercept)
    mad = float(np.median(np.abs(residual - np.median(residual))))
    on_line = np.abs(residual - np.median(residual)) <= cfg.mad_factor * max(mad, 1.0)
    kept, cx = kept[on_line], cx[on_line]
    entry.update(dropped_outliers=int((~on_line).sum()), line={"slope": round(slope, 5), "intercept": round(intercept, 1)})

    step = width / cfg.grid_steps
    margin = cfg.margin_px
    lo = max(0.0, float(np.percentile(cx, X_RANGE_PERCENTILES[0])) - margin)
    hi = min(float(width), float(np.percentile(cx, X_RANGE_PERCENTILES[1])) + margin)
    measured = {}                                   # grid x -> (y_min, y_max, count)
    for k in range(cfg.grid_steps + 1):
        gx = k * step
        near = np.abs(cx - gx) <= step / 2
        if near.sum() >= cfg.min_per_point:
            window = kept[near]
            measured[gx] = (float(np.percentile(window[:, 1], cfg.top_percentile)) - margin,
                            float(np.percentile(window[:, 3], cfg.bottom_percentile)) + margin,
                            int(near.sum()))
    if not measured:
        entry.update(learned=False, reason=f"no grid point has {cfg.min_per_point} boxes")
        return entry

    mx = np.array(sorted(measured))
    ymins = np.array([measured[x][0] for x in mx])
    ymaxs = np.array([measured[x][1] for x in mx])
    xs = sorted({lo, hi} | {k * step for k in range(cfg.grid_steps + 1) if lo <= k * step <= hi})
    points = [[int(round(x))] + [int(round(v)) for v in band_at(mx, ymins, ymaxs, slope, x)]
              for x in xs]                         # beyond the measured points: along the line slope (D-094)
    entry.update(learned=True, x_range=[int(round(lo)), int(round(hi))], points=points,
                 measured_points=len(measured), used_boxes=int(len(kept)))
    return entry


def learn_all(boxes_by_class, frames_used, width, cfg):
    return {cls: learn_class([b for _, b in items], [f for f, _ in items], frames_used, width, cfg)
            for cls, items in sorted(boxes_by_class.items())}


# ======================================================================
# Learned paths (read from roi.json)
# ======================================================================
class RoiPaths:
    def __init__(self, data):
        self.data = data
        self.frame_size = tuple(data["learned_from"]["frame_size"])
        self.paths = {}
        for cls, entry in data["classes"].items():
            if entry.get("learned"):
                pts = np.array(entry["points"], float)
                order = np.argsort(pts[:, 0])
                slope = float(entry.get("line", {}).get("slope", 0.0))
                self.paths[cls] = (pts[order, 0], pts[order, 1], pts[order, 2], slope)

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"schema_version {data.get('schema_version')!r}, expected {SCHEMA_VERSION}")
        return cls(data)

    def learned(self, cls) -> bool:
        return cls in self.paths

    def band(self, cls, x):
        xs, ymins, ymaxs, slope = self.paths[cls]
        return band_at(xs, ymins, ymaxs, slope, x)

    def distance_outside(self, cls, box) -> float:
        """0 when inside the class band, else how far outside in px (y only, D-094: every x is
        valid; x_range is never a limit). Classes not learned: 0."""
        if cls not in self.paths:
            return 0.0
        xmin, ymin, xmax, ymax = box
        top_limit, bottom_limit = self.band(cls, (xmin + xmax) / 2)
        return float(max(top_limit - ymin, ymax - bottom_limit, 0.0))


PAIR_COLOURS = [(0, 230, 255), (255, 0, 255), (255, 200, 0), (0, 200, 0), (0, 0, 255), (255, 255, 255),
                (0, 128, 255), (180, 0, 120), (128, 255, 0)]      # BGR: yellow, magenta, cyan, green, red, ...


class PreviewFrames:
    """Real frames for the previews, kept while learning (F56): a frame is kept only when it shows a
    class not yet covered, so a few full frames in memory instead of one per class."""

    def __init__(self):
        self.frames = []                     # [(set of classes, image)]
        self.covered = set()

    def add(self, classes, image):
        new = set(classes) - self.covered
        if new:
            self.frames.append((set(classes), image.copy()))
            self.covered |= set(classes)

    def pick(self, wanted):
        """The kept frame showing most of the wanted classes (all of them if possible)."""
        if not self.frames:
            return None
        return max(self.frames, key=lambda item: len(item[0] & set(wanted)))[1]


def preview_name(index, classes):
    safe = ["".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in c) for c in classes]
    return f"{PREVIEW_PREFIX}{index:02d}_{'__'.join(safe) or 'none'}.jpg"


def draw_preview(image, data, classes=None, index=1, total=1):
    """The bands of `classes` (default: every learned class) on a frame, over the full width (D-094):
    a light fill plus the top and bottom lines in the class colour, a legend with the names, and the
    classes not learned as text (they are never filtered)."""
    out = image.copy()
    h, w = out.shape[:2]
    learned_all = {c: e for c, e in data["classes"].items() if e.get("learned")}
    classes = list(learned_all) if classes is None else list(classes)
    paths = RoiPaths({"learned_from": {"frame_size": [w, h]}, "classes": {c: learned_all[c] for c in classes}})
    overlay = out.copy()
    bands = {}
    for i, cls in enumerate(classes):
        xs = sorted({0, w} | {p[0] for p in learned_all[cls]["points"]})
        band = [paths.band(cls, x) for x in xs]
        top = np.array([[x, b[0]] for x, b in zip(xs, band)], np.int32)
        bottom = np.array([[x, b[1]] for x, b in zip(xs, band)], np.int32)
        bands[cls] = (top, bottom)
        cv2.fillPoly(overlay, [np.vstack([top, bottom[::-1]])], PAIR_COLOURS[i % len(PAIR_COLOURS)])
    out = cv2.addWeighted(overlay, 0.12, out, 0.88, 0)
    for i, cls in enumerate(classes):
        colour = PAIR_COLOURS[i % len(PAIR_COLOURS)]
        top, bottom = bands[cls]
        cv2.polylines(out, [top], False, colour, 3, cv2.LINE_AA)
        cv2.polylines(out, [bottom], False, colour, 3, cv2.LINE_AA)
        y = int(min(h - 10, max(20, top[0][1] + 28)))
        cv2.putText(out, cls, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, colour, 2, cv2.LINE_AA)

    not_learned = sorted(c for c, e in data["classes"].items() if not e.get("learned"))
    lines = [(f"auto-ROI {data['side']}  image {index}/{total}  learned {data['learned_from']['time']}  "
              f"frames used {data['learned_from']['frames_used']}", (255, 255, 255), False)]
    lines += [(f"{cls}  (seen at x {learned_all[cls]['x_range'][0]}-{learned_all[cls]['x_range'][1]})",
               PAIR_COLOURS[i % len(PAIR_COLOURS)], True) for i, cls in enumerate(classes)]
    if not_learned:
        lines.append(("not learned (never filtered): " + ", ".join(not_learned), (190, 190, 190), False))
    width = min(w, max(500, 14 * max(len(t) for t, _, _ in lines)))
    cv2.rectangle(out, (0, 0), (width, 14 + 30 * len(lines)), (0, 0, 0), cv2.FILLED)
    for k, (text, colour, swatch) in enumerate(lines):
        x = 10
        if swatch:
            cv2.rectangle(out, (10, 14 + 30 * k), (40, 34 + 30 * k), colour, cv2.FILLED)
            x = 50
        cv2.putText(out, text, (x, 32 + 30 * k), cv2.FONT_HERSHEY_SIMPLEX, 0.75, colour, 2, cv2.LINE_AA)
    return out


def remove_previews(folder):
    for name in os.listdir(folder):
        if (name.startswith(PREVIEW_PREFIX) and name.endswith(".jpg")) or name == LEGACY_PREVIEW:
            os.remove(os.path.join(folder, name))


def write_previews(folder, data, frames, per_image):
    """F56: the old previews of the side are removed, then one image per `per_image` learned classes
    (sorted names; the last may have fewer). Returns the file names."""
    remove_previews(folder)
    learned = sorted(c for c, e in data["classes"].items() if e.get("learned"))
    groups = [learned[i:i + per_image] for i in range(0, len(learned), per_image)] or [[]]
    names = []
    for index, group in enumerate(groups, 1):
        image = frames.pick(group)
        if image is None:
            width, height = data["learned_from"]["frame_size"]
            image = np.zeros((height, width, 3), np.uint8)
        name = preview_name(index, group)
        atomic_write_jpg(os.path.join(folder, name), draw_preview(image, data, group, index, len(groups)))
        names.append(name)
    return names


# ======================================================================
# The part inspection talks to
# ======================================================================
class AutoRoi:
    """Called once per frame by inspection (`process`) and at every new plate (`on_new_plate`).
    `events(name, payload)` publishes an event (heartbeat MQTT client), may be None."""

    def __init__(self, settings, logger, events=None, model_name="", model_version=""):
        self.s, self.cfg, self.log, self.events = settings, settings.auto_roi, logger, events
        self.model = {"name": model_name, "version": model_version}
        self.dir = os.path.join(settings.paths.root, "data", "state", settings.worker_id)
        self.request = os.path.join(settings.paths.root, "run", settings.worker_id + REQUEST_SUFFIX)
        os.makedirs(os.path.join(self.dir, OUTSIDE_DIR), exist_ok=True)
        self.judge = MotionJudge(settings.motion, self.cfg.min_wheel_step_px, settings.tracker.new_wheel_jump_px)
        self.paths = None
        self.size_mismatch_logged = False
        self.learning = None            # None | "armed" | "active"
        self.learn = None
        self.counts = defaultdict(lambda: {"inside": 0, "outside": 0})
        self.recent = defaultdict(lambda: deque(maxlen=self.cfg.drift_window))
        self.drifting = set()
        self.minute, self.written_this_minute, self.suppressed = None, 0, 0
        self.stationary = StationaryWatch(self.cfg.static_px, self.cfg.static_share, self.cfg.drift_window)
        self.previews = sorted(n for n in os.listdir(self.dir) if n.startswith(PREVIEW_PREFIX) and n.endswith(".jpg"))
        self.outside_lines = self._count_outside_lines()
        self.frames = 0
        self.last_outside = []
        self._load()

    # ---- start -------------------------------------------------------------
    def _load(self):
        path = os.path.join(self.dir, ROI_FILE)
        if not os.path.exists(path):
            self.paths = None
            self.log.info(f"auto-ROI: no {ROI_FILE} yet, nothing is checked (mode {self.cfg.mode})")
            return
        try:
            self.paths = RoiPaths.load(path)
            learned = sorted(self.paths.paths)
            self.log.info(f"auto-ROI: {ROI_FILE} from {self.paths.data['learned_from'].get('time')}, "
                          f"learned classes {learned}, mode {self.cfg.mode}")
        except Exception as e:                          # D-054: unreadable = ignored, never a crash
            self.paths = None
            self.log.error(f"auto-ROI: {path} is unreadable ({e}); ignored, nothing is checked")

    # ---- per frame -----------------------------------------------------------
    def process(self, by_class, image, frame_path):
        """Learning (if active), then observe / filter. Returns by_class, filtered in filter mode."""
        self.frames += 1
        try:
            moving = self.judge.moved(by_class.get("wheel", []) + by_class.get("half_wheel", []))
            self._learning_step(by_class, image, moving)
            result = self._check(by_class, image, frame_path, moving)
            if self.frames % STATUS_EVERY_FRAMES == 0:
                self.write_status()
            return result
        except Exception:
            self.log.exception("auto-ROI: error, skipped for this frame (inspection continues)")
            return by_class

    def on_new_plate(self):
        try:
            if self.learning == "armed":
                self.learning = "active"
                self.learn = {"boxes": defaultdict(list), "seen": 0, "used": 0, "size": None,
                              "frames": PreviewFrames(), "start": now_iso()}
                self.log.info("auto-ROI: learning started at this plate (ends at the next plate)")
            elif self.learning == "active":
                self._finish()
        except Exception:
            self.log.exception("auto-ROI: learning failed")
            self._end_learning("error", "see the log")

    # ---- learning -------------------------------------------------------------
    def _learning_step(self, by_class, image, moving):
        if self.learning is None:
            if os.path.exists(self.request):
                self.learning = "armed"
                self.log.info(f"auto-ROI: learning requested ({self.request}); starts at the next plate")
            return
        if self.learning != "active":
            return
        learn = self.learn
        learn["seen"] += 1
        if learn["seen"] > self.cfg.learn_max_frames:
            self.log.warning(f"auto-ROI: no second plate within {self.cfg.learn_max_frames} frames; learning given up")
            self._end_learning("no_second_plate", f"no second plate within {self.cfg.learn_max_frames} frames")
            return
        if not moving:
            return
        learn["used"] += 1
        learn["size"] = (image.shape[1], image.shape[0])
        learn["frames"].add([c for c, d in by_class.items() if d], image)
        for cls, dets in by_class.items():
            for det in dets:
                learn["boxes"][cls].append((learn["used"], box_of(det)))

    def _finish(self):
        learn = self.learn
        if learn["used"] == 0 or learn["size"] is None:
            self._end_learning("no_movement", "no frame with a moving wheel in the cycle")
            return
        width, height = learn["size"]
        classes = learn_all(learn["boxes"], learn["used"], width, self.cfg)
        data = {"schema_version": SCHEMA_VERSION, "side": self.s.side,
                "learned_from": {"camera_id": self.s.camera_id, "model": self.model,
                                 "frames_seen": learn["seen"], "frames_used": learn["used"],
                                 "frame_size": [width, height], "started": learn["start"], "time": now_iso()},
                "settings": {k: getattr(self.cfg, k) for k in (
                    "margin_px", "static_px", "static_share", "min_per_point", "grid_steps",
                    "top_percentile", "bottom_percentile", "mad_factor", "min_wheel_step_px")},
                "classes": classes}
        roi_path = os.path.join(self.dir, ROI_FILE)
        self.previews = write_previews(self.dir, data, learn["frames"], self.cfg.preview_classes_per_image)
        if os.path.exists(roi_path):                    # keep the previous one; roi.json never missing
            with open(roi_path, "rb") as src, open(os.path.join(self.dir, PREV_FILE + ".tmp"), "wb") as dst:
                dst.write(src.read())
            os.replace(os.path.join(self.dir, PREV_FILE + ".tmp"), os.path.join(self.dir, PREV_FILE))
        atomic_write_json(roi_path, data)
        learned = sorted(c for c, e in classes.items() if e.get("learned"))
        not_learned = sorted(c for c, e in classes.items() if not e.get("learned"))
        self.log.info(f"auto-ROI: learned from {learn['used']} moving frames of {learn['seen']}: "
                      f"{learned}; not learned: {not_learned}; wrote {roi_path}")
        self._end_learning("done", "", learned=learned, not_learned=not_learned)
        self.counts.clear()
        self.recent.clear()
        self.drifting.clear()
        self._load()

    def _end_learning(self, result, reason, **extra):
        self.learning, self.learn = None, None
        try:
            if os.path.exists(self.request):
                os.remove(self.request)
        except OSError as e:
            self.log.error(f"auto-ROI: could not delete {self.request}: {e}")
        self._event("roi_learned" if result == "done" else "roi_learn_failed",
                    dict(extra, result=result, reason=reason))
        self.write_status()

    # ---- observe / filter -------------------------------------------------------
    def _check(self, by_class, image, frame_path, moving):
        self.last_outside = []                   # F60: [(class, detection)] of this frame, for the overlay
        if self.cfg.mode == "off" or self.paths is None:
            return by_class
        if (image.shape[1], image.shape[0]) != self.paths.frame_size:
            if not self.size_mismatch_logged:
                self.log.error(f"auto-ROI: frame size {image.shape[1]}x{image.shape[0]} differs from the "
                               f"learned {self.paths.frame_size}; ROI ignored until relearned")
                self.size_mismatch_logged = True
            return by_class
        filtered = {}
        for cls, dets in by_class.items():
            if not self.paths.learned(cls):
                filtered[cls] = dets
                continue
            keep = []
            for det in dets:
                distance = self.paths.distance_outside(cls, box_of(det))
                outside = distance > 0
                self.counts[cls]["outside" if outside else "inside"] += 1
                # F55: drift counts only moving detections (learning's rule): a moving frame and
                # not a stationary repeat. Stationary ones are still counted and listed above/below.
                if moving and not self.stationary.stationary(cls, box_of(det)):
                    self._drift(cls, outside)
                if outside:
                    self.last_outside.append((cls, det))
                    self._record_outside(cls, det, distance, image, frame_path)
                if not outside or self.cfg.mode != "filter":
                    keep.append(det)
            filtered[cls] = keep
            if moving:
                self.stationary.add_frame(cls, [box_of(d) for d in dets])
        return filtered

    def _drift(self, cls, outside):
        window = self.recent[cls]
        window.append(outside)
        if len(window) < window.maxlen:
            return
        ratio = sum(window) / len(window)
        if ratio > self.cfg.drift_ratio and cls not in self.drifting:
            self.drifting.add(cls)
            self.log.warning(f"auto-ROI drift: {cls} {ratio:.0%} of the last {len(window)} detections outside "
                             f"its path (limit {self.cfg.drift_ratio:.0%}); reported only")
            self._event("roi_drift", {"side": self.s.side, "class": cls, "ratio": round(ratio, 3),
                                      "window": len(window)})
        elif ratio <= self.cfg.drift_ratio and cls in self.drifting:
            self.drifting.discard(cls)
            self.log.info(f"auto-ROI: {cls} back inside its path ({ratio:.0%} outside)")

    def _record_outside(self, cls, det, distance, image, frame_path):
        minute = datetime.datetime.now().strftime("%Y%m%d%H%M")
        if minute != self.minute:
            self.minute, self.written_this_minute = minute, 0
        if self.written_this_minute >= self.cfg.outside_max_per_minute:
            self.suppressed += 1                         # still counted, just not listed
            return
        self.written_this_minute += 1
        xmin, ymin, xmax, ymax = (int(v) for v in box_of(det))
        stamp = datetime.datetime.now().strftime("%Y_%m_%d_%H_%M_%S_%f")
        crop_name = f"{stamp}_{cls}.jpg"
        pad = self.cfg.outside_crop_pad_px
        h, w = image.shape[:2]
        crop = image[max(0, ymin - pad):min(h, ymax + pad), max(0, xmin - pad):min(w, xmax + pad)]
        if crop.size:
            cv2.imwrite(os.path.join(self.dir, OUTSIDE_DIR, crop_name), crop)
        else:
            crop_name = None
        line = {"time": now_iso(), "frame": os.path.basename(frame_path), "class": cls,
                "box": [xmin, ymin, xmax, ymax], "score": round(float(det[0]), 3),
                "distance_px": round(distance, 1), "mode": self.cfg.mode, "crop": crop_name and f"{OUTSIDE_DIR}/{crop_name}"}
        with open(os.path.join(self.dir, OUTSIDE_FILE), "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
        self.outside_lines += 1
        if self.outside_lines > self.cfg.outside_keep * TRIM_SLACK:
            self._trim_outside()

    def _count_outside_lines(self):
        try:
            with open(os.path.join(self.dir, OUTSIDE_FILE), encoding="utf-8") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def _trim_outside(self):
        path = os.path.join(self.dir, OUTSIDE_FILE)
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        keep = lines[-self.cfg.outside_keep:]
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp, path)
        self.outside_lines = len(keep)
        kept_crops = set()
        for line in keep:
            try:
                crop = json.loads(line).get("crop")
                if crop:
                    kept_crops.add(os.path.basename(crop))
            except ValueError:
                pass
        crop_dir = os.path.join(self.dir, OUTSIDE_DIR)
        for name in os.listdir(crop_dir):
            if name not in kept_crops:
                os.remove(os.path.join(crop_dir, name))

    # ---- status for the UI, events ------------------------------------------------
    def write_status(self):
        try:
            status = {
                "updated": now_iso(), "side": self.s.side, "mode": self.cfg.mode,
                "roi": None if self.paths is None else {
                    "learned_at": self.paths.data["learned_from"].get("time"),
                    "learned_classes": sorted(self.paths.paths),
                    "not_learned": sorted(c for c, e in self.paths.data["classes"].items() if not e.get("learned")),
                "previews": self.previews},
                "learning": {"state": self.learning or "idle",
                             "frames_seen": self.learn["seen"] if self.learn else 0,
                             "frames_used": self.learn["used"] if self.learn else 0},
                "classes": {cls: dict(c, recent_outside_ratio=(round(sum(self.recent[cls]) / len(self.recent[cls]), 3)
                                                               if self.recent.get(cls) else None),
                                      drift=cls in self.drifting)
                            for cls, c in sorted(self.counts.items())},
                "outside_list": {"file": OUTSIDE_FILE, "entries": self.outside_lines,
                                 "not_listed_rate_limit": self.suppressed},
            }
            atomic_write_json(os.path.join(self.dir, STATUS_FILE), status)
        except Exception:
            self.log.exception("auto-ROI: could not write the status file")

    def _event(self, name, payload):
        if self.events is None:
            return
        try:
            self.events(name, dict(payload, side=self.s.side))
        except Exception:
            self.log.debug(f"auto-ROI: event {name} not published")
