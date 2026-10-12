"""Load and validate the site config `site.yaml` (schema v1, CONTRACTS section 7, D-047)
and the site secrets `secrets.env` (CONTRACTS section 9).

Public API
    load_site_config(path=None) -> SiteConfig
    load_secrets(config, path=None) -> Secrets
    SiteConfigError            (.problems = list of "key.path: what is wrong")

Command line
    python -m common.config.site_config [path] [--with-secrets]
    prints OK or every problem; exit code 0 (valid) or 1 (invalid).

Why it is strict: one file sets up a whole 24x7 site, so a typo must stop the
start-up with a clear message instead of silently falling back to a default.
That is why unknown keys, duplicate keys and wrong types are all errors, and why
every problem is collected before raising (fix them all in one go).

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

SCHEMA_VERSION = 1
CONFIG_ENV_VAR = "DPC_SITE_CONFIG"
DEFAULT_CONFIG_PATH = "~/INSIGHTZZ/config/site.yaml"

CAMERA_SIDES = {"CAM1": "LEFT", "CAM2": "RIGHT"}      # CONTRACTS 4.1: CAM1 -> LEFT, CAM2 -> RIGHT
CAMERA_DRIVERS = ("basler", "flir")
ROTATIONS = (0, 90, 180, 270)
FRAME_SUBDIRS = ("live", "_tmp", "INF", "RAW_IMAGES", "UI")
IMAGE_KINDS = ("DEFECTIVE", "REPEATED", "RAW", "PLATE")
OBJECT_SIZE_KEYS = ("wheel_w", "wheel_h", "bucket_clip_w", "chain_w", "half_bucket_clip_w")
DISK_PERCENT_ORDER = ("delete", "raw_delete", "mail", "inference_delete")   # must increase in this order

_SITE_ID_RE = re.compile(r"^[a-z0-9_-]+$")
# DB and table names end up inside SQL text, so only plain identifiers are allowed.
_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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
    def secrets_file(self) -> str:
        return os.path.join(self.config, "secrets.env")

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
class LoggingConfig:
    backup_count: int


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    name: str
    health_table: str


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int


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

    @property
    def log_file_name(self) -> str:
        return f"FrameCapture_{self.id}.log"

    @property
    def defect_side(self) -> str:
        """Legacy DEFECT_SIDE value: Left | Right."""
        return self.side.capitalize()


@dataclass(frozen=True)
class ObjectSizes:
    wheel_w: int
    wheel_h: int
    bucket_clip_w: int
    chain_w: int
    half_bucket_clip_w: int


@dataclass(frozen=True)
class MachineConfig:
    total_wheel_count: int
    expected_cycle_total: Optional[int]
    wheel_stand_required_counts: Dict[int, int]
    object_sizes: Dict[str, ObjectSizes]      # side -> sizes


@dataclass(frozen=True)
class ModelConfig:
    dir: str             # absolute
    weights: str         # absolute file path inside dir
    class_json: str      # absolute file path inside dir
    detectron_yaml: str  # absolute
    threshold: float
    classes: Tuple[str, ...]
    detect_labels: Tuple[str, ...]


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


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int


@dataclass(frozen=True)
class MailConfig:
    sender: str          # graph | smtp
    project_name: str
    report_dir: str      # absolute
    recipients: Tuple[str, ...]
    cc: Tuple[str, ...]
    smtp: Optional[SmtpSettings]


@dataclass(frozen=True)
class KeepLimit:
    days: int
    items: int


@dataclass(frozen=True)
class RetentionConfig:
    raw_image_hours: int
    cleanup_interval_seconds: int
    repeated: KeepLimit
    plate_days: int
    defective: KeepLimit
    disk_percent: Dict[str, int]


@dataclass(frozen=True)
class SiteConfig:
    schema_version: int
    site: SiteInfo
    paths: Paths
    logging: LoggingConfig
    db: DbConfig
    mqtt: MqttConfig
    cameras: Tuple[Camera, ...]
    machine: MachineConfig
    model: ModelConfig
    plc: PlcConfig
    mail: MailConfig
    retention: RetentionConfig
    source_path: str

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

    def string(self, name, required=True, default=None, choices=None, pattern=None, pattern_hint=""):
        value = self._raw(name, required)
        if value is _MISSING:
            return default
        if not isinstance(value, str):
            hint = " (quote it)" if isinstance(value, (int, float)) else ""
            self.problem(name, f"must be a string{hint}, got {value!r}")
            return None
        if not value.strip():
            self.problem(name, "must not be empty")
            return None
        if choices is not None and value not in choices:
            self.problem(name, f"must be one of {', '.join(map(str, choices))}, got {value!r}")
            return None
        if pattern is not None and not pattern.match(value):
            self.problem(name, f"{pattern_hint}, got {value!r}")
            return None
        return value

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


def _read_yaml(path: str) -> Any:
    if not os.path.isfile(path):
        raise SiteConfigError(path, ["file not found"])
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.load(f, Loader=_StrictLoader)
    except yaml.YAMLError as exc:
        raise SiteConfigError(path, [f"not valid YAML: {exc}"]) from None


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


def _read_logging(top: _Section) -> LoggingConfig:
    s = top.section("logging", required=False)
    cfg = LoggingConfig(backup_count=s.integer("backup_count", required=False, default=5, low=1, high=365))
    s.finish()
    return cfg


def _read_db(top: _Section) -> DbConfig:
    s = top.section("db")
    hint = "use only letters, digits and _"
    cfg = DbConfig(
        host=s.string("host", required=False, default="localhost"),
        port=s.integer("port", required=False, default=3306, low=1, high=65535),
        name=s.string("name", pattern=_SQL_IDENTIFIER_RE, pattern_hint=hint),
        health_table=s.string("health_table", required=False, default="health_status",
                              pattern=_SQL_IDENTIFIER_RE, pattern_hint=hint),
    )
    s.finish()
    return cfg


def _read_mqtt(top: _Section) -> MqttConfig:
    s = top.section("mqtt", required=False)
    cfg = MqttConfig(
        host=s.string("host", required=False, default="localhost"),
        port=s.integer("port", required=False, default=1883, low=1, high=65535),
    )
    s.finish()
    return cfg


def _read_camera(item: Any, key: str, problems: List[str]) -> Camera:
    s = _Section(item, key, problems)
    driver = s.string("driver", choices=CAMERA_DRIVERS)
    flir = None
    if s.present("flir") or driver == "flir":
        # Required with driver flir, not allowed otherwise (cross-field check below).
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
    )
    s.finish()
    if cam.id and cam.side and CAMERA_SIDES[cam.id] != cam.side:
        s.problem("side", f"{cam.id} must be {CAMERA_SIDES[cam.id]} (CAM1 = LEFT, CAM2 = RIGHT), got {cam.side}")
    return cam


def _read_cameras(top: _Section) -> Tuple[Camera, ...]:
    items = top._raw("cameras", required=True)
    if items is _MISSING:
        return ()
    if not isinstance(items, list) or not 1 <= len(items) <= 2:
        top.problem("cameras", "must be a list with 1 or 2 cameras")
        return ()
    cameras = tuple(_read_camera(item, f"cameras[{i}]", top.problems) for i, item in enumerate(items))
    for attr in ("id", "side", "serial"):
        values = [getattr(c, attr) for c in cameras if getattr(c, attr) is not None]
        for value in sorted({v for v in values if values.count(v) > 1}):
            top.problem("cameras", f"{attr} {value!r} is used by more than one camera")
    return cameras


def _read_object_sizes(s: _Section, sides: List[str]) -> Dict[str, ObjectSizes]:
    sizes_section = s.section("object_sizes")
    result = {}
    for side in sides:
        side_section = sizes_section.section(side)
        values = {k: side_section.integer(k, low=1, high=100000) for k in OBJECT_SIZE_KEYS}
        side_section.finish()
        result[side] = ObjectSizes(**values)
    # Only the sides that have a camera are allowed (CONTRACTS 7 cross-field check).
    # Skipped when no camera is valid: the camera problems are already reported.
    if not sides:
        sizes_section.used.update(sizes_section.data)
    for key in (sizes_section.data if sides else ()):
        if key not in sides:
            sizes_section.problem(str(key), "no camera is configured for this side" if key in CAMERA_SIDES.values()
                                  else "unknown key (use LEFT or RIGHT)")
            sizes_section.used.add(key)
    sizes_section.finish()
    return result


def _read_wheel_stand_counts(s: _Section, total_wheels: Optional[int]) -> Dict[int, int]:
    raw = s._raw("wheel_stand_required_counts", required=False)
    if raw is _MISSING:
        return {}
    if not isinstance(raw, dict):
        s.problem("wheel_stand_required_counts", "must be a mapping of wheel number -> required count")
        return {}
    result = {}
    for wheel, count in raw.items():
        where = f"wheel_stand_required_counts[{wheel!r}]"
        if isinstance(wheel, bool) or not isinstance(wheel, int) or wheel < 1:
            s.problem(where, "wheel number must be an integer >= 1")
        elif total_wheels is not None and wheel > total_wheels:
            s.problem(where, f"wheel number is above total_wheel_count ({total_wheels})")
        elif isinstance(count, bool) or not isinstance(count, int) or count < 1:
            s.problem(where, f"required count must be an integer >= 1, got {count!r}")
        else:
            result[wheel] = count
    return result


def _read_machine(top: _Section, sides: List[str]) -> MachineConfig:
    s = top.section("machine")
    total = s.integer("total_wheel_count", low=1, high=100000)
    cfg = MachineConfig(
        total_wheel_count=total,
        expected_cycle_total=s.integer("expected_cycle_total", required=False, low=1, high=100000),
        wheel_stand_required_counts=_read_wheel_stand_counts(s, total),
        object_sizes=_read_object_sizes(s, sides),
    )
    s.finish()
    return cfg


def _read_model(top: _Section, paths: Paths) -> ModelConfig:
    s = top.section("model")
    model_dir = s.string("dir")
    weights = s.string("weights", required=False, default="model_final.pth")
    class_json = s.string("class_json")
    detectron_yaml = s.string("detectron_yaml")
    classes = s.string_list("classes")
    if classes is not None and not classes:
        s.problem("classes", "must not be empty")
    if classes and len(set(classes)) != len(classes):
        s.problem("classes", "contains a class more than once")
    detect_labels = s.string_list("detect_labels", required=False, default=classes)
    if classes and detect_labels:
        for label in detect_labels:
            if label not in classes:
                s.problem("detect_labels", f"{label!r} is not in classes")
    cfg = ModelConfig(
        dir=_resolve(paths, model_dir),
        # weights and class_json are files inside the model folder
        weights=_join(_resolve(paths, model_dir), weights),
        class_json=_join(_resolve(paths, model_dir), class_json),
        detectron_yaml=_resolve(paths, detectron_yaml),
        threshold=s.number("threshold", low=0, high=1, low_exclusive=True, high_exclusive=True),
        classes=classes,
        detect_labels=detect_labels,
    )
    s.finish()
    return cfg


def _read_plc(top: _Section) -> PlcConfig:
    s = top.section("plc")
    ip = s.string("ip")
    if ip is not None:
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            s.problem("ip", f"must be an IPv4 address, got {ip!r}")
            ip = None
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
    )
    s.finish()
    return cfg


def _read_mail(top: _Section, paths: Paths) -> MailConfig:
    s = top.section("mail")
    sender = s.string("sender", choices=("graph", "smtp"))
    email_hint = "must be an email address"
    recipients = s.string_list("recipients", item_pattern=_EMAIL_RE, item_hint=email_hint)
    if recipients is not None and not recipients:
        s.problem("recipients", "needs at least one address")
    smtp = None
    if sender == "smtp":
        sm = s.section("smtp", required=False)
        # Defaults are the values hardcoded in today's SMTP sender.
        smtp = SmtpSettings(host=sm.string("host", required=False, default="smtp.gmail.com"),
                            port=sm.integer("port", required=False, default=587, low=1, high=65535))
        sm.finish()
    elif s.present("smtp"):
        s.used.add("smtp")
        s.problem("smtp", "only allowed when sender is smtp")
    cfg = MailConfig(
        sender=sender,
        project_name=s.string("project_name"),
        report_dir=_resolve(paths, s.string("report_dir", required=False, default="data/reports")),
        recipients=recipients,
        cc=s.string_list("cc", required=False, default=(), item_pattern=_EMAIL_RE, item_hint=email_hint),
        smtp=smtp,
    )
    s.finish()
    return cfg


def _read_keep_limit(s: _Section, name: str, days: int, items: int) -> KeepLimit:
    k = s.section(name, required=False)
    limit = KeepLimit(days=k.integer("days", required=False, default=days, low=1, high=3650),
                      items=k.integer("items", required=False, default=items, low=1, high=10000000))
    k.finish()
    return limit


def _read_retention(top: _Section) -> RetentionConfig:
    # Defaults are today's deleter_config values.
    s = top.section("retention", required=False)
    d = s.section("disk_percent", required=False)
    defaults = {"delete": 75, "raw_delete": 85, "mail": 90, "inference_delete": 95}
    disk = {key: d.integer(key, required=False, default=defaults[key], low=1, high=99)
            for key in DISK_PERCENT_ORDER}
    d.finish()
    values = [disk[k] for k in DISK_PERCENT_ORDER]
    if None not in values and values != sorted(set(values)):
        d.problem("", "must increase strictly in the order "
                      + " < ".join(f"{k} ({disk[k]})" for k in DISK_PERCENT_ORDER))
    cfg = RetentionConfig(
        raw_image_hours=s.integer("raw_image_hours", required=False, default=24, low=1, high=8760),
        cleanup_interval_seconds=s.integer("cleanup_interval_seconds", required=False, default=10,
                                           low=1, high=86400),
        repeated=_read_keep_limit(s, "repeated", 20, 200),
        plate_days=s.integer("plate_days", required=False, default=10, low=1, high=3650),
        defective=_read_keep_limit(s, "defective", 10, 200),
        disk_percent=disk,
    )
    s.finish()
    return cfg


def _resolve(paths: Paths, value: Optional[str]) -> Optional[str]:
    if value is None or paths.root is None:
        return None
    return paths.resolve(value)


def _join(folder: Optional[str], name: Optional[str]) -> Optional[str]:
    if folder is None or name is None:
        return None
    return os.path.normpath(os.path.join(folder, os.path.expanduser(name)))


# ======================================================================
# Public: site.yaml
# ======================================================================

def load_site_config(path: Optional[str] = None) -> SiteConfig:
    """Read, default and validate site.yaml. Raises SiteConfigError listing every problem."""
    path = os.path.abspath(os.path.expanduser(path)) if path else default_config_path()
    problems: List[str] = []
    top = _Section(_read_yaml(path), "", problems)

    version = top.integer("schema_version")
    if version is not None and version != SCHEMA_VERSION:
        top.problem("schema_version", f"must be {SCHEMA_VERSION}, got {version}")

    site = _read_site(top)
    paths = _read_paths(top)
    logging_cfg = _read_logging(top)
    db = _read_db(top)
    mqtt = _read_mqtt(top)
    cameras = _read_cameras(top)
    sides = [c.side for c in cameras if c.side]
    machine = _read_machine(top, sides)
    model = _read_model(top, paths)
    plc = _read_plc(top)
    mail = _read_mail(top, paths)
    retention = _read_retention(top)
    top.finish()

    if problems:
        raise SiteConfigError(path, problems)
    return SiteConfig(schema_version=version, site=site, paths=paths, logging=logging_cfg, db=db,
                      mqtt=mqtt, cameras=cameras, machine=machine, model=model, plc=plc, mail=mail,
                      retention=retention, source_path=path)


# ======================================================================
# Public: secrets.env
# ======================================================================

SECRET_KEYS = (
    "DPC_DB_USER", "DPC_DB_PASS",
    "DPC_MS_TENANT_ID", "DPC_MS_CLIENT_ID", "DPC_MS_CLIENT_SECRET",
    "DPC_MAIL_SENDER", "DPC_SMTP_PASSWORD", "DPC_DISK_REPORT_TO",
    "DPC_MQTT_USER", "DPC_MQTT_PASS",
)
# Retired keys are accepted with a warning so an old secrets.env does not stop a site.
# DPC_SITE_NAME: replaced by site.name (D-047); workers/cleanup/delete_script.py still reads it.
RETIRED_SECRET_KEYS = {"DPC_SITE_NAME": "retired, the site name is site.name in site.yaml (D-047)"}
PLACEHOLDER = "CHANGE_ME"


@dataclass(frozen=True)
class Secrets:
    """Values from secrets.env. repr never shows a value."""
    values: Dict[str, str] = field(repr=False)
    source_path: str
    warnings: Tuple[str, ...] = ()

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self.values.get(key, default)

    def __getitem__(self, key: str) -> str:
        return self.values[key]

    def __repr__(self) -> str:
        return f"Secrets(source_path={self.source_path!r}, keys={sorted(self.values)})"


def required_secret_keys(config: SiteConfig) -> List[str]:
    """Which keys this site needs, from what site.yaml says (CONTRACTS 9)."""
    keys = ["DPC_DB_USER", "DPC_DB_PASS"]
    if config.mail.sender == "graph":
        keys += ["DPC_MS_TENANT_ID", "DPC_MS_CLIENT_ID", "DPC_MS_CLIENT_SECRET", "DPC_MAIL_SENDER"]
    elif config.mail.sender == "smtp":
        keys += ["DPC_MAIL_SENDER", "DPC_SMTP_PASSWORD"]
    return keys


def _check_file_is_private(path: str) -> List[str]:
    """secrets.env must belong to the service user and be readable by nobody else (chmod 600)."""
    info = os.stat(path)
    problems = []
    if not stat.S_ISREG(info.st_mode):
        problems.append("must be a regular file")
    if info.st_mode & 0o077:
        problems.append(f"permissions are {stat.S_IMODE(info.st_mode):o}; run: chmod 600 {path}")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        problems.append("is owned by another user; it must belong to the user running the services")
    return problems


def _parse_env_file(path: str, problems: List[str]) -> Dict[str, str]:
    """KEY=VALUE per line, # comments, optional matching quotes (systemd EnvironmentFile style).
    Problems name the line and key, never the value."""
    values: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as f:
        for number, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                problems.append(f"line {number}: expected KEY=VALUE")
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key in values:
                problems.append(f"line {number}: {key} is set more than once")
            values[key] = value
    return values


def load_secrets(config: SiteConfig, path: Optional[str] = None) -> Secrets:
    """Read <root>/config/secrets.env (or `path`). Secrets come only from this file, never
    from site.yaml. Raises SiteConfigError if the file is missing, not chmod 600, or lacks a
    key this site needs."""
    path = os.path.abspath(os.path.expanduser(path)) if path else config.paths.secrets_file
    if not os.path.isfile(path):
        raise SiteConfigError(path, ["file not found (copy config/examples/secrets.example.env, chmod 600)"])
    problems = _check_file_is_private(path)
    if problems:   # refuse to read a file others can read
        raise SiteConfigError(path, problems)

    values = _parse_env_file(path, problems)
    warnings = []
    for key in values:
        if key in RETIRED_SECRET_KEYS:
            warnings.append(f"{key}: {RETIRED_SECRET_KEYS[key]}")
        elif key not in SECRET_KEYS:
            close = difflib.get_close_matches(key, SECRET_KEYS, n=1)
            problems.append(f"{key}: unknown key" + (f" (did you mean '{close[0]}'?)" if close else ""))

    for key in required_secret_keys(config):
        if not values.get(key):
            problems.append(f"{key}: missing")
    for key, value in values.items():
        if value == PLACEHOLDER:
            problems.append(f"{key}: still the placeholder {PLACEHOLDER}")
    if bool(values.get("DPC_MQTT_USER")) != bool(values.get("DPC_MQTT_PASS")):
        problems.append("DPC_MQTT_USER and DPC_MQTT_PASS: set both or neither")

    if problems:
        raise SiteConfigError(path, problems)
    return Secrets(values=values, source_path=path, warnings=tuple(warnings))


# ======================================================================
# Command line check
# ======================================================================

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m common.config.site_config",
                                     description="Check a DPC site.yaml (schema v1).")
    parser.add_argument("path", nargs="?", help=f"site.yaml (default: ${CONFIG_ENV_VAR} or {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--with-secrets", action="store_true",
                        help="also check <root>/config/secrets.env (permissions and required keys)")
    args = parser.parse_args(argv)
    try:
        config = load_site_config(args.path)
        if args.with_secrets:
            for warning in load_secrets(config).warnings:
                print(f"WARNING {warning}")
    except SiteConfigError as exc:
        print(exc)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
