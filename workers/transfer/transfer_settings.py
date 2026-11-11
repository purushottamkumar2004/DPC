"""Settings of the transfer program, read ONLY from its rendered JSON (CONTRACTS 13, D-062):
env DPC_WORKER_CONFIG = <root>/config/rendered/transfer.json.

Shape asked from CONFIG (handoff, P12):
  worker_id "transfer", site {id, name}, log {backup_count},
  paths {root, logs, run, state, staging, downloads, models},
  cameras [{id, side, serial, fps, exposure_us, rotate, raw_images}],
  sides [{side, camera_id, inspection_worker, plate}],
  models_in_use {model name: [sides]}, machine {total_wheel_count},
  graph {tenant_id, client_id, client_secret}, mqtt {host, port, user, password},
  transfer {drive {site | user}, models_folder,
            upload {chunk_mb, remote_folder, staging_max_gb, max_mbps, plate_images} or null}

Rules (as mail): a folder the layout fixes (CONTRACTS 4.1) and a plain number get a default
with ONE warning each while CONFIG does not render it yet; a secret, the site id, the drive and
the cameras are never defaulted. `<site.id>` inside remote_folder / models_folder is replaced
here too, so a file rendered before the renderer resolves it still works.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

WORKER_ID = "transfer"

# Defaults of the transfer.* numbers (D-104 and this plan; CONFIG renders them).
DEFAULT_CHUNK_MB = 500
DEFAULT_STAGING_MAX_GB = 20.0
DEFAULT_MAX_MBPS = 20.0                    # megabits per second; null in the file = unlimited
DEFAULT_PLATE_IMAGES = 100
DEFAULT_REMOTE_FOLDER = "DPC/data/<site.id>"
DEFAULT_MODELS_FOLDER = "DPC/models/<site.id>"

_MISSING = object()
_REQUIRED = object()


class ConfigError(Exception):
    pass


@dataclass
class Side:
    side: str                              # LEFT | RIGHT
    camera_id: str                         # CAM1 | CAM2
    serial: str
    fps: Optional[float]
    exposure_us: Optional[int]
    rotate: Optional[int]
    raw_images: str                        # the camera's RAW_IMAGES folder
    plate: str                             # data/images/PLATE/<SIDE>
    inspection_worker: str                 # inspection_left | inspection_right


@dataclass
class Upload:
    chunk_mb: int
    remote_folder: str
    staging_max_gb: float
    max_mbps: Optional[float]              # None = unlimited
    plate_images: int


@dataclass
class Settings:
    site_id: str
    site_name: str
    root: str
    logs: str
    run: str
    state: str
    staging: str
    downloads: str
    models: str
    backup_count: int
    sides: List[Side]
    models_in_use: Dict[str, List[str]]
    total_wheel_count: Optional[int]
    graph: Dict[str, str]
    drive: Dict[str, str]                  # {"site": "host:/sites/x"} or {"user": "upn"}
    models_folder: str
    upload: Optional[Upload]               # None = this site uploads nothing
    mqtt: Optional[dict]
    warnings: List[str] = field(default_factory=list)

    def secrets(self) -> List[str]:
        values = list(self.graph.values())
        if self.mqtt and self.mqtt.get("password"):
            values.append(str(self.mqtt["password"]))
        return [v for v in values if v]


def read_raw_config(path: Optional[str] = None) -> dict:
    """The rendered JSON as a dict; {} when the env var or the file is missing."""
    path = path or os.environ.get("DPC_WORKER_CONFIG", "")
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must hold a JSON object")
    return data


def secrets_in(raw: dict) -> List[str]:
    """Secret values from the raw file, so even a config error never shows them."""
    found = []
    for section, keys in (("graph", ("tenant_id", "client_id", "client_secret")), ("mqtt", ("password",))):
        part = raw.get(section)
        if isinstance(part, dict):
            found += [str(part[k]) for k in keys if part.get(k)]
    return found


class _Reader:
    """Dotted keys; collects missing required keys and default warnings."""

    def __init__(self, raw: dict):
        self.raw = raw
        self.missing: List[str] = []
        self.warnings: List[str] = []

    def lookup(self, key: str, base=None):
        value = self.raw if base is None else base
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return _MISSING
            value = value[part]
        return value

    def get(self, key, kind, default=_REQUIRED, nullable=False, base=None, label=None):
        label = label or key
        value = self.lookup(key, base)
        if value is None and nullable:
            return None
        if value is _MISSING or value is None or value == "":
            if default is _REQUIRED:
                self.missing.append(label)
                return None
            self.warnings.append(f"{label} missing in transfer.json, using default {default!r}")
            return default
        if kind is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if not isinstance(value, kind) or isinstance(value, bool):
            raise ConfigError(f"{label} must be {kind.__name__}, got {type(value).__name__}")
        return value


def _drive(r: _Reader) -> Dict[str, str]:
    drive = r.lookup("transfer.drive")
    if drive is _MISSING or drive is None:
        r.missing.append("transfer.drive")
        return {}
    if not isinstance(drive, dict) or len(drive) != 1 or not set(drive) <= {"site", "user"}:
        raise ConfigError('transfer.drive must be {"site": "<host>:/sites/<path>"} or {"user": "<upn>"}')
    key, value = next(iter(drive.items()))
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"transfer.drive.{key} must be a non-empty text")
    if key == "site" and ":/" not in value:
        raise ConfigError('transfer.drive.site must look like "contoso.sharepoint.com:/sites/DPC"')
    return {key: value.strip()}


def _upload(r: _Reader, site_id: str) -> Optional[Upload]:
    block = r.lookup("transfer.upload")
    if block is None or block is _MISSING:
        return None                                       # D-055: absent = download only
    if not isinstance(block, dict):
        raise ConfigError("transfer.upload must be an object or null")
    up = Upload(
        chunk_mb=r.get("chunk_mb", int, DEFAULT_CHUNK_MB, base=block, label="transfer.upload.chunk_mb"),
        remote_folder=r.get("remote_folder", str, DEFAULT_REMOTE_FOLDER, base=block,
                            label="transfer.upload.remote_folder"),
        staging_max_gb=r.get("staging_max_gb", float, DEFAULT_STAGING_MAX_GB, base=block,
                             label="transfer.upload.staging_max_gb"),
        max_mbps=DEFAULT_MAX_MBPS,
        plate_images=r.get("plate_images", int, DEFAULT_PLATE_IMAGES, base=block,
                           label="transfer.upload.plate_images"))
    if "max_mbps" in block:
        up.max_mbps = r.get("max_mbps", float, nullable=True, base=block, label="transfer.upload.max_mbps")
    else:
        r.warnings.append(f"transfer.upload.max_mbps missing in transfer.json, using default {DEFAULT_MAX_MBPS}")
    if up.chunk_mb < 1:
        raise ConfigError("transfer.upload.chunk_mb must be at least 1")
    if up.staging_max_gb <= 0:
        raise ConfigError("transfer.upload.staging_max_gb must be above 0")
    if up.max_mbps is not None and up.max_mbps <= 0:
        raise ConfigError("transfer.upload.max_mbps must be above 0 (null = unlimited)")
    if up.plate_images < 0:
        raise ConfigError("transfer.upload.plate_images must be 0 (off) or more")
    up.remote_folder = _remote(up.remote_folder, site_id, "transfer.upload.remote_folder")
    return up


def _remote(path: str, site_id: str, label: str) -> str:
    path = path.replace("<site.id>", site_id).strip("/")
    if not path or any(part in ("", ".", "..") for part in path.split("/")):
        raise ConfigError(f"{label} must be a folder path like DPC/data/{site_id}")
    return path


def _sides(r: _Reader, root: str) -> List[Side]:
    cameras = r.lookup("cameras")
    sides = r.lookup("sides")
    if not isinstance(cameras, list) or not cameras:
        r.missing.append("cameras")
        return []
    if not isinstance(sides, list) or not sides:
        r.missing.append("sides")
        return []
    by_id = {c.get("id"): c for c in cameras if isinstance(c, dict)}
    result = []
    for entry in sides:
        if not isinstance(entry, dict) or entry.get("side") not in ("LEFT", "RIGHT"):
            raise ConfigError(f"sides: every entry needs side LEFT or RIGHT, got {entry!r}")
        side = entry["side"]
        cam = by_id.get(entry.get("camera_id"))
        if cam is None:
            raise ConfigError(f"sides.{side}: camera_id {entry.get('camera_id')!r} is not in cameras")
        raw = cam.get("raw_images") or os.path.join(root, "data", "frames", cam["id"], "RAW_IMAGES")
        plate = entry.get("plate") or os.path.join(root, "data", "images", "PLATE", side)
        if not cam.get("raw_images"):
            r.warnings.append(f"cameras.{cam['id']}.raw_images missing, using the layout folder {raw}")
        if not entry.get("plate"):
            r.warnings.append(f"sides.{side}.plate missing, using the layout folder {plate}")
        result.append(Side(side=side, camera_id=cam["id"], serial=str(cam.get("serial", "")),
                           fps=cam.get("fps"), exposure_us=cam.get("exposure_us"), rotate=cam.get("rotate"),
                           raw_images=raw, plate=plate,
                           inspection_worker=entry.get("inspection_worker") or f"inspection_{side.lower()}"))
    return result


def build_settings(raw: dict) -> Settings:
    r = _Reader(raw)
    site_id = r.get("site.id", str)
    root = r.get("paths.root", str)
    if root:
        root = os.path.expanduser(root)
    if r.missing:
        raise ConfigError("missing in transfer.json: " + ", ".join(r.missing))

    def folder(key, *layout):
        value = r.lookup(f"paths.{key}")
        if isinstance(value, str) and value:
            return os.path.expanduser(value)
        default = os.path.join(root, *layout)
        r.warnings.append(f"paths.{key} missing in transfer.json, using the layout folder {default}")
        return default

    graph = {k: r.get(f"graph.{k}", str) for k in ("tenant_id", "client_id", "client_secret")}
    models_in_use = r.lookup("models_in_use")
    if models_in_use is _MISSING or not isinstance(models_in_use, dict) or not models_in_use:
        r.missing.append("models_in_use")
        models_in_use = {}
    mqtt = r.lookup("mqtt")
    s = Settings(
        site_id=site_id, site_name=str(r.lookup("site.name") if r.lookup("site.name") is not _MISSING else site_id),
        root=root, logs=folder("logs", "logs"), run=folder("run", "run"),
        state=folder("state", "data", "state", "transfer"),
        staging=folder("staging", "data", "transfer", "staging"),
        downloads=folder("downloads", "data", "transfer", "downloads"),
        models=folder("models", "data", "models"),
        backup_count=r.get("log.backup_count", int, 5),
        sides=_sides(r, root),
        models_in_use={str(k): list(v or []) for k, v in models_in_use.items()},
        total_wheel_count=(r.lookup("machine.total_wheel_count")
                           if isinstance(r.lookup("machine.total_wheel_count"), int) else None),
        graph=graph, drive=_drive(r),
        models_folder=_remote(r.get("transfer.models_folder", str, DEFAULT_MODELS_FOLDER), site_id,
                              "transfer.models_folder"),
        upload=None, mqtt=mqtt if isinstance(mqtt, dict) else None)
    s.upload = _upload(r, site_id)
    if r.missing:
        raise ConfigError("missing in transfer.json: " + ", ".join(r.missing))
    s.warnings = r.warnings
    return s
