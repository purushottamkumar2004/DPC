"""Settings of the cleanup worker (STATUS P9), read ONLY from its rendered JSON.

File: env DPC_WORKER_CONFIG = <root>/config/rendered/cleanup.json (CONTRACTS 13, D-062).

Until CONFIG renders cleanup.json (STATUS F29), the program still runs:
- a missing file / env var: root = the folder three levels above this file
  (<root>/code/workers/cleanup, CONTRACTS 4.1); nothing site-specific is hardcoded;
- every missing key gets a built-in default equal to the Tadipatri value, with ONE warning
  per key (same approach as inspection's F22). The defaults live only in DEFAULTS below;
- without `graph` and mail addresses the disk mail is off (one warning).
A key that is present but has the wrong type stops the program with the key's name.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

WORKER_ID = "cleanup"
CODE_DIR = os.path.dirname(os.path.abspath(__file__))

# Tadipatri values (docs/sites/tadipatri.site.reference.yaml, `cleanup` section).
DEFAULTS = {
    "cleanup.raw_image_hours": 20,
    "cleanup.interval_seconds": 10,
    "cleanup.repeated.days": 20,
    "cleanup.repeated.items": 200,
    "cleanup.plate_days": 10,
    "cleanup.defective.days": 10,
    "cleanup.defective.items": 200,
    "cleanup.disk_percent.delete": 75,
    "cleanup.disk_percent.raw_delete": 85,
    "cleanup.disk_percent.mail": 90,
    "cleanup.disk_percent.inference_delete": 95,
    "cleanup.disk_report_time": "09:00",      # was DAILY_REPORT_HOUR/MINUTE in delete_script.py
    "log.backup_count": 5,
    "site.name": "DPC site",
}
# Fallback camera list (CONTRACTS 4.1: CAM1 -> LEFT, CAM2 -> RIGHT).
DEFAULT_CAMERAS = [{"id": "CAM1", "side": "LEFT"}, {"id": "CAM2", "side": "RIGHT"}]

# Folder groups and how each is derived from the layout (CONTRACTS 4.1).
FRAME_GROUPS = {"raw_images": "RAW_IMAGES", "inf": "INF"}                  # data/frames/<CAM>/<x>
IMAGE_GROUPS = {"repeated": "REPEATED", "plate": "PLATE", "defective": "DEFECTIVE",
                "defect_raw": "RAW", "magnet": "MAGNET"}                   # data/images/<x>/<SIDE>
SIDE_GROUPS = {"bolt_data": "bolt_data"}                                   # data/<x>/<SIDE>
FOLDER_GROUPS = list(FRAME_GROUPS) + list(IMAGE_GROUPS) + list(SIDE_GROUPS)

# Folders owned by the capture (hard links, rule 12): never deleted from.
FORBIDDEN_NAMES = {"live", "_tmp", "UI", "ui_live"}


class ConfigError(Exception):
    pass


@dataclass
class KeepLimit:
    days: int
    items: int


@dataclass
class DiskPercent:
    delete: float
    raw_delete: float
    mail: float
    inference_delete: float


@dataclass
class MailSettings:
    enabled: bool
    sender_address: str = ""
    to: List[str] = field(default_factory=list)
    cc: List[str] = field(default_factory=list)
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""

    def secrets(self):
        return [self.client_secret, self.client_id, self.tenant_id]


@dataclass
class Settings:
    worker_id: str
    root: str
    logs: str
    data: str
    site_name: str
    backup_count: int
    raw_image_hours: int
    interval_seconds: int
    repeated: KeepLimit
    plate_days: int
    defective: KeepLimit
    disk_percent: DiskPercent
    report_hour: int
    report_minute: int
    folders: Dict[str, List[str]]
    mail: MailSettings
    warnings: List[str] = field(default_factory=list)


class _Reader:
    """Reads dotted keys from the JSON; collects one warning per key that falls back."""

    def __init__(self, data: dict):
        self.data = data
        self.warnings: List[str] = []

    def raw(self, key: str):
        node = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def get(self, key: str, kind):
        value = self.raw(key)
        if value is None:
            value = DEFAULTS[key]
            self.warnings.append(f"cleanup.json: '{key}' missing, using default {value!r}")
        if kind is int and isinstance(value, bool):
            raise ConfigError(f"'{key}' must be an integer")
        if kind in (int, float) and not isinstance(value, (int, float)):
            raise ConfigError(f"'{key}' must be a number")
        if kind is str and not isinstance(value, str):
            raise ConfigError(f"'{key}' must be text")
        return kind(value)


def default_folders(root: str, cameras) -> Dict[str, List[str]]:
    """Every folder cleanup works on, from the layout (CONTRACTS 4.1)."""
    data = os.path.join(root, "data")
    folders = {}
    for group, name in FRAME_GROUPS.items():
        folders[group] = [os.path.join(data, "frames", c["id"], name) for c in cameras]
    for group, name in IMAGE_GROUPS.items():
        folders[group] = [os.path.join(data, "images", name, c["side"]) for c in cameras]
    for group, name in SIDE_GROUPS.items():
        folders[group] = [os.path.join(data, name, c["side"]) for c in cameras]
    return folders


def _parse_time(text: str):
    try:
        hour, minute = text.split(":")
        hour, minute = int(hour), int(minute)
    except ValueError:
        raise ConfigError(f"'cleanup.disk_report_time' must be HH:MM, got {text!r}")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(f"'cleanup.disk_report_time' must be HH:MM, got {text!r}")
    return hour, minute


def _folders(r: _Reader, root: str) -> Dict[str, List[str]]:
    cameras = r.raw("cameras")
    if cameras is None:
        cameras = DEFAULT_CAMERAS
        r.warnings.append("cleanup.json: 'cameras' missing, using CAM1=LEFT, CAM2=RIGHT")
    derived = default_folders(root, cameras)
    folders = {}
    for group in FOLDER_GROUPS:
        listed = r.raw(f"paths.{group}")
        if listed is None:
            r.warnings.append(f"cleanup.json: 'paths.{group}' missing, derived from the layout")
            listed = derived[group]
        if not isinstance(listed, list) or not all(isinstance(p, str) for p in listed):
            raise ConfigError(f"'paths.{group}' must be a list of folders")
        folders[group] = [os.path.abspath(os.path.expanduser(p)) for p in listed]
    return folders


def _mail(r: _Reader) -> MailSettings:
    graph = r.raw("graph")
    mail = r.raw("mail")
    if not isinstance(graph, dict) or not isinstance(mail, dict):
        r.warnings.append("cleanup.json: 'graph' or 'mail' missing: disk mail is OFF")
        return MailSettings(enabled=False)
    to, cc = mail.get("to") or [], mail.get("cc") or []
    if not isinstance(to, list) or not isinstance(cc, list):
        raise ConfigError("'mail.to' and 'mail.cc' must be lists of addresses")
    needed = [graph.get("tenant_id"), graph.get("client_id"), graph.get("client_secret"),
              mail.get("sender_address")]
    if not all(isinstance(v, str) and v for v in needed) or not to:
        r.warnings.append("cleanup.json: graph login, 'mail.sender_address' or 'mail.to' empty: disk mail is OFF")
        return MailSettings(enabled=False)
    return MailSettings(enabled=True, sender_address=mail["sender_address"], to=list(to), cc=list(cc),
                        tenant_id=graph["tenant_id"], client_id=graph["client_id"],
                        client_secret=graph["client_secret"])


def check_folders(data_dir: str, folders: Dict[str, List[str]]) -> None:
    """Safety (rule 12, P9 item 7): every folder lies inside <root>/data and is never a
    capture-owned folder (live, _tmp, UI). Raises ConfigError otherwise."""
    data_real = os.path.realpath(data_dir)
    for group, paths in folders.items():
        for path in paths:
            real = os.path.realpath(path)
            if not real.startswith(data_real + os.sep):
                raise ConfigError(f"'paths.{group}': {path} is not inside {data_dir}")
            parts = set(os.path.relpath(real, data_real).split(os.sep))
            if parts & FORBIDDEN_NAMES:
                raise ConfigError(f"'paths.{group}': {path} is a capture folder (live/_tmp/UI), never cleaned")


def root_from_code_location() -> str:
    """<root>/code/workers/cleanup -> <root> (CONTRACTS 4.1)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(CODE_DIR)))


def config_path_from_env() -> Optional[str]:
    return os.environ.get("DPC_WORKER_CONFIG") or None


def load_settings(path: Optional[str]) -> Settings:
    warnings = []
    if path is None:
        data = {}
        warnings.append("DPC_WORKER_CONFIG not set (cleanup.json not rendered yet): root taken "
                        "from the code location, Tadipatri defaults used")
    else:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise ConfigError(f"cannot read {path}: {type(e).__name__}")
        if not isinstance(data, dict):
            raise ConfigError(f"{path} must hold a JSON object")

    r = _Reader(data)
    root = r.raw("paths.root")
    if root is None:
        root = root_from_code_location()
        r.warnings.append(f"cleanup.json: 'paths.root' missing, using the code location {root}")
    if not isinstance(root, str):
        raise ConfigError("'paths.root' must be a folder")
    root = os.path.abspath(os.path.expanduser(root))
    logs = r.raw("paths.logs") or os.path.join(root, "logs")
    data_dir = r.raw("paths.data") or os.path.join(root, "data")

    hour, minute = _parse_time(r.get("cleanup.disk_report_time", str))
    disk = DiskPercent(delete=r.get("cleanup.disk_percent.delete", float),
                       raw_delete=r.get("cleanup.disk_percent.raw_delete", float),
                       mail=r.get("cleanup.disk_percent.mail", float),
                       inference_delete=r.get("cleanup.disk_percent.inference_delete", float))
    folders = _folders(r, root)
    settings = Settings(
        worker_id=data.get("worker_id") or WORKER_ID,
        root=root, logs=logs, data=data_dir,
        site_name=r.get("site.name", str),
        backup_count=r.get("log.backup_count", int),
        raw_image_hours=r.get("cleanup.raw_image_hours", int),
        interval_seconds=r.get("cleanup.interval_seconds", int),
        repeated=KeepLimit(r.get("cleanup.repeated.days", int), r.get("cleanup.repeated.items", int)),
        plate_days=r.get("cleanup.plate_days", int),
        defective=KeepLimit(r.get("cleanup.defective.days", int), r.get("cleanup.defective.items", int)),
        disk_percent=disk, report_hour=hour, report_minute=minute,
        folders=folders, mail=_mail(r),
    )
    check_folders(data_dir, folders)
    settings.warnings = warnings + r.warnings
    return settings
