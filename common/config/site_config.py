"""Load and validate the site config `site.yaml` (schema v3, grouped by program:
CONTRACTS sections 7.1 and 9, D-048, D-050, D-055). The file holds the site's secrets.

v3 layout: shared sections first (site, paths, db, mqtt, graph), then one section per
program (capture, inspection, plc, mail, cleanup, transfer). Model files, classes and the
label map are not in site.yaml: they come from the model package (common/config/model_package.py,
CONTRACTS 12). site.yaml names only WHICH model; the running version is the `current` pointer,
managed by the Supervisor (D-060). A v2 file is refused with one line per
moved key; scripts/convert_site_yaml_v3.py converts one.

Public API
    load_site_config(path=None) -> SiteConfig      (.warnings = non-fatal notes, e.g. db.user root)
    SiteConfigError            (.problems = list of "key.path: what is wrong")
    redacted_copy(path=None) -> str      the file text with every secret replaced by ***
    legacy_env_vars(config) -> dict      DPC_* env vars for legacy workers (CONTRACTS 9 env bridge)
    v2_moves(data), convert_v2_to_v3(data, model_name)   v2 -> v3 (D-055)

Command line
    python -m common.config.site_config [path]               prints OK or every problem; exit 0 or 1
    python -m common.config.site_config [path] --redacted    prints a copy that is safe to share

Why it is strict: one file sets up a whole 24x7 site, so a typo must stop the
start-up with a clear message instead of silently falling back to a default.
That is why unknown keys, duplicate keys and wrong types are all errors, and why
every problem is collected before raising (fix them all in one go).

Secrets: the file must be chmod 600 and owned by the user running the services, or
it is refused before it is read. No problem message, repr or log line shows a secret
value; secret values are shown as ***.

This module checks the file only. Whether model files exist, cameras answer or
the DB login works is the job of the preflight (`dpc-check`, STATUS item 5).
"""

import argparse
import difflib
import ipaddress
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import yaml

SCHEMA_VERSION = 3
CONFIG_ENV_VAR = "DPC_SITE_CONFIG"
DEFAULT_CONFIG_PATH = "~/INSIGHTZZ/config/site.yaml"

CAMERA_SIDES = {"CAM1": "LEFT", "CAM2": "RIGHT"}      # CONTRACTS 4.1: CAM1 -> LEFT, CAM2 -> RIGHT
CAMERA_DRIVERS = ("basler", "flir")
ROTATIONS = (0, 90, 180, 270)
FRAME_SUBDIRS = ("live", "_tmp", "INF", "RAW_IMAGES", "UI")
IMAGE_KINDS = ("DEFECTIVE", "REPEATED", "RAW", "PLATE", "MAGNET", "TEMP")   # data/images/<KIND>/<SIDE> (4.1, D-067, D-072)
OBJECT_SIZE_KEYS = ("wheel_w", "wheel_h", "bucket_clip_w", "chain_w", "half_bucket_clip_w")
DISK_PERCENT_ORDER = ("delete", "raw_delete", "mail", "inference_delete")   # must increase in this order
AUTO_ROI_MODES = ("off", "observe", "filter")          # D-054
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
SITE_ID_TOKEN = "<site.id>"                            # replaced in transfer.upload.remote_folder

# Per-side defaults = each side's value in today's code (INSPECTION.md, D-072, D-073 D5): nothing
# changes until a value is set in site.yaml. Left = LeftAlgorithm.py (L:), Right = RightAlgorithm.py (R:).
SIDE_DEFAULTS = {
    "LEFT": {
        "motion": "rtl",                                                     # L:1776-1810
        "bucket_clip_window": {"xmin": 50, "xmax": 1990, "ymin": 750, "ymax": 1085},       # L:604-607
        "half_bucket_clip_window": {"xmin": 100, "xmax": 1850, "ymin": 470, "ymax": 750},  # L:772-775
        "tracker": {
            "new_wheel_jump_px": 100,                                        # L:1775
            "new_plate_jump_px": 150,                                        # L:2208
            "high_dusty_suppress_px": 50,                                    # L:323
            "clip_confirm": {"frames": 5, "min_pairs": 3, "max_gap_ms": 300, "max_dist_px": 200},  # L:657-676
            "skidding": {"window_frames": 3, "required_ratio": 0.7, "time_gap_reset_ms": 500,      # L:1256-1263
                         "offset_px": 80, "min_vertical_px": 8, "vertical_factor": 0.5,
                         "same_wheel_scale": 0.5, "min_magnet_travel_px": 9,                    # L:1516
                         "wheel_xmin": 20, "wheel_xmax": 2025,                                   # L:1502
                         "magnet_check_min_wheel_w": 600},                                       # L:1282
            "wheel_missing": {"min_frames": 9, "belt_stop_frames": 20,                            # L:1927-1929
                              "bbox": {"xmin": 668, "xmax": 1474, "ymin": 508, "ymax": 1278}},
            "best_image_band_x": {"min": 400, "max": 850},   # Left had no best image; Right's band (D-076)
        },
    },
    "RIGHT": {
        "motion": "ltr",                                                     # R:1906-1935
        "bucket_clip_window": {"xmin": 10, "xmax": 1270, "ymin": 550, "ymax": 780},        # R:712-715
        "half_bucket_clip_window": {"xmin": 100, "xmax": 1850, "ymin": 480, "ymax": 730},  # R:894-897
        "tracker": {
            "new_wheel_jump_px": 100,                                        # R:1906
            "new_plate_jump_px": 150,                                        # R:2598
            "high_dusty_suppress_px": 50,                                    # R:337
            "clip_confirm": {"frames": 5, "min_pairs": 3, "max_gap_ms": 500, "max_dist_px": 500},  # R:784
            "skidding": {"window_frames": 3, "required_ratio": 0.7, "time_gap_reset_ms": 1000,     # R:1325-1332
                         "offset_px": 80, "min_vertical_px": 8, "vertical_factor": 0.5,
                         "same_wheel_scale": 0.5, "min_magnet_travel_px": 9,                    # R:1585
                         "wheel_xmin": 20, "wheel_xmax": 2025,                                   # R:1571
                         "magnet_check_min_wheel_w": 600},                                       # R:1351
            "wheel_missing": {"min_frames": 9, "belt_stop_frames": 20,                            # R:2317-2319
                              "bbox": {"xmin": 668, "xmax": 1474, "ymin": 508, "ymax": 1278}},
            "best_image_band_x": {"min": 400, "max": 850},   # R:2826-2835 (D-076)
        },
    },
}
MOTIONS = ("rtl", "ltr")
_HH_MM_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
_SITE_ID_RE = re.compile(r"^[a-z0-9_-]+$")
# DB and table names end up inside SQL text, so only plain identifiers are allowed.
_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Model names (and versions, in model_package) are folder names: no slashes, no leading dot.
_MODEL_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SiteConfigError(Exception):
    """The config (or secrets file) is invalid. `problems` lists every problem found."""

    def __init__(self, source: str, problems: List[str]):
        self.source = source
        self.problems = list(problems)
        lines = [f"{source}: {len(self.problems)} problem(s)"] + [f"  - {p}" for p in self.problems]
        super().__init__("\n".join(lines))


# ======================================================================
# Typed result
# ======================================================================

@dataclass(frozen=True)
class SiteInfo:
    id: str
    name: str


@dataclass(frozen=True)
class Paths:
    """The deployed layout (CONTRACTS 4.1). Only `root` is in the file; the rest is derived."""
    root: str

    @property
    def code(self) -> str:
        return os.path.join(self.root, "code")

    @property
    def config(self) -> str:
        return os.path.join(self.root, "config")

    @property
    def rendered(self) -> str:
        return os.path.join(self.config, "rendered")

    @property
    def logs(self) -> str:
        return os.path.join(self.root, "logs")

    @property
    def run(self) -> str:
        return os.path.join(self.root, "run")

    @property
    def data(self) -> str:
        return os.path.join(self.root, "data")

    def resolve(self, path: str) -> str:
        """Expand `~`; a relative path is relative to `root`."""
        path = os.path.expanduser(path)
        return os.path.normpath(path if os.path.isabs(path) else os.path.join(self.root, path))

    def frame_dir(self, camera_id: str, sub: str) -> str:
        """e.g. frame_dir("CAM1", "INF") -> <root>/data/frames/CAM1/INF"""
        if camera_id not in CAMERA_SIDES or sub not in FRAME_SUBDIRS:
            raise ValueError(f"unknown frame folder {camera_id}/{sub}")
        return os.path.join(self.data, "frames", camera_id, sub)

    def image_dir(self, kind: str, side: str) -> str:
        """e.g. image_dir("DEFECTIVE", "LEFT") -> <root>/data/images/DEFECTIVE/LEFT"""
        if kind not in IMAGE_KINDS or side not in CAMERA_SIDES.values():
            raise ValueError(f"unknown image folder {kind}/{side}")
        return os.path.join(self.data, "images", kind, side)


@dataclass(frozen=True)
class LogSettings:
    """Per-program log settings; files always go to <root>/logs/ (D-055)."""
    backup_count: int


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    name: str
    health_table: str
    user: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int
    user: Optional[str] = field(repr=False)        # None when the broker has no login
    password: Optional[str] = field(repr=False)


@dataclass(frozen=True)
class GraphSettings:
    """Microsoft Graph login, shared by mail and transfer (D-055)."""
    tenant_id: str = field(repr=False)
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)


@dataclass(frozen=True)
class FlirSettings:
    interface: str
    throughput_bps: int


@dataclass(frozen=True)
class Camera:
    id: str              # CAM1 | CAM2
    side: str            # LEFT | RIGHT
    driver: str          # basler | flir
    serial: str
    fps: float
    exposure_us: int
    rotate: int          # 0 | 90 | 180 | 270
    flir: Optional[FlirSettings]
    ip: Optional[str] = None     # GigE address, optional (D-064): UI status and dpc-check ping

    @property
    def log_file_name(self) -> str:
        return f"FrameCapture_{self.id}.log"

    @property
    def defect_side(self) -> str:
        """Legacy DEFECT_SIDE value: Left | Right."""
        return self.side.capitalize()


@dataclass(frozen=True)
class CaptureConfig:
    log: LogSettings
    cameras: Tuple[Camera, ...]
    health_interval_s: int = 600     # health row rhythm (P4d)


@dataclass(frozen=True)
class ObjectSizes:
    wheel_w: int
    wheel_h: int
    bucket_clip_w: int
    chain_w: int
    half_bucket_clip_w: int


@dataclass(frozen=True)
class Window:
    """A pixel window in this camera's image: a detection must lie inside it."""
    xmin: int
    xmax: int
    ymin: int
    ymax: int


@dataclass(frozen=True)
class ClipConfirm:
    frames: int          # candidates kept
    min_pairs: int       # consecutive pairs that must pass
    max_gap_ms: int
    max_dist_px: int


@dataclass(frozen=True)
class Skidding:
    window_frames: int
    required_ratio: float
    time_gap_reset_ms: int
    offset_px: int
    min_vertical_px: int
    vertical_factor: float
    same_wheel_scale: float
    min_magnet_travel_px: int        # below this over the window = Wheel_Skidding
    wheel_xmin: int
    wheel_xmax: int
    magnet_check_min_wheel_w: int    # wider wheel without magnet -> MAGNET diagnostics


@dataclass(frozen=True)
class WheelMissingSettings:
    min_frames: int                  # frames without a wheel that make a WheelMissing
    belt_stop_frames: int            # more than this = belt stopped, cancel
    bbox: Window                     # fixed box drawn on the defect image


@dataclass(frozen=True)
class BandX:
    """x range of the wheel centre for the best image per wheel (D-073 D4, D-076)."""
    min: int
    max: int


@dataclass(frozen=True)
class Tracker:
    new_wheel_jump_px: int
    new_plate_jump_px: int
    high_dusty_suppress_px: int
    clip_confirm: ClipConfirm
    skidding: Skidding
    wheel_missing: WheelMissingSettings
    best_image_band_x: BandX


@dataclass(frozen=True)
class SideSettings:
    object_sizes: ObjectSizes
    motion: str                      # rtl | ltr: direction the belt moves in this camera's image
    bucket_clip_window: Window
    half_bucket_clip_window: Window
    tracker: Tracker


@dataclass(frozen=True)
class MachineConfig:
    total_wheel_count: int
    expected_cycle_total: int        # default = total_wheel_count (D-073 D8)


@dataclass(frozen=True)
class WheelReport:
    daily_time: str                  # "HH:MM": daily best-image copy per wheel (D-073 D4)


@dataclass(frozen=True)
class ModelRef:
    """Which model inspection uses. Only the name: the running version is the `current`
    pointer in `root`, managed by the Supervisor (D-060). Files, classes and the label map are
    in the package, read with common.config.model_package.load_model_package()."""
    name: str
    root: str            # <root>/data/models/<name>: the versions, `current` and `pinned`


@dataclass(frozen=True)
class AutoRoi:
    mode: str            # off | observe | filter (D-054)
    learn_images: int


@dataclass(frozen=True)
class InspectionConfig:
    log: LogSettings
    model: ModelRef
    threshold: float
    machine: MachineConfig
    sides: Dict[str, SideSettings]      # LEFT / RIGHT, one per configured camera side
    auto_roi: AutoRoi
    live_wheel_counter_side: str        # the one side that writes wheel_counter_table (D-073 D3)
    wheel_report: WheelReport


@dataclass(frozen=True)
class PlcConfig:
    type: str
    ip: str
    port: int
    timeout_s: float
    light_register: int
    cleaning_register: int
    conveyor_status_register: int
    cleaning_pulse_seconds: float
    legacy_db_numbers: Optional[Dict[str, int]]   # {"write": n, "read": n}, legacy XML only
    light_on_value: int          # value that switches the light ON: 1, or 0 where inverted (D-064)
    log: LogSettings


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    password: str = field(repr=False)


@dataclass(frozen=True)
class MailFlood:
    """Flood breaker of the defect-alert mail (D-083): more than max_per_window alert mails in
    window_hours -> the next ones go only to internal_to."""
    max_per_window: int
    window_hours: float
    internal_to: Tuple[str, ...]


@dataclass(frozen=True)
class MailConfig:
    sender: str          # graph | smtp (graph uses the shared top-level `graph` login)
    project_name: str
    report_dir: str      # absolute
    recipients: Tuple[str, ...]
    cc: Tuple[str, ...]
    sender_address: str
    disk_report_to: Tuple[str, ...]
    smtp: Optional[SmtpSettings]     # set when sender = smtp
    log: LogSettings
    report_url: Optional[str] = None             # link line in the alert mail; None = left out (F36)
    max_alert_age_hours: Optional[float] = 24.0  # older pending rows are never mailed; None = off (D-083)
    flood: Optional[MailFlood] = None            # None = flood breaker off (D-083)


@dataclass(frozen=True)
class KeepLimit:
    days: int
    items: int


@dataclass(frozen=True)
class CleanupConfig:
    raw_image_hours: int
    interval_seconds: int
    repeated: KeepLimit
    plate_days: int
    defective: KeepLimit
    disk_percent: Dict[str, int]
    log: LogSettings
    disk_report_time: str = "09:00"   # "HH:MM" of the daily disk mail (F29; was DAILY_REPORT_HOUR 9)


@dataclass(frozen=True)
class TransferUpload:
    weekday: str         # monday .. sunday
    chunk_mb: int
    remote_folder: str   # OneDrive folder, "<site.id>" already replaced


@dataclass(frozen=True)
class TransferConfig:
    upload: Optional[TransferUpload]   # None = no weekly upload (model download only)
    models_folder: str                 # OneDrive folder with <name>/<version>/ (D-060)
    log: LogSettings


@dataclass(frozen=True)
class SiteConfig:
    schema_version: int
    site: SiteInfo
    paths: Paths
    db: DbConfig
    mqtt: MqttConfig
    graph: Optional[GraphSettings]
    capture: CaptureConfig
    inspection: InspectionConfig
    plc: PlcConfig
    mail: MailConfig
    cleanup: CleanupConfig
    transfer: Optional[TransferConfig]   # None = no transfer service on this site
    source_path: str
    warnings: Tuple[str, ...] = ()   # the file is valid, but these deserve a look (printed by the CLI)

    @property
    def cameras(self) -> Tuple[Camera, ...]:
        return self.capture.cameras

    def camera(self, camera_id: str) -> Camera:
        for cam in self.cameras:
            if cam.id == camera_id:
                return cam
        raise KeyError(f"camera {camera_id} is not configured")

    def camera_for_side(self, side: str) -> Camera:
        for cam in self.cameras:
            if cam.side == side:
                return cam
        raise KeyError(f"no camera configured for side {side}")


# ======================================================================
# Reading helpers: every getter records problems instead of raising,
# so one run reports everything that is wrong.
# ======================================================================

_MISSING = object()


class _Section:
    """One YAML mapping. Remembers which keys were read; `finish()` reports the rest as unknown."""

    def __init__(self, data: Any, key: str, problems: List[str], report_missing: bool = True):
        self.key = key                # path of this mapping, e.g. "cameras[0]" ("" for the top level)
        self.problems = problems
        # False when this whole section is missing and that was already reported once:
        # then "site: missing" is enough, without "site.id: missing" and "site.name: missing".
        self.report_missing = report_missing
        if data is not None and not isinstance(data, dict):
            self.problem("", "must be a mapping (key: value)")
            data = None
        self.data = data or {}
        self.used = set()

    def path(self, name: str) -> str:
        return f"{self.key}.{name}" if self.key else str(name)

    def problem(self, name: str, message: str):
        where = self.path(name) if name else (self.key or "<top level>")
        self.problems.append(f"{where}: {message}")

    def present(self, name: str) -> bool:
        return self.data.get(name) is not None

    def _raw(self, name: str, required: bool) -> Any:
        self.used.add(name)
        value = self.data.get(name)
        if value is None:
            if required and self.report_missing:
                self.problem(name, "missing")
            return _MISSING
        return value

    # ---- typed getters (return the default, or None, when the value is missing or wrong) ----

    def string(self, name, required=True, default=None, choices=None, pattern=None, pattern_hint="",
               secret=False, allow_empty=False):
        """`secret=True`: the value never appears in a problem message.
        `allow_empty=True`: "" counts as not set (returns the default)."""
        value = self._raw(name, required)
        if value is _MISSING:
            return default
        shown = "***" if secret else repr(value)
        if not isinstance(value, str):
            hint = " (quote it)" if isinstance(value, (int, float)) else ""
            self.problem(name, f"must be a string{hint}, got {shown}")
            return None
        if not value.strip():
            if allow_empty:
                return default
            self.problem(name, "must not be empty")
            return None
        if choices is not None and value not in choices:
            self.problem(name, f"must be one of {', '.join(map(str, choices))}, got {value!r}")
            return None
        if pattern is not None and not pattern.match(value):
            self.problem(name, f"{pattern_hint}, got {value!r}")
            return None
        return value

    def choice(self, name, choices, required=False, default=None):
        """A string from `choices`. YAML 1.1 reads an unquoted off/on as false/true, so a
        boolean is taken as "off"/"on" when that word is one of the choices."""
        self.used.add(name)
        value = self.data.get(name)
        if isinstance(value, bool) and ("on" if value else "off") in choices:
            return "on" if value else "off"
        self.used.discard(name)
        return self.string(name, required=required, default=default, choices=choices)

    def integer(self, name, required=True, default=None, low=None, high=None, choices=None):
        value = self._raw(name, required)
        if value is _MISSING:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            self.problem(name, f"must be an integer, got {value!r}")
            return None
        return self._check_range(name, value, low, high, choices)

    def number(self, name, required=True, default=None, low=None, high=None, low_exclusive=False,
               high_exclusive=False):
        value = self._raw(name, required)
        if value is _MISSING:
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.problem(name, f"must be a number, got {value!r}")
            return None
        if low is not None and (value <= low if low_exclusive else value < low):
            self.problem(name, f"must be {'>' if low_exclusive else '>='} {low}, got {value}")
            return None
        if high is not None and (value >= high if high_exclusive else value > high):
            self.problem(name, f"must be {'<' if high_exclusive else '<='} {high}, got {value}")
            return None
        return float(value)

    def _check_range(self, name, value, low, high, choices):
        if choices is not None and value not in choices:
            self.problem(name, f"must be one of {', '.join(map(str, choices))}, got {value}")
            return None
        if (low is not None and value < low) or (high is not None and value > high):
            self.problem(name, f"must be between {low} and {high}, got {value}")
            return None
        return value

    def string_list(self, name, required=True, default=None, item_pattern=None, item_hint=""):
        value = self._raw(name, required)
        if value is _MISSING:
            return default
        if not isinstance(value, list):
            self.problem(name, f"must be a list, got {value!r}")
            return None
        result, ok = [], True
        for i, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                self.problem(f"{name}[{i}]", f"must be a non-empty string, got {item!r}")
                ok = False
            elif item_pattern is not None and not item_pattern.match(item):
                self.problem(f"{name}[{i}]", f"{item_hint}, got {item!r}")
                ok = False
            else:
                result.append(item)
        return tuple(result) if ok else None

    def section(self, name, required=True) -> "_Section":
        """A nested mapping. When missing, an empty section is returned so its defaults still apply."""
        value = self._raw(name, required)
        if value is _MISSING:
            return _Section(None, self.path(name), self.problems, report_missing=False)
        return _Section(value, self.path(name), self.problems, self.report_missing)

    def finish(self):
        """Report keys nobody asked for: almost always a typo."""
        for key in self.data:
            if key in self.used:
                continue
            close = difflib.get_close_matches(str(key), [str(k) for k in self.used], n=1)
            hint = f" (did you mean '{close[0]}'?)" if close else ""
            self.problem(str(key), f"unknown key{hint}")


# ======================================================================
# YAML parsing: safe_load, but a duplicate key is an error (a later
# duplicate would silently win otherwise).
# ======================================================================

class _StrictLoader(yaml.SafeLoader):
    pass


def _construct_mapping_no_duplicates(loader, node, deep=False):
    seen = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
            seen.add(key)
        except TypeError:          # unhashable key: let PyYAML report it
            continue
        if duplicate:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark)
    return loader.construct_mapping(node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                              _construct_mapping_no_duplicates)


def default_config_path() -> str:
    """$DPC_SITE_CONFIG, else ~/INSIGHTZZ/config/site.yaml (CONTRACTS 7)."""
    return os.path.expanduser(os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH)


def _check_file_is_private(path: str) -> List[str]:
    """The file holds secrets: it must belong to the service user and be readable by nobody
    else (chmod 600)."""
    info = os.stat(path)
    problems = []
    if not stat.S_ISREG(info.st_mode):
        problems.append("must be a regular file")
    if info.st_mode & 0o077:
        problems.append(f"permissions are {stat.S_IMODE(info.st_mode):o}; it holds secrets, run: chmod 600 {path}")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        problems.append("is owned by another user; it must belong to the user running the services")
    return problems


def _yaml_error_message(exc: yaml.YAMLError) -> str:
    """Only the problem and its position: PyYAML's full message can quote a line of the
    file, and that line could hold a secret."""
    mark = getattr(exc, "problem_mark", None)
    where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark is not None else ""
    return f"not valid YAML: {getattr(exc, 'problem', None) or 'cannot be parsed'}{where}"


def _read_text(path: str) -> str:
    if not os.path.isfile(path):
        raise SiteConfigError(path, ["file not found"])
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _read_yaml(path: str) -> Any:
    if not os.path.isfile(path):
        raise SiteConfigError(path, ["file not found"])
    problems = _check_file_is_private(path)
    if problems:      # refuse before reading: the file holds secrets
        raise SiteConfigError(path, problems)
    try:
        return yaml.load(_read_text(path), Loader=_StrictLoader)
    except yaml.YAMLError as exc:
        raise SiteConfigError(path, [_yaml_error_message(exc)]) from None


# ======================================================================
# One reader per top-level section. Each returns the typed object
# (possibly holding None values when there were problems).
# ======================================================================

def _read_site(top: _Section) -> SiteInfo:
    s = top.section("site")
    info = SiteInfo(
        id=s.string("id", pattern=_SITE_ID_RE, pattern_hint="use lowercase letters, digits, - or _"),
        name=s.string("name"),
    )
    s.finish()
    return info


def _read_paths(top: _Section) -> Paths:
    s = top.section("paths")
    root = s.string("root")
    s.finish()
    if root is None:
        return Paths(root=None)
    root = os.path.normpath(os.path.expanduser(root))
    if not os.path.isabs(root):
        s.problem("root", f"must be an absolute path or start with ~, got {root!r}")
        return Paths(root=None)
    return Paths(root=root)


def _read_log(parent: _Section) -> LogSettings:
    """`<program>.log`, optional. Files always go to <root>/logs/."""
    s = parent.section("log", required=False)
    log = LogSettings(backup_count=s.integer("backup_count", required=False, default=5, low=1, high=365))
    s.finish()
    return log


def _read_db(top: _Section) -> DbConfig:
    s = top.section("db")
    hint = "use only letters, digits and _"
    cfg = DbConfig(
        host=s.string("host", required=False, default="localhost"),
        port=s.integer("port", required=False, default=3306, low=1, high=65535),
        name=s.string("name", pattern=_SQL_IDENTIFIER_RE, pattern_hint=hint),
        health_table=s.string("health_table", required=False, default="health_status",
                              pattern=_SQL_IDENTIFIER_RE, pattern_hint=hint),
        user=s.string("user", secret=True),
        password=s.string("password", secret=True),
    )
    s.finish()
    return cfg


def _read_mqtt(top: _Section) -> MqttConfig:
    s = top.section("mqtt", required=False)
    cfg = MqttConfig(
        host=s.string("host", required=False, default="localhost"),
        port=s.integer("port", required=False, default=1883, low=1, high=65535),
        # "" means "no login", as in the example file
        user=s.string("user", required=False, secret=True, allow_empty=True),
        password=s.string("password", required=False, secret=True, allow_empty=True),
    )
    if (cfg.user is None) != (cfg.password is None):
        s.problem("user", "give both user and password, or neither")
    s.finish()
    return cfg


def _read_graph(top: _Section, needed_by: List[str]) -> Optional[GraphSettings]:
    """Shared Microsoft Graph login (D-055). Required when a program on this site uses Graph."""
    if not top.present("graph"):
        top.used.add("graph")
        if needed_by:
            top.problem("graph", f"missing (needed by {', '.join(needed_by)})")
        return None
    g = top.section("graph")
    graph = GraphSettings(tenant_id=g.string("tenant_id", secret=True),
                          client_id=g.string("client_id", secret=True),
                          client_secret=g.string("client_secret", secret=True))
    g.finish()
    return graph


# ---- capture ----

def _ipv4(s: _Section, name: str, required: bool = True) -> Optional[str]:
    value = s.string(name, required=required)
    if value is None:
        return None
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        s.problem(name, f"must be an IPv4 address, got {value!r}")
        return None
    return value


def _read_camera(item: Any, key: str, problems: List[str]) -> Camera:
    s = _Section(item, key, problems)
    driver = s.string("driver", choices=CAMERA_DRIVERS)
    flir = None
    if s.present("flir") or driver == "flir":
        # Required with driver flir, not allowed otherwise.
        f = s.section("flir", required=(driver == "flir"))
        flir = FlirSettings(interface=f.string("interface"),
                            throughput_bps=f.integer("throughput_bps", low=1))
        f.finish()
        if driver != "flir" and driver is not None:
            s.problem("flir", "only allowed when driver is flir")
    cam = Camera(
        id=s.string("id", choices=tuple(CAMERA_SIDES)),
        side=s.string("side", choices=tuple(CAMERA_SIDES.values())),
        driver=driver,
        serial=s.string("serial"),
        fps=s.number("fps", low=0, low_exclusive=True, high=30),
        exposure_us=s.integer("exposure_us", low=10, high=1000000),
        rotate=s.integer("rotate", choices=ROTATIONS),
        flir=flir,
        ip=_ipv4(s, "ip", required=False),
    )
    s.finish()
    if cam.id and cam.side and CAMERA_SIDES[cam.id] != cam.side:
        s.problem("side", f"{cam.id} must be {CAMERA_SIDES[cam.id]} (CAM1 = LEFT, CAM2 = RIGHT), got {cam.side}")
    return cam


def _read_cameras(s: _Section) -> Tuple[Camera, ...]:
    items = s._raw("cameras", required=True)
    if items is _MISSING:
        return ()
    if not isinstance(items, list) or not 1 <= len(items) <= 2:
        s.problem("cameras", "must be a list with 1 or 2 cameras")
        return ()
    cameras = tuple(_read_camera(item, f"{s.path('cameras')}[{i}]", s.problems) for i, item in enumerate(items))
    for attr in ("id", "side", "serial", "ip"):
        values = [getattr(c, attr) for c in cameras if getattr(c, attr) is not None]
        for value in sorted({v for v in values if values.count(v) > 1}):
            s.problem("cameras", f"{attr} {value!r} is used by more than one camera")
    return cameras


def _read_capture(top: _Section) -> CaptureConfig:
    s = top.section("capture")
    cfg = CaptureConfig(log=_read_log(s), cameras=_read_cameras(s),
                        health_interval_s=s.integer("health_interval_s", required=False, default=600,
                                                    low=1, high=86400))
    s.finish()
    return cfg


# ---- inspection ----

def _read_window(parent: _Section, name: str, default: Dict[str, int]) -> Window:
    """Optional {xmin, xmax, ymin, ymax}; a missing corner keeps its default."""
    w = parent.section(name, required=False)
    values = {k: w.integer(k, required=False, default=default[k], low=0, high=100000)
              for k in ("xmin", "xmax", "ymin", "ymax")}
    w.finish()
    if None not in values.values() and not (values["xmin"] < values["xmax"] and values["ymin"] < values["ymax"]):
        w.problem("", "needs xmin < xmax and ymin < ymax")
    return Window(**values)


def _read_tracker(parent: _Section, default: Dict[str, Any]) -> Tracker:
    t = parent.section("tracker", required=False)
    c = t.section("clip_confirm", required=False)
    dc = default["clip_confirm"]
    clip = ClipConfirm(frames=c.integer("frames", required=False, default=dc["frames"], low=2, high=100),
                       min_pairs=c.integer("min_pairs", required=False, default=dc["min_pairs"], low=1, high=99),
                       max_gap_ms=c.integer("max_gap_ms", required=False, default=dc["max_gap_ms"], low=1, high=60000),
                       max_dist_px=c.integer("max_dist_px", required=False, default=dc["max_dist_px"], low=1, high=100000))
    if clip.frames is not None and clip.min_pairs is not None and clip.min_pairs > clip.frames - 1:
        c.problem("min_pairs", f"must be at most frames - 1 ({clip.frames - 1}), got {clip.min_pairs}")
    c.finish()

    k = t.section("skidding", required=False)
    dk = default["skidding"]
    ints = {name: k.integer(name, required=False, default=dk[name], low=0, high=100000)
            for name in ("window_frames", "time_gap_reset_ms", "offset_px", "min_vertical_px",
                         "min_magnet_travel_px", "wheel_xmin", "wheel_xmax", "magnet_check_min_wheel_w")}
    floats = {name: k.number(name, required=False, default=dk[name], low=0, high=10)
              for name in ("required_ratio", "vertical_factor", "same_wheel_scale")}
    skid = Skidding(**ints, **floats)
    if skid.window_frames == 0:
        k.problem("window_frames", "must be at least 1")
    if None not in (skid.wheel_xmin, skid.wheel_xmax) and skid.wheel_xmin >= skid.wheel_xmax:
        k.problem("wheel_xmin", "must be below wheel_xmax")
    k.finish()

    m = t.section("wheel_missing", required=False)
    dm = default["wheel_missing"]
    missing = WheelMissingSettings(
        min_frames=m.integer("min_frames", required=False, default=dm["min_frames"], low=1, high=100000),
        belt_stop_frames=m.integer("belt_stop_frames", required=False, default=dm["belt_stop_frames"],
                                   low=1, high=100000),
        bbox=_read_window(m, "bbox", dm["bbox"]))
    if None not in (missing.min_frames, missing.belt_stop_frames) and missing.min_frames > missing.belt_stop_frames:
        m.problem("min_frames", f"must be at most belt_stop_frames ({missing.belt_stop_frames})")
    m.finish()

    b = t.section("best_image_band_x", required=False)
    db_ = default["best_image_band_x"]
    band = BandX(min=b.integer("min", required=False, default=db_["min"], low=0, high=100000),
                 max=b.integer("max", required=False, default=db_["max"], low=0, high=100000))
    if None not in (band.min, band.max) and band.min >= band.max:
        b.problem("", "needs min < max")
    b.finish()

    tracker = Tracker(
        new_wheel_jump_px=t.integer("new_wheel_jump_px", required=False, default=default["new_wheel_jump_px"],
                                    low=1, high=100000),
        new_plate_jump_px=t.integer("new_plate_jump_px", required=False, default=default["new_plate_jump_px"],
                                    low=1, high=100000),
        high_dusty_suppress_px=t.integer("high_dusty_suppress_px", required=False,
                                         default=default["high_dusty_suppress_px"], low=0, high=100000),
        clip_confirm=clip, skidding=skid, wheel_missing=missing, best_image_band_x=band)
    t.finish()
    return tracker


def _read_side(s: _Section, side: str) -> SideSettings:
    """One side. Everything but object_sizes is optional; the default is THIS side's value in
    today's code (SIDE_DEFAULTS, D-073 D5)."""
    default = SIDE_DEFAULTS[side]
    side_section = s.section(side)
    sizes = side_section.section("object_sizes")
    values = {k: sizes.integer(k, low=1, high=100000) for k in OBJECT_SIZE_KEYS}
    sizes.finish()
    settings = SideSettings(
        object_sizes=ObjectSizes(**values),
        motion=side_section.string("motion", required=False, default=default["motion"], choices=MOTIONS),
        bucket_clip_window=_read_window(side_section, "bucket_clip_window", default["bucket_clip_window"]),
        half_bucket_clip_window=_read_window(side_section, "half_bucket_clip_window",
                                             default["half_bucket_clip_window"]),
        tracker=_read_tracker(side_section, default["tracker"]),
    )
    side_section.finish()
    return settings


def _read_sides(parent: _Section, sides: List[str]) -> Dict[str, SideSettings]:
    """`inspection.sides`: exactly one entry per configured camera side."""
    s = parent.section("sides")
    result = {side: _read_side(s, side) for side in sides}
    if not sides:      # no valid camera: those problems are already reported
        s.used.update(s.data)
    for key in (s.data if sides else ()):
        if key not in sides:
            s.problem(str(key), "no camera is configured for this side" if key in CAMERA_SIDES.values()
                      else "unknown key (use LEFT or RIGHT)")
            s.used.add(key)
    s.finish()
    return result


def _read_machine(parent: _Section) -> MachineConfig:
    s = parent.section("machine")
    total = s.integer("total_wheel_count", low=1, high=100000)
    expected = s.integer("expected_cycle_total", required=False, default=total, low=1, high=100000)
    if "wheel_stand_required_counts" in s.data:
        s.used.add("wheel_stand_required_counts")
        s.problem("wheel_stand_required_counts", "withdrawn (D-073 D8): the wheel-stand mapping is learned by "
                                                 "inspection (data/state/inspection_<side>/wheel_stands.json)")
    s.finish()
    return MachineConfig(total_wheel_count=total, expected_cycle_total=expected)


MOVED_TO_SUPERVISOR = ("moved to the Supervisor (D-060): the running version is "
                       "data/models/<name>/current, managed with the Supervisor's model commands")


def _read_model_ref(parent: _Section, paths: Paths) -> ModelRef:
    s = parent.section("model")
    name = s.string("name", pattern=_MODEL_PART_RE,
                    pattern_hint="use letters, digits, '.', '_' or '-' (it is a folder name)")
    if "version" in s.data:
        s.used.add("version")
        s.problem("version", MOVED_TO_SUPERVISOR)
    s.finish()
    return ModelRef(name=name, root=os.path.join(paths.data, "models", name) if name and paths.root else None)


def _read_auto_roi(parent: _Section) -> AutoRoi:
    s = parent.section("auto_roi", required=False)
    roi = AutoRoi(mode=s.choice("mode", AUTO_ROI_MODES, default="observe"),     # D-054 starts in observe
                  learn_images=s.integer("learn_images", required=False, default=30, low=1, high=100000))
    s.finish()
    return roi


def _read_wheel_report(parent: _Section) -> WheelReport:
    s = parent.section("wheel_report", required=False)
    report = WheelReport(daily_time=s.string("daily_time", required=False, default="17:58", pattern=_HH_MM_RE,
                                             pattern_hint='use "HH:MM" (24 h, quoted)'))
    s.finish()
    return report


def _read_inspection(top: _Section, paths: Paths, sides: List[str]) -> InspectionConfig:
    s = top.section("inspection")
    # Today only Right writes the live wheel number (D-073 D3): RIGHT when that side exists.
    default_writer = "RIGHT" if "RIGHT" in sides or not sides else sides[0]
    writer = s.string("live_wheel_counter_side", required=False, default=default_writer,
                      choices=tuple(CAMERA_SIDES.values()))
    if writer is not None and sides and writer not in sides:
        s.problem("live_wheel_counter_side", f"{writer} has no camera on this site")
    cfg = InspectionConfig(
        log=_read_log(s),
        model=_read_model_ref(s, paths),
        threshold=s.number("threshold", low=0, high=1, low_exclusive=True, high_exclusive=True),
        machine=_read_machine(s),
        sides=_read_sides(s, sides),
        auto_roi=_read_auto_roi(s),
        live_wheel_counter_side=writer,
        wheel_report=_read_wheel_report(s),
    )
    s.finish()
    return cfg


# ---- plc ----

def _read_plc(top: _Section) -> PlcConfig:
    s = top.section("plc")
    ip = _ipv4(s, "ip")
    legacy = None
    if s.present("legacy_db_numbers"):
        ls = s.section("legacy_db_numbers", required=False)
        legacy = {"write": ls.integer("write", low=1), "read": ls.integer("read", low=1)}
        ls.finish()
    else:
        s.used.add("legacy_db_numbers")
    cfg = PlcConfig(
        type=s.string("type", required=False, default="modbus_tcp", choices=("modbus_tcp",)),
        ip=ip,
        port=s.integer("port", required=False, default=502, low=1, high=65535),
        timeout_s=s.number("timeout_s", required=False, default=3.0, low=0, low_exclusive=True, high=60),
        light_register=s.integer("light_register", low=0, high=65535),
        cleaning_register=s.integer("cleaning_register", low=0, high=65535),
        conveyor_status_register=s.integer("conveyor_status_register", low=0, high=65535),
        cleaning_pulse_seconds=s.number("cleaning_pulse_seconds", required=False, default=10.0,
                                        low=0, low_exclusive=True, high=600),
        legacy_db_numbers=legacy,
        # Tadipatri's light register is inverted (0 = ON); writing 1 there would switch it off
        light_on_value=s.integer("light_on_value", required=False, default=1, choices=(0, 1)),
        log=_read_log(s),
    )
    s.finish()
    return cfg


# ---- mail ----

def _read_smtp(s: _Section, sender: Optional[str]) -> Optional[SmtpSettings]:
    """`mail.smtp`: required with sender smtp; absent or null otherwise."""
    if sender != "smtp":
        s.used.add("smtp")
        if sender is not None and s.present("smtp"):
            s.problem("smtp", "only allowed when sender is smtp (use null or leave it out)")
        return None
    sm = s.section("smtp")
    # host and port defaults are the values hardcoded in today's SMTP sender
    smtp = SmtpSettings(host=sm.string("host", required=False, default="smtp.gmail.com"),
                        port=sm.integer("port", required=False, default=587, low=1, high=65535),
                        password=sm.string("password", secret=True))
    sm.finish()
    return smtp


def _read_mail(top: _Section, paths: Paths) -> MailConfig:
    s = top.section("mail")
    sender = s.string("sender", choices=("graph", "smtp"))
    email_hint = "must be an email address"
    recipients = s.string_list("recipients", item_pattern=_EMAIL_RE, item_hint=email_hint)
    if recipients is not None and not recipients:
        s.problem("recipients", "needs at least one address")
    # [] or absent: the daily disk report goes to the normal recipients
    disk_report_to = s.string_list("disk_report_to", required=False, default=(),
                                   item_pattern=_EMAIL_RE, item_hint=email_hint)
    cfg = MailConfig(
        sender=sender,
        project_name=s.string("project_name"),
        report_dir=_resolve(paths, s.string("report_dir", required=False, default="data/reports")),
        recipients=recipients,
        cc=s.string_list("cc", required=False, default=(), item_pattern=_EMAIL_RE, item_hint=email_hint),
        sender_address=s.string("sender_address", pattern=_EMAIL_RE, pattern_hint=email_hint),
        disk_report_to=disk_report_to or recipients,
        smtp=_read_smtp(s, sender),
        log=_read_log(s),
        report_url=s.string("report_url", required=False),
        max_alert_age_hours=_read_max_age(s),
        flood=_read_flood(s, email_hint),
    )
    s.finish()
    return cfg


def _read_max_age(s: _Section) -> Optional[float]:
    """Absent = 24 h; explicit null = off (no age limit); else a number > 0."""
    if "max_alert_age_hours" in s.data and s.data["max_alert_age_hours"] is None:
        s.used.add("max_alert_age_hours")
        return None
    return s.number("max_alert_age_hours", required=False, default=24.0, low=0, low_exclusive=True, high=100000)


def _read_flood(s: _Section, email_hint: str) -> Optional[MailFlood]:
    """Absent or null = off (D-083). Given = all three keys required."""
    if not s.present("flood"):
        s.used.add("flood")
        return None
    f = s.section("flood")
    internal = f.string_list("internal_to", item_pattern=_EMAIL_RE, item_hint=email_hint)
    if internal is not None and not internal:
        f.problem("internal_to", "needs at least one address")
    flood = MailFlood(max_per_window=f.integer("max_per_window", low=1, high=100000),
                      window_hours=f.number("window_hours", low=0, low_exclusive=True, high=100000),
                      internal_to=internal)
    f.finish()
    return flood


# ---- cleanup ----

def _read_keep_limit(s: _Section, name: str, days: int, items: int) -> KeepLimit:
    k = s.section(name, required=False)
    limit = KeepLimit(days=k.integer("days", required=False, default=days, low=1, high=3650),
                      items=k.integer("items", required=False, default=items, low=1, high=10000000))
    k.finish()
    return limit


def _read_cleanup(top: _Section) -> CleanupConfig:
    # Defaults are today's deleter_config values.
    s = top.section("cleanup", required=False)
    d = s.section("disk_percent", required=False)
    defaults = {"delete": 75, "raw_delete": 85, "mail": 90, "inference_delete": 95}
    disk = {key: d.integer(key, required=False, default=defaults[key], low=1, high=99)
            for key in DISK_PERCENT_ORDER}
    d.finish()
    values = [disk[k] for k in DISK_PERCENT_ORDER]
    if None not in values and values != sorted(set(values)):
        d.problem("", "must increase strictly in the order "
                      + " < ".join(f"{k} ({disk[k]})" for k in DISK_PERCENT_ORDER))
    cfg = CleanupConfig(
        raw_image_hours=s.integer("raw_image_hours", required=False, default=24, low=1, high=8760),
        interval_seconds=s.integer("interval_seconds", required=False, default=10, low=1, high=86400),
        repeated=_read_keep_limit(s, "repeated", 20, 200),
        plate_days=s.integer("plate_days", required=False, default=10, low=1, high=3650),
        defective=_read_keep_limit(s, "defective", 10, 200),
        disk_percent=disk,
        log=_read_log(s),
        disk_report_time=s.string("disk_report_time", required=False, default="09:00", pattern=_HH_MM_RE,
                                  pattern_hint='use "HH:MM" (24 h, quoted)'),
    )
    s.finish()
    return cfg


# ---- transfer (D-057, D-060) ----

def _read_transfer(top: _Section, site_id: Optional[str]) -> Optional[TransferConfig]:
    """Optional: absent means this site runs no transfer service."""
    if not top.present("transfer"):
        top.used.add("transfer")
        return None
    s = top.section("transfer")
    upload = None
    if s.present("upload"):
        u = s.section("upload")
        folder = u.string("remote_folder", required=False, default=f"DPC/data/{SITE_ID_TOKEN}")
        upload = TransferUpload(
            weekday=u.string("weekday", required=False, default="sunday", choices=WEEKDAYS),
            chunk_mb=u.integer("chunk_mb", required=False, default=500, low=1, high=100000),
            remote_folder=folder.replace(SITE_ID_TOKEN, site_id) if folder and site_id else folder,
        )
        u.finish()
    else:
        s.used.add("upload")
    cfg = TransferConfig(upload=upload,
                         models_folder=s.string("models_folder", required=False, default="DPC/models"),
                         log=_read_log(s))
    s.finish()
    return cfg


def _resolve(paths: Paths, value: Optional[str]) -> Optional[str]:
    if value is None or paths.root is None:
        return None
    return paths.resolve(value)


# ======================================================================
# Public: site.yaml
# ======================================================================

def load_site_config(path: Optional[str] = None) -> SiteConfig:
    """Read, default and validate site.yaml (schema v3). Raises SiteConfigError listing every problem."""
    path = os.path.abspath(os.path.expanduser(path)) if path else default_config_path()
    data = _read_yaml(path)
    old_version = data.get("schema_version") if isinstance(data, dict) else None
    if old_version == 2:
        raise SiteConfigError(path, v2_move_problems(data))
    if old_version == 1:
        raise SiteConfigError(path, ["schema_version: this is a v1 file; start from "
                                     "config/examples/site.example.yaml (v3, CONTRACTS 7.1)"])

    problems: List[str] = []
    top = _Section(data, "", problems)
    version = top.integer("schema_version")
    if version is not None and version != SCHEMA_VERSION:
        top.problem("schema_version", f"must be {SCHEMA_VERSION}, got {version}")

    site = _read_site(top)
    paths = _read_paths(top)
    db = _read_db(top)
    mqtt = _read_mqtt(top)
    capture = _read_capture(top)
    sides = [c.side for c in capture.cameras if c.side]
    inspection = _read_inspection(top, paths, sides)
    plc = _read_plc(top)
    mail = _read_mail(top, paths)
    cleanup = _read_cleanup(top)
    transfer = _read_transfer(top, site.id)
    graph = _read_graph(top, _graph_users(mail, transfer))
    if "models" in top.data:
        top.used.add("models")
        top.problem("models", "moved to the Supervisor config (workers.yaml section models, D-060); "
                              "the OneDrive folder is transfer.models_folder")
    top.finish()

    if problems:
        raise SiteConfigError(path, problems)
    return SiteConfig(schema_version=version, site=site, paths=paths, db=db, mqtt=mqtt, graph=graph,
                      capture=capture, inspection=inspection, plc=plc, mail=mail, cleanup=cleanup,
                      transfer=transfer, source_path=path, warnings=tuple(_warnings(db)))


def _graph_users(mail: MailConfig, transfer: Optional[TransferConfig]) -> List[str]:
    """Which programs need the shared `graph` login on this site."""
    users = []
    if mail.sender == "graph":
        users.append("mail (sender graph)")
    if transfer is not None:
        users.append("transfer")
    return users


def _warnings(db: DbConfig) -> List[str]:
    """Allowed but not recommended settings. Never include a secret value here."""
    warnings = []
    if db.user == "root":
        # Allowed since D-050; a leaked site.yaml would then open every database on the server.
        warnings.append("db.user is root; a dedicated user with limited rights is recommended (D-030)")
    return warnings


# ======================================================================
# v2 -> v3 (D-055): which keys moved. Used for the error message and by
# scripts/convert_site_yaml_v3.py. Key names only, never values.
# ======================================================================

PROGRAM_LOGS = ("capture", "inspection", "plc", "mail", "cleanup")
MODEL_PACKAGE_KEYS = ("dir", "weights", "class_json", "detectron_yaml", "classes", "detect_labels")
_TO_MODEL_PACKAGE = None     # marks "goes into the model package, not into site.yaml"


def _get(data: Any, *keys) -> Any:
    for key in keys:
        if not isinstance(data, dict) or key not in data:
            return _MISSING
        data = data[key]
    return data


def v2_moves(data: Dict[str, Any]) -> List[Tuple[str, Optional[str]]]:
    """(old key, new key) for every v2 key present that moves in v3. New key None = it now
    comes from the model package (CONTRACTS 12) and is removed from site.yaml."""
    moves: List[Tuple[str, Optional[str]]] = []
    if _get(data, "logging", "backup_count") is not _MISSING:
        moves.append(("logging.backup_count", "<program>.log.backup_count (" + ", ".join(PROGRAM_LOGS) + ")"))
    if _get(data, "cameras") is not _MISSING:
        moves.append(("cameras", "capture.cameras"))
    for key in ("total_wheel_count", "expected_cycle_total", "wheel_stand_required_counts"):
        if _get(data, "machine", key) is not _MISSING:
            moves.append((f"machine.{key}", f"inspection.machine.{key}"))
    sizes = _get(data, "machine", "object_sizes")
    for side in (sizes if isinstance(sizes, dict) else {}):
        moves.append((f"machine.object_sizes.{side}", f"inspection.sides.{side}.object_sizes"))
    if _get(data, "model", "threshold") is not _MISSING:
        moves.append(("model.threshold", "inspection.threshold"))
    for key in MODEL_PACKAGE_KEYS:
        if _get(data, "model", key) is not _MISSING:
            moves.append((f"model.{key}", _TO_MODEL_PACKAGE))
    for key in ("tenant_id", "client_id", "client_secret"):
        if _get(data, "mail", "graph", key) is not _MISSING:
            moves.append((f"mail.graph.{key}", f"graph.{key}"))
    retention = _get(data, "retention")
    for key in (retention if isinstance(retention, dict) else {}):
        new = "cleanup.interval_seconds" if key == "cleanup_interval_seconds" else f"cleanup.{key}"
        moves.append((f"retention.{key}", new))
    return moves


def v2_move_problems(data: Dict[str, Any]) -> List[str]:
    lines = ["schema_version: this is a v2 file; move these keys, then set schema_version: 3 "
             "(CONTRACTS 7.1, D-055; scripts/convert_site_yaml_v3.py does it for you)"]
    for old, new in v2_moves(data):
        if new is _TO_MODEL_PACKAGE:
            lines.append(f"{old} -> remove: it comes from the model package now (CONTRACTS 12)")
        else:
            lines.append(f"{old} -> {new}")
    lines.append("inspection.model -> add {name} of the model package (D-056); "
                 "the version is managed by the Supervisor (D-060)")
    return lines


def convert_v2_to_v3(data: Dict[str, Any], model_name: str) -> Dict[str, Any]:
    """A v3 mapping built from a v2 mapping, following v2_moves(). Values are copied, never
    printed. Keys that did not move are copied as they are; the result still has to pass
    load_site_config (run the check after converting)."""
    import copy
    old = copy.deepcopy(data)
    log = {"backup_count": old.pop("logging", {}).get("backup_count", 5)}
    machine = old.pop("machine", {}) or {}
    model = old.pop("model", {}) or {}
    mail = old.pop("mail", {}) or {}
    graph = mail.pop("graph", None)
    retention = old.pop("retention", {}) or {}
    if "cleanup_interval_seconds" in retention:
        retention["interval_seconds"] = retention.pop("cleanup_interval_seconds")
    sizes = machine.pop("object_sizes", {}) or {}

    new: Dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    for key in ("site", "paths", "db", "mqtt"):
        if key in old:
            new[key] = old.pop(key)
    if graph is not None:
        new["graph"] = graph
    new["capture"] = {"log": dict(log), "cameras": old.pop("cameras", [])}
    inspection = {"log": dict(log), "model": {"name": model_name}}
    if "threshold" in model:
        inspection["threshold"] = model["threshold"]
    inspection["machine"] = machine
    inspection["sides"] = {side: {"object_sizes": value} for side, value in sizes.items()}
    inspection["auto_roi"] = {"mode": "observe", "learn_images": 30}
    new["inspection"] = inspection
    if "plc" in old:
        new["plc"] = dict(old.pop("plc"), log=dict(log))
    new["mail"] = dict(mail, log=dict(log))
    new["cleanup"] = dict(retention, log=dict(log))
    old.pop("schema_version", None)
    new.update(old)              # anything left (unknown keys) is kept so the check reports it
    return new


# ======================================================================
# Public: redacted copy (safe to paste into a chat, mirror or UI)
# ======================================================================

# Secret keys from CONTRACTS 7 and 9, as key paths.
SECRET_KEY_PATHS = {
    ("db", "user"), ("db", "password"),
    ("mqtt", "user"), ("mqtt", "password"),
    ("graph", "tenant_id"), ("graph", "client_id"), ("graph", "client_secret"),
    ("mail", "smtp", "password"),
}
# Belt and braces: also hide anything whose key name looks secret, wherever it is
# (a misspelt "pasword:" or a key added by hand must not leak either).
_SECRET_NAME_RE = re.compile(r"pass|pasw|pwd|secret|token|api_?key|tenant|client_id", re.IGNORECASE)
_ANCHOR_OR_TAG_RE = re.compile(r"^(?:[&!]\S*\s+)*")   # "&name " / "!!str " before a value: keep them
REDACTED = '"***"'


def _secret_spans(node, path=(), inside_secret=False) -> List[Tuple[int, int]]:
    """Character ranges of every secret value in the composed YAML tree."""
    if isinstance(node, yaml.ScalarNode):
        quoted = node.style in ("'", '"')
        is_set = node.value != "" and (quoted or node.value not in ("~", "null"))   # "" and null stay visible
        if inside_secret and is_set:
            return [(node.start_mark.index, node.end_mark.index)]
        return []
    spans = []
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            key = key_node.value if isinstance(key_node, yaml.ScalarNode) else ""
            child = path + (key,)
            secret = inside_secret or child in SECRET_KEY_PATHS or bool(_SECRET_NAME_RE.search(key))
            spans += _secret_spans(value_node, child, secret)
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            spans += _secret_spans(item, path + (index,), inside_secret)
    return spans


def redact_text(text: str, source: str = "<text>") -> str:
    """The same text, comments and layout kept, with each secret value replaced by "***"."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        # Cannot tell where the secrets are, so print nothing of the file.
        raise SiteConfigError(source, [_yaml_error_message(exc)]) from None
    spans = set(_secret_spans(root)) if root is not None else set()   # a set: YAML aliases repeat a node
    for start, end in sorted(spans, reverse=True):
        value = text[start:end]
        start += _ANCHOR_OR_TAG_RE.match(value).end()      # keep anchors so aliases still resolve
        end = start + len(text[start:end].rstrip())        # keep the line break after a block scalar
        text = text[:start] + REDACTED + text[end:]
    return text


def redacted_copy(path: Optional[str] = None) -> str:
    """site.yaml with every secret replaced by "***". Does not validate the file, so a broken
    file can still be shared to get help. Comments are kept as they are: never write a
    secret in a comment."""
    path = os.path.abspath(os.path.expanduser(path)) if path else default_config_path()
    return redact_text(_read_text(path), path)


# ======================================================================
# Public: env vars for legacy workers (CONTRACTS 9 env bridge, D-049)
# ======================================================================

def legacy_env_vars(config: SiteConfig) -> Dict[str, str]:
    """The DPC_* variables the Supervisor exports to workers with `env_from_site: true`.
    Only variables that have a value are returned (e.g. no DPC_SMTP_PASSWORD on a Graph site)."""
    mail = config.mail
    env = {
        "DPC_DB_USER": config.db.user,
        "DPC_DB_PASS": config.db.password,
        "DPC_MAIL_SENDER": mail.sender_address,
        "DPC_DISK_REPORT_TO": ",".join(mail.disk_report_to),
        "DPC_SITE_NAME": config.site.name,
    }
    if config.graph is not None:
        graph = config.graph
        env.update({"DPC_MS_TENANT_ID": graph.tenant_id, "DPC_MS_CLIENT_ID": graph.client_id,
                    "DPC_MS_CLIENT_SECRET": graph.client_secret})
    if mail.smtp is not None:
        env["DPC_SMTP_PASSWORD"] = mail.smtp.password
    if config.mqtt.user is not None:
        env.update({"DPC_MQTT_USER": config.mqtt.user, "DPC_MQTT_PASS": config.mqtt.password})
    return env


# ======================================================================
# Command line
# ======================================================================

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m common.config.site_config",
                                     description="Check a DPC site.yaml (schema v3).")
    parser.add_argument("path", nargs="?", help=f"site.yaml (default: ${CONFIG_ENV_VAR} or {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--redacted", action="store_true",
                        help="print the file with every secret replaced by *** (safe to share); no check")
    args = parser.parse_args(argv)
    try:
        if args.redacted:
            print(redacted_copy(args.path), end="")
            return 0
        config = load_site_config(args.path)
    except SiteConfigError as exc:
        print(exc)
        return 1
    print("OK")
    for warning in config.warnings:
        print(f"WARNING {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
