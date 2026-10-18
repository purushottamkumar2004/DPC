"""Load and validate the site config `site.yaml` (schema v2, CONTRACTS sections 7 and 9,
D-047, D-048, D-049). Since v2 the file also holds the site's secrets.

Public API
    load_site_config(path=None) -> SiteConfig
    SiteConfigError            (.problems = list of "key.path: what is wrong")
    redacted_copy(path=None) -> str      the file text with every secret replaced by ***
    legacy_env_vars(config) -> dict      DPC_* env vars for legacy workers (CONTRACTS 9 env bridge)

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

SCHEMA_VERSION = 2
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
    user: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int
    user: Optional[str] = field(repr=False)        # None when the broker has no login
    password: Optional[str] = field(repr=False)


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
    password: str = field(repr=False)


@dataclass(frozen=True)
class GraphSettings:
    tenant_id: str = field(repr=False)
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)


@dataclass(frozen=True)
class MailConfig:
    sender: str          # graph | smtp
    project_name: str
    report_dir: str      # absolute
    recipients: Tuple[str, ...]
    cc: Tuple[str, ...]
    sender_address: str
    disk_report_to: Tuple[str, ...]
    graph: Optional[GraphSettings]   # set when sender = graph
    smtp: Optional[SmtpSettings]     # set when sender = smtp


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
        user=s.string("user", secret=True),
        password=s.string("password", secret=True),
    )
    if cfg.user == "root":
        s.problem("user", "must not be root; use a dedicated user with limited rights (D-030)")
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
    cfg = MailConfig(
        sender=sender,
        project_name=s.string("project_name"),
        report_dir=_resolve(paths, s.string("report_dir", required=False, default="data/reports")),
        recipients=recipients,
        cc=s.string_list("cc", required=False, default=(), item_pattern=_EMAIL_RE, item_hint=email_hint),
        sender_address=s.string("sender_address", pattern=_EMAIL_RE, pattern_hint=email_hint),
        disk_report_to=s.string_list("disk_report_to", required=False, default=recipients,
                                     item_pattern=_EMAIL_RE, item_hint=email_hint),
        graph=_read_graph(s, sender),
        smtp=_read_smtp(s, sender),
    )
    s.finish()
    return cfg


def _sender_block(s: _Section, name: str, sender: Optional[str]) -> Optional[_Section]:
    """The `graph` / `smtp` block: required when `sender` names it, not allowed otherwise."""
    if sender == name:
        return s.section(name)
    s.used.add(name)
    if sender is not None and s.present(name):
        s.problem(name, f"only allowed when sender is {name}")
    return None


def _read_graph(s: _Section, sender: Optional[str]) -> Optional[GraphSettings]:
    g = _sender_block(s, "graph", sender)
    if g is None:
        return None
    graph = GraphSettings(tenant_id=g.string("tenant_id", secret=True),
                          client_id=g.string("client_id", secret=True),
                          client_secret=g.string("client_secret", secret=True))
    g.finish()
    return graph


def _read_smtp(s: _Section, sender: Optional[str]) -> Optional[SmtpSettings]:
    sm = _sender_block(s, "smtp", sender)
    if sm is None:
        return None
    # host and port defaults are the values hardcoded in today's SMTP sender
    smtp = SmtpSettings(host=sm.string("host", required=False, default="smtp.gmail.com"),
                        port=sm.integer("port", required=False, default=587, low=1, high=65535),
                        password=sm.string("password", secret=True))
    sm.finish()
    return smtp


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
    data = _read_yaml(path)
    if isinstance(data, dict) and data.get("schema_version") == 1:
        raise SiteConfigError(path, [_v1_upgrade_message(data)])

    problems: List[str] = []
    top = _Section(data, "", problems)
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


def _v1_upgrade_message(data: Dict[str, Any]) -> str:
    """A v1 file lacks the secrets (they were in secrets.env). Say exactly what to add."""
    def block(*keys):
        node = data
        for key in keys:
            node = node.get(key) if isinstance(node, dict) else None
        return node if isinstance(node, dict) else {}

    db, mail = block("db"), block("mail")
    to_add = [key for key, present in (("db.user", db.get("user")), ("db.password", db.get("password")),
                                       ("mail.sender_address", mail.get("sender_address"))) if not present]
    if mail.get("sender") == "smtp":
        if not block("mail", "smtp").get("password"):
            to_add.append("mail.smtp.password")
    elif not block("mail", "graph"):
        to_add.append("mail.graph (tenant_id, client_id, client_secret)")
    what = f"add {', '.join(to_add)} (the values from the old secrets.env), then " if to_add else ""
    return f"schema_version: this is a v1 file; {what}set schema_version: 2 (CONTRACTS 7, D-048)"


# ======================================================================
# Public: redacted copy (safe to paste into a chat, mirror or UI)
# ======================================================================

# Secret keys from CONTRACTS 7 and 9, as key paths.
SECRET_KEY_PATHS = {
    ("db", "user"), ("db", "password"),
    ("mqtt", "user"), ("mqtt", "password"),
    ("mail", "graph", "tenant_id"), ("mail", "graph", "client_id"), ("mail", "graph", "client_secret"),
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
    if mail.graph is not None:
        env.update({"DPC_MS_TENANT_ID": mail.graph.tenant_id, "DPC_MS_CLIENT_ID": mail.graph.client_id,
                    "DPC_MS_CLIENT_SECRET": mail.graph.client_secret})
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
                                     description="Check a DPC site.yaml (schema v2).")
    parser.add_argument("path", nargs="?", help=f"site.yaml (default: ${CONFIG_ENV_VAR} or {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--redacted", action="store_true",
                        help="print the file with every secret replaced by *** (safe to share); no check")
    args = parser.parse_args(argv)
    try:
        if args.redacted:
            print(redacted_copy(args.path), end="")
            return 0
        load_site_config(args.path)
    except SiteConfigError as exc:
        print(exc)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
