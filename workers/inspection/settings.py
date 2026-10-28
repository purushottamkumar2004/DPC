"""Settings of one inspection side, read from its rendered JSON (CONTRACTS 13, D-062).

The Supervisor passes the file path in env DPC_WORKER_CONFIG. Every folder and every tuning
value comes from that file; nothing is hardcoded per site (D-063, D-072, D-073 D5). The model
files are not in the JSON: they come from the model package `current` points to (model.py).

A missing or wrong key stops the program with a message naming the key: inspection must not
run half-configured (a wrong window or threshold silently changes which defects are found).
"""

import datetime
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

CONFIG_ENV = "DPC_WORKER_CONFIG"
SIDES = ("LEFT", "RIGHT")
MOTIONS = ("rtl", "ltr")


class SettingsError(ValueError):
    """The rendered config is missing a key or holds a wrong value."""


@dataclass(frozen=True)
class Window:
    """A pixel window an object must lie in: xmin < obj.xmin, obj.xmax < xmax, same for y."""
    xmin: int
    xmax: int
    ymin: int
    ymax: int


@dataclass(frozen=True)
class ClipConfirm:
    frames: int          # candidate frames kept (5)
    min_pairs: int       # consecutive pairs that must agree (3)
    max_gap_ms: float    # pair time gap limit
    max_dist_px: float   # pair centroid distance limit


@dataclass(frozen=True)
class Skidding:
    window_frames: int
    required_ratio: float
    time_gap_reset_ms: float
    offset_px: float
    min_vertical_px: float
    vertical_factor: float
    same_wheel_scale: float
    min_magnet_travel_px: float
    wheel_xmin: float
    wheel_xmax: float
    magnet_check_min_wheel_w: float


@dataclass(frozen=True)
class WheelMissing:
    min_frames: int
    belt_stop_frames: int
    bbox: Window


@dataclass(frozen=True)
class Band:
    """x range a wheel centre must lie in: min < cx < max."""
    min: float
    max: float


@dataclass(frozen=True)
class Tracker:
    new_wheel_jump_px: float
    new_plate_jump_px: float
    high_dusty_suppress_px: float
    clip_confirm: ClipConfirm
    skidding: Skidding
    wheel_missing: WheelMissing
    best_image_band_x: Band       # best image per wheel (D4, F25)


@dataclass(frozen=True)
class Paths:
    root: str
    logs: str
    inf: str                  # work queue (FrameCapture links frames in)
    raw: str                  # frames moved out of INF (and skidding evidence source)
    defect_raw: str           # raw copies of defect frames
    defective: str
    repeated: str
    plate: str
    magnet: str
    bolt_data: str
    wheel_status_images: str

    def output_dirs(self) -> Tuple[str, ...]:
        """Folders this program writes into; created at start when missing (D-075)."""
        return (self.logs, self.raw, self.defect_raw, self.defective, self.repeated, self.plate,
                self.magnet, self.bolt_data, self.wheel_status_images, os.path.join(self.root, "run"))


@dataclass(frozen=True)
class Db:
    host: str
    port: int
    name: str
    user: str
    password: str


@dataclass(frozen=True)
class Mqtt:
    host: str
    port: int
    user: str
    password: str


@dataclass(frozen=True)
class Settings:
    config_path: str
    config_hash: str               # _generated.sha256, shortened, for the heartbeat
    worker_id: str
    side: str                      # LEFT | RIGHT
    defect_side: str               # Left | Right (defects_table.defect_side)
    camera_id: str
    writes_live_wheel_counter: bool
    paths: Paths
    log_backup_count: int
    model_name: str
    models_dir: str
    threshold: float
    total_wheel_count: int
    expected_cycle_total: int
    daily_report_time: datetime.time
    bucket_clip_w: int
    half_bucket_clip_w: int
    motion: str                    # rtl (Left today) | ltr (Right today)
    bucket_clip_window: Window
    half_bucket_clip_window: Window
    tracker: Tracker
    db: Db
    mqtt: Optional[Mqtt]           # None: no mqtt block (rendered before F25), heartbeat off

    def summary(self) -> str:
        """One log line of what this process will do. Never contains a secret."""
        return (f"worker={self.worker_id} side={self.side} camera={self.camera_id} motion={self.motion} "
                f"model={self.model_name} threshold={self.threshold} total_wheels={self.total_wheel_count} "
                f"expected_cycle_total={self.expected_cycle_total} live_counter={self.writes_live_wheel_counter} "
                f"daily_report={self.daily_report_time.strftime('%H:%M')} "
                f"heartbeat={'on' if self.mqtt else 'off'} config={self.config_path}")


# ----------------------------------------------------------------------
# Reading helpers: each names the full key in its error message
# ----------------------------------------------------------------------
def _get(section: Any, key: str, where: str) -> Any:
    if not isinstance(section, dict) or key not in section:
        raise SettingsError(f"missing key '{where + '.' if where else ''}{key}'")
    return section[key]


def _number(section, key, where, kind=float, low=None):
    value = _get(section, key, where)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError(f"'{where}.{key}' must be a number, got {value!r}")
    if kind is int and value != int(value):
        raise SettingsError(f"'{where}.{key}' must be a whole number, got {value!r}")
    value = kind(value)
    if low is not None and value < low:
        raise SettingsError(f"'{where}.{key}' must be at least {low}, got {value}")
    return value


def _text(section, key, where) -> str:
    value = _get(section, key, where)
    if not isinstance(value, str) or not value:
        raise SettingsError(f"'{where}.{key}' must be a non-empty text, got {value!r}")
    return value


def _window(section, key, where) -> Window:
    block = _get(section, key, where)
    name = f"{where + '.' if where else ''}{key}"
    w = Window(**{k: _number(block, k, name, int) for k in ("xmin", "xmax", "ymin", "ymax")})
    if w.xmin >= w.xmax or w.ymin >= w.ymax:
        raise SettingsError(f"'{name}' must have xmin < xmax and ymin < ymax, got {w}")
    return w


def _daily_time(text: str) -> datetime.time:
    try:
        return datetime.datetime.strptime(text, "%H:%M").time()
    except ValueError:
        raise SettingsError(f"'wheel_report.daily_time' must be HH:MM, got {text!r}") from None


def _tracker(data) -> Tracker:
    t = _get(data, "tracker", "")
    c = _get(t, "clip_confirm", "tracker")
    s = _get(t, "skidding", "tracker")
    m = _get(t, "wheel_missing", "tracker")
    w_c, w_s, w_m = "tracker.clip_confirm", "tracker.skidding", "tracker.wheel_missing"
    clip = ClipConfirm(frames=_number(c, "frames", w_c, int, 2), min_pairs=_number(c, "min_pairs", w_c, int, 1),
                       max_gap_ms=_number(c, "max_gap_ms", w_c, low=0), max_dist_px=_number(c, "max_dist_px", w_c, low=0))
    if clip.min_pairs > clip.frames - 1:
        raise SettingsError(f"'{w_c}.min_pairs' ({clip.min_pairs}) cannot exceed frames - 1 ({clip.frames - 1})")
    skid = Skidding(
        window_frames=_number(s, "window_frames", w_s, int, 2),
        required_ratio=_number(s, "required_ratio", w_s, low=0),
        time_gap_reset_ms=_number(s, "time_gap_reset_ms", w_s, low=0),
        offset_px=_number(s, "offset_px", w_s),
        min_vertical_px=_number(s, "min_vertical_px", w_s, low=0),
        vertical_factor=_number(s, "vertical_factor", w_s, low=0),
        same_wheel_scale=_number(s, "same_wheel_scale", w_s, low=0),
        min_magnet_travel_px=_number(s, "min_magnet_travel_px", w_s, low=0),
        wheel_xmin=_number(s, "wheel_xmin", w_s),
        wheel_xmax=_number(s, "wheel_xmax", w_s),
        magnet_check_min_wheel_w=_number(s, "magnet_check_min_wheel_w", w_s, low=0))
    missing = WheelMissing(min_frames=_number(m, "min_frames", w_m, int, 1),
                           belt_stop_frames=_number(m, "belt_stop_frames", w_m, int, 1),
                           bbox=_window(m, "bbox", w_m))
    if missing.min_frames > missing.belt_stop_frames:
        raise SettingsError(f"'{w_m}.min_frames' must not exceed '{w_m}.belt_stop_frames'")
    b = _get(t, "best_image_band_x", "tracker")
    band = Band(min=_number(b, "min", "tracker.best_image_band_x"), max=_number(b, "max", "tracker.best_image_band_x"))
    if band.min >= band.max:
        raise SettingsError(f"'tracker.best_image_band_x' must have min < max, got {band}")
    return Tracker(new_wheel_jump_px=_number(t, "new_wheel_jump_px", "tracker", low=0),
                   new_plate_jump_px=_number(t, "new_plate_jump_px", "tracker", low=0),
                   high_dusty_suppress_px=_number(t, "high_dusty_suppress_px", "tracker", low=0),
                   clip_confirm=clip, skidding=skid, wheel_missing=missing, best_image_band_x=band)


def _mqtt(data) -> Optional[Mqtt]:
    """The heartbeat broker (F25). user/password null = no login. Without the block (a file
    rendered before F25) the program runs without a heartbeat and says so once."""
    block = data.get("mqtt")
    if block is None:
        return None
    return Mqtt(host=_text(block, "host", "mqtt"), port=_number(block, "port", "mqtt", int, 1),
                user=str(block.get("user") or ""), password=str(block.get("password") or ""))


# ----------------------------------------------------------------------
def load_settings(path: str) -> Settings:
    """Read and check the rendered JSON. Raises SettingsError (or OSError/JSON errors)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SettingsError("the file does not hold a JSON object")

    side = _get(data, "side", "")
    if side not in SIDES:
        raise SettingsError(f"'side' must be one of {SIDES}, got {side!r}")
    motion = _get(data, "motion", "")
    if motion not in MOTIONS:
        raise SettingsError(f"'motion' must be one of {MOTIONS}, got {motion!r}")
    live = _get(data, "writes_live_wheel_counter", "")
    if not isinstance(live, bool):
        raise SettingsError(f"'writes_live_wheel_counter' must be true or false, got {live!r}")

    p = _get(data, "paths", "")
    paths = Paths(**{k: _text(p, k, "paths") for k in (
        "root", "logs", "inf", "raw", "defect_raw", "defective", "repeated", "plate",
        "magnet", "bolt_data", "wheel_status_images")})

    machine = _get(data, "machine", "")
    total = _number(machine, "total_wheel_count", "machine", int, 1)
    sizes = _get(data, "object_sizes", "")
    model = _get(data, "model", "")
    db = _get(data, "db", "")
    threshold = _number(data, "threshold", "")
    if not 0 < threshold < 1:
        raise SettingsError(f"'threshold' must be between 0 and 1, got {threshold}")

    return Settings(
        config_path=path,
        config_hash=str(data.get("_generated", {}).get("sha256", ""))[:12],
        worker_id=_text(data, "worker_id", ""),
        side=side,
        defect_side=_text(data, "defect_side", ""),
        camera_id=_text(data, "camera_id", ""),
        writes_live_wheel_counter=live,
        paths=paths,
        log_backup_count=_number(_get(data, "log", ""), "backup_count", "log", int, 0),
        model_name=_text(model, "name", "model"),
        models_dir=_text(model, "models_dir", "model"),
        threshold=threshold,
        total_wheel_count=total,
        expected_cycle_total=_number(machine, "expected_cycle_total", "machine", int, 1),
        daily_report_time=_daily_time(_text(_get(data, "wheel_report", ""), "daily_time", "wheel_report")),
        bucket_clip_w=_number(sizes, "bucket_clip_w", "object_sizes", int, 0),
        half_bucket_clip_w=_number(sizes, "half_bucket_clip_w", "object_sizes", int, 0),
        motion=motion,
        bucket_clip_window=_window(data, "bucket_clip_window", ""),
        half_bucket_clip_window=_window(data, "half_bucket_clip_window", ""),
        tracker=_tracker(data),
        db=Db(host=_text(db, "host", "db"), port=_number(db, "port", "db", int, 1), name=_text(db, "name", "db"),
              user=_text(db, "user", "db"), password=str(_get(db, "password", "db"))),
        mqtt=_mqtt(data),
    )


def settings_from_env() -> Settings:
    path = os.environ.get(CONFIG_ENV)
    if not path:
        raise SettingsError(f"env {CONFIG_ENV} is not set (the Supervisor sets it to the rendered "
                            f"inspection_<side>.json)")
    return load_settings(path)


def config_secrets(settings: Optional[Settings]) -> Tuple[str, ...]:
    """Secret values that must never appear in a log line or in last_error.json."""
    if settings is None:
        return ()
    values = [settings.db.password]
    if settings.mqtt:
        values.append(settings.mqtt.password)
    return tuple(v for v in values if v)

