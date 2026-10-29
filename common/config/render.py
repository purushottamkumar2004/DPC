"""Render one JSON config per worker from site.yaml (CONTRACTS 13, D-062, D-063).

    from common.config.render import render
    changes = render()                     # [Change(worker_id, "written" | "unchanged" | "removed"), ...]

site.yaml is the only file a person edits. Each worker gets <root>/config/rendered/<worker_id>.json
with only its own settings and only the secrets it uses; every folder is derived from paths.root
and the fixed layout (CONTRACTS 4.1), never typed. The Supervisor calls render() at every start
and on reload, and restarts only the workers whose file changed.

"Changed" means the worker's settings changed. The `_generated` header (site.yaml sha256 and
time) is not compared: editing, say, the mail section must not restart inspection. So a file's
`_generated.sha256` is the site.yaml its settings were last written from.

Rules: a site.yaml error raises SiteConfigError and writes nothing; files are written atomically
(temp file + rename) with mode 600 (they hold the DB login); only files this renderer wrote
(they carry `_generated.renderer`) are ever removed.
"""

import datetime
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from common.config.site_config import SiteConfig, load_site_config, default_config_path

RENDERER_VERSION = 1
WRITTEN, UNCHANGED, REMOVED = "written", "unchanged", "removed"
CAMERA_WORKER = {"CAM1": "frame_capture_cam1", "CAM2": "frame_capture_cam2"}
SIDE_WORKER = {"LEFT": "inspection_left", "RIGHT": "inspection_right"}


@dataclass(frozen=True)
class Change:
    worker_id: str
    status: str            # written | unchanged | removed
    path: str


# ======================================================================
# What each worker gets
# ======================================================================

def _db(cfg: SiteConfig) -> Dict[str, Any]:
    db = cfg.db
    return {"host": db.host, "port": db.port, "name": db.name, "user": db.user, "password": db.password,
            "health_table": db.health_table}


def capture_config(cfg: SiteConfig, camera) -> Dict[str, Any]:
    """frame_capture_<camn>: replaces BASLER_CONFIGn.xml (CONTRACTS 3, 13)."""
    p = cfg.paths
    cam = {"id": camera.id, "side": camera.side, "driver": camera.driver, "serial": camera.serial,
           "fps": camera.fps, "exposure_us": camera.exposure_us, "rotate": camera.rotate, "ip": camera.ip}
    if camera.flir is not None:
        cam["flir"] = {"interface": camera.flir.interface, "throughput_bps": camera.flir.throughput_bps}
    return {
        "worker_id": CAMERA_WORKER[camera.id],
        "paths": {"root": p.root, "logs": p.logs,
                  "live": p.frame_dir(camera.id, "live"), "tmp": p.frame_dir(camera.id, "_tmp"),
                  "inf": p.frame_dir(camera.id, "INF"), "ui": p.frame_dir(camera.id, "UI"),
                  "raw_images": p.frame_dir(camera.id, "RAW_IMAGES")},
        "log": {"backup_count": cfg.capture.log.backup_count, "file_name": camera.log_file_name},
        "camera": cam,
        "health_interval_s": cfg.capture.health_interval_s,     # P4d; the capture reads it at top level
        "db": _db(cfg),
    }


def inspection_config(cfg: SiteConfig, camera) -> Dict[str, Any]:
    """inspection_left / inspection_right (D-063): site-wide values copied into both files,
    this side's object sizes and tuning only, folders derived, no model files or classes
    (they come from the model package that data/models/<name>/current points to, D-060)."""
    p, side, insp = cfg.paths, camera.side, cfg.inspection
    worker_id = SIDE_WORKER[side]
    machine = {"total_wheel_count": insp.machine.total_wheel_count,
               "expected_cycle_total": insp.machine.expected_cycle_total}
    side_cfg = insp.sides[side]
    sizes = side_cfg.object_sizes
    plc = {"ip": cfg.plc.ip}
    if cfg.plc.legacy_db_numbers is not None:
        plc["legacy_db_numbers"] = dict(cfg.plc.legacy_db_numbers)
    return {
        "worker_id": worker_id,
        "side": side,
        "defect_side": camera.defect_side,          # Left / Right (D-063)
        "camera_id": camera.id,
        "writes_live_wheel_counter": insp.live_wheel_counter_side == side,   # one writer (D-073 D3)
        "paths": {"root": p.root, "logs": p.logs,
                  "inf": p.frame_dir(camera.id, "INF"),
                  "raw": p.frame_dir(camera.id, "RAW_IMAGES"),     # frames out of INF, rename (D-031)
                  "defect_raw": p.image_dir("RAW", side),           # raw copies of defect frames (D-072)
                  "defective": p.image_dir("DEFECTIVE", side),
                  "repeated": p.image_dir("REPEATED", side),
                  "plate": p.image_dir("PLATE", side),
                  "temp": p.image_dir("TEMP", side),
                  "magnet": p.image_dir("MAGNET", side),            # magnet diagnostics (D-072)
                  "bolt_data": os.path.join(p.data, "bolt_data", side),
                  "wheel_status_images": os.path.join(p.data, "wheel_status", "IMAGES", side)},   # D-073 D4
        "log": {"backup_count": insp.log.backup_count},
        "model": {"name": insp.model.name, "models_dir": insp.model.root},
        "threshold": insp.threshold,
        "machine": machine,
        "object_sizes": {"wheel_w": sizes.wheel_w, "wheel_h": sizes.wheel_h, "bucket_clip_w": sizes.bucket_clip_w,
                         "chain_w": sizes.chain_w, "half_bucket_clip_w": sizes.half_bucket_clip_w},
        "auto_roi": {"mode": insp.auto_roi.mode, "learn_images": insp.auto_roi.learn_images},
        "wheel_report": {"daily_time": insp.wheel_report.daily_time},
        # this side's tuning (defaults = today's values of this side, D-073 D5)
        "motion": side_cfg.motion,
        "bucket_clip_window": asdict(side_cfg.bucket_clip_window),
        "half_bucket_clip_window": asdict(side_cfg.half_bucket_clip_window),
        "tracker": asdict(side_cfg.tracker),
        "db": _db(cfg),
        # heartbeat transport (D-076); user/password null when the broker has no login
        "mqtt": {"host": cfg.mqtt.host, "port": cfg.mqtt.port, "user": cfg.mqtt.user, "password": cfg.mqtt.password},
        "plc": plc,
    }


def cleanup_config(cfg: SiteConfig) -> Dict[str, Any]:
    """cleanup (F29, D-082): exactly the keys workers/cleanup/cleanup_settings.py reads. Folder lists
    have one entry per configured camera / side, in camera order; all lie inside <root>/data.
    The disk mail is Graph only (D-082 item 7): graph is null unless mail.sender is graph."""
    p, c = cfg.paths, cfg.cleanup
    cams = cfg.cameras
    folders = {
        "raw_images": [p.frame_dir(cam.id, "RAW_IMAGES") for cam in cams],
        "inf": [p.frame_dir(cam.id, "INF") for cam in cams],
        "repeated": [p.image_dir("REPEATED", cam.side) for cam in cams],
        "plate": [p.image_dir("PLATE", cam.side) for cam in cams],
        "defective": [p.image_dir("DEFECTIVE", cam.side) for cam in cams],
        "defect_raw": [p.image_dir("RAW", cam.side) for cam in cams],
        "magnet": [p.image_dir("MAGNET", cam.side) for cam in cams],
        "bolt_data": [os.path.join(p.data, "bolt_data", cam.side) for cam in cams],
    }
    graph = None
    if cfg.graph is not None and cfg.mail.sender == "graph":
        graph = {"tenant_id": cfg.graph.tenant_id, "client_id": cfg.graph.client_id,
                 "client_secret": cfg.graph.client_secret}
    return {
        "worker_id": "cleanup",
        "site": {"id": cfg.site.id, "name": cfg.site.name},
        "cameras": [{"id": cam.id, "side": cam.side} for cam in cams],
        "paths": {"root": p.root, "logs": p.logs, "data": p.data, **folders},
        "log": {"backup_count": c.log.backup_count},
        "cleanup": {"raw_image_hours": c.raw_image_hours, "interval_seconds": c.interval_seconds,
                    "repeated": {"days": c.repeated.days, "items": c.repeated.items},
                    "plate_days": c.plate_days,
                    "defective": {"days": c.defective.days, "items": c.defective.items},
                    "disk_percent": dict(c.disk_percent), "disk_report_time": c.disk_report_time},
        # to: disk_report_to, else recipients (the loader already falls back); cc empty on purpose
        "mail": {"sender_address": cfg.mail.sender_address, "to": list(cfg.mail.disk_report_to), "cc": []},
        "graph": graph,
    }


def worker_configs(cfg: SiteConfig) -> Dict[str, Dict[str, Any]]:
    """worker_id -> settings. Capture and inspection for the configured cameras only (a
    one-camera site gets two of each kind less); cleanup always."""
    configs = {}
    for camera in cfg.cameras:
        configs[CAMERA_WORKER[camera.id]] = capture_config(cfg, camera)
    for camera in cfg.cameras:
        configs[SIDE_WORKER[camera.side]] = inspection_config(cfg, camera)
    configs["cleanup"] = cleanup_config(cfg)
    return configs


# ======================================================================
# Writing
# ======================================================================

def _sha256_of_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _read_rendered(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _settings(data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Everything but the header: what decides whether the worker must restart."""
    return None if data is None else {k: v for k, v in data.items() if k != "_generated"}


def _write_private(path: str, data: Dict[str, Any]) -> None:
    """Atomic, mode 600 from the first byte (the file holds the DB login)."""
    folder = os.path.dirname(path)
    temp = os.path.join(folder, f".{os.path.basename(path)}.tmp-{os.getpid()}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        if os.path.exists(temp):
            os.remove(temp)
        raise


def render(site_yaml: Optional[str] = None, dry_run: bool = False) -> List[Change]:
    """Validate site.yaml and bring <root>/config/rendered/ in line with it. Raises
    SiteConfigError (nothing written) when site.yaml is invalid. dry_run: report only."""
    path = os.path.abspath(os.path.expanduser(site_yaml)) if site_yaml else default_config_path()
    cfg = load_site_config(path)                       # raises before anything is written
    header = {"from": "site.yaml", "sha256": _sha256_of_file(path),
              "at": datetime.datetime.now().isoformat(timespec="seconds"), "renderer": RENDERER_VERSION}
    folder = cfg.paths.rendered
    if not dry_run:
        os.makedirs(folder, mode=0o700, exist_ok=True)

    changes = []
    wanted = worker_configs(cfg)
    for worker_id, settings in wanted.items():
        target = os.path.join(folder, f"{worker_id}.json")
        if _settings(_read_rendered(target)) == settings and os.path.isfile(target):
            changes.append(Change(worker_id, UNCHANGED, target))
            continue
        if not dry_run:
            _write_private(target, {"_generated": header, **settings})
        changes.append(Change(worker_id, WRITTEN, target))

    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        worker_id, ext = os.path.splitext(name)
        target = os.path.join(folder, name)
        if ext != ".json" or worker_id in wanted:
            continue
        data = _read_rendered(target)
        if data and isinstance(data.get("_generated"), dict) and "renderer" in data["_generated"]:
            if not dry_run:
                os.remove(target)                      # only files this renderer wrote
            changes.append(Change(worker_id, REMOVED, target))
    return changes
