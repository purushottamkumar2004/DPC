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

from common.config.model_package import models_in_use
from common.config.site_config import AUTO_ROI_KEYS, SiteConfig, load_site_config, default_config_path

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

def _mqtt(cfg: SiteConfig) -> Dict[str, Any]:
    """The broker login every worker's heartbeat uses (D-034, D-099); user/password null = no login."""
    m = cfg.mqtt
    return {"host": m.host, "port": m.port, "user": m.user, "password": m.password}


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
        "mqtt": _mqtt(cfg),                                      # heartbeat (F61, F70)
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
        "model": {"name": side_cfg.model.name, "models_dir": side_cfg.model.root},   # this side's (D-086)
        "threshold": insp.threshold,
        "machine": machine,
        "object_sizes": {"wheel_w": sizes.wheel_w, "wheel_h": sizes.wheel_h, "bucket_clip_w": sizes.bucket_clip_w,
                         "chain_w": sizes.chain_w, "half_bucket_clip_w": sizes.half_bucket_clip_w},
        # every key settings.py reads, so inspection never falls back to a default (D-093, F53)
        "auto_roi": {"mode": insp.auto_roi.mode,
                     **{key: getattr(insp.auto_roi, key) for key in AUTO_ROI_KEYS}},
        "wheel_report": {"daily_time": insp.wheel_report.daily_time},
        # this side's tuning (defaults = today's values of this side, D-073 D5)
        "motion": side_cfg.motion,
        "bucket_clip_window": asdict(side_cfg.bucket_clip_window),
        "half_bucket_clip_window": asdict(side_cfg.half_bucket_clip_window),
        "tracker": asdict(side_cfg.tracker),
        "checks": asdict(side_cfg.checks),           # D-085: per-side value already applied
        "db": _db(cfg),
        # heartbeat transport (D-076); user/password null when the broker has no login
        "mqtt": _mqtt(cfg),
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
        "mqtt": _mqtt(cfg),                                      # heartbeat (F68, F70)
    }


def mail_config(cfg: SiteConfig) -> Dict[str, Any]:
    """mail (F36, D-083): exactly the keys workers/mail/mail_worker.py reads. Optional values are
    always written (null = off), so the program never falls back to a default with a warning.
    graph only with sender graph, smtp only with sender smtp."""
    p, m = cfg.paths, cfg.mail
    graph = None
    if m.sender == "graph" and cfg.graph is not None:
        graph = {"tenant_id": cfg.graph.tenant_id, "client_id": cfg.graph.client_id,
                 "client_secret": cfg.graph.client_secret}
    smtp = None
    if m.smtp is not None:
        smtp = {"host": m.smtp.host, "port": m.smtp.port, "password": m.smtp.password}
    flood = None
    if m.flood is not None:
        flood = {"max_per_window": m.flood.max_per_window, "window_hours": m.flood.window_hours,
                 "internal_to": list(m.flood.internal_to)}
    db = cfg.db
    return {
        "worker_id": "mail",
        "site": {"id": cfg.site.id, "name": cfg.site.name},
        "paths": {"root": p.root, "logs": p.logs, "state": os.path.join(p.data, "state")},
        "log": {"backup_count": m.log.backup_count},
        "db": {"host": db.host, "port": db.port, "name": db.name, "user": db.user, "password": db.password},
        "mail": {"sender": m.sender, "sender_address": m.sender_address, "project_name": m.project_name,
                 "to": list(m.recipients), "cc": list(m.cc), "smtp": smtp, "report_url": m.report_url,
                 "max_alert_age_hours": m.max_alert_age_hours, "flood": flood},
        "graph": graph,
        "mqtt": _mqtt(cfg),                                      # heartbeat (F69, F70)
    }


def plc_config(cfg: SiteConfig) -> Dict[str, Any]:
    """plc (F39, D-084): the CONTRACTS 13 `plc` key set, all keys always written so
    workers/plc/plc_settings.py never falls back to a default. legacy_db_numbers is left out:
    it is for the legacy algorithm XML only."""
    p, plc, db = cfg.paths, cfg.plc, cfg.db
    return {
        "worker_id": "plc",
        "paths": {"root": p.root, "logs": p.logs, "state": os.path.join(p.data, "state")},
        "log": {"backup_count": plc.log.backup_count},
        "plc": {"type": plc.type, "ip": plc.ip, "port": plc.port, "timeout_s": plc.timeout_s,
                "light_register": plc.light_register, "light_on_value": plc.light_on_value,
                "cleaning_register": plc.cleaning_register,
                "conveyor_status_register": plc.conveyor_status_register,
                "cleaning_pulse_seconds": plc.cleaning_pulse_seconds,
                "light_check_seconds": plc.light_check_seconds,
                "status_interval_seconds": plc.status_interval_seconds,
                "cleaning_cooldown_seconds": plc.cleaning_cooldown_seconds,
                "auto_cleaning": plc.auto_cleaning},
        "mqtt": _mqtt(cfg),
        "db": {"host": db.host, "port": db.port, "name": db.name, "user": db.user, "password": db.password},
    }


MQTT_TOPIC_PREFIX = "dpc"     # every topic starts with dpc/ (CONTRACTS 2)


def ui_config(cfg: SiteConfig) -> Dict[str, Any]:
    """ui (F58, D-095, D-096): exactly the CONTRACTS 13 `ui` key set, every key always written so
    ui/dpc_ui/ui_config.py takes nothing from its defaults. Only the DB and MQTT logins are
    secrets here; the UI never shows them."""
    p, ui, db = cfg.paths, cfg.ui, cfg.db
    return {
        "worker_id": "ui",
        "site": {"id": cfg.site.id, "name": cfg.site.name},
        "paths": {"root": p.root, "code": p.code, "config": p.config, "site_yaml": cfg.source_path,
                  "workers_file": os.path.join(p.config, "workers.yaml"), "logs": p.logs, "run": p.run,
                  "data": p.data, "frames": os.path.join(p.data, "frames"),
                  "state": os.path.join(p.data, "state", "ui")},
        "log": {"backup_count": ui.log.backup_count},
        "ui": {"port": ui.port, "refresh_seconds": ui.refresh_seconds, "event_keep_days": ui.event_keep_days,
               "event_keep_rows": ui.event_keep_rows},
        "db": {"host": db.host, "port": db.port, "name": db.name, "user": db.user, "password": db.password},
        "mqtt": {"host": cfg.mqtt.host, "port": cfg.mqtt.port, "user": cfg.mqtt.user, "password": cfg.mqtt.password,
                 "topic_prefix": MQTT_TOPIC_PREFIX},
        "cameras": [{"id": cam.id, "side": cam.side, "serial": cam.serial, "ip": cam.ip} for cam in cfg.cameras],
        "sides": [{"side": cam.side, "camera_id": cam.id, "inspection_worker": SIDE_WORKER[cam.side]}
                  for cam in cfg.cameras],
        "cleanup": {"disk_percent": dict(cfg.cleanup.disk_percent)},
        "machine": {"total_wheel_count": cfg.inspection.machine.total_wheel_count},
    }


def transfer_config(cfg: SiteConfig) -> Dict[str, Any]:
    """transfer (F81, D-104, D-105): the shape workers/transfer/transfer_settings.py reads, every key
    written so the program takes nothing from its defaults. Only rendered when site.yaml has a
    transfer section. graph is required for it (the loader checks)."""
    p, t = cfg.paths, cfg.transfer
    upload = None
    if t.upload is not None:
        u = t.upload
        upload = {"chunk_mb": u.chunk_mb, "remote_folder": u.remote_folder, "staging_max_gb": u.staging_max_gb,
                  "max_mbps": u.max_mbps, "plate_images": u.plate_images}
    return {
        "worker_id": "transfer",
        "site": {"id": cfg.site.id, "name": cfg.site.name},
        "log": {"backup_count": t.log.backup_count},
        "paths": {"root": p.root, "logs": p.logs, "run": p.run,
                  "state": os.path.join(p.data, "state", "transfer"),
                  "staging": os.path.join(p.data, "transfer", "staging"),
                  "downloads": os.path.join(p.data, "transfer", "downloads"),
                  "models": os.path.join(p.data, "models")},
        "cameras": [{"id": cam.id, "side": cam.side, "serial": cam.serial, "fps": cam.fps,
                     "exposure_us": cam.exposure_us, "rotate": cam.rotate,
                     "raw_images": p.frame_dir(cam.id, "RAW_IMAGES")} for cam in cfg.cameras],
        "sides": [{"side": cam.side, "camera_id": cam.id, "inspection_worker": SIDE_WORKER[cam.side],
                   "plate": p.image_dir("PLATE", cam.side)} for cam in cfg.cameras],
        "models_in_use": models_in_use(cfg),
        "machine": {"total_wheel_count": cfg.inspection.machine.total_wheel_count},
        "graph": {"tenant_id": cfg.graph.tenant_id, "client_id": cfg.graph.client_id,
                  "client_secret": cfg.graph.client_secret},
        "mqtt": _mqtt(cfg),
        "transfer": {"drive": dict(t.drive), "models_folder": t.models_folder, "upload": upload},
    }


def worker_configs(cfg: SiteConfig) -> Dict[str, Dict[str, Any]]:
    """worker_id -> settings. Capture and inspection for the configured cameras only (a
    one-camera site gets two of each kind less); cleanup, mail, plc and ui always; transfer when configured."""
    configs = {}
    for camera in cfg.cameras:
        configs[CAMERA_WORKER[camera.id]] = capture_config(cfg, camera)
    for camera in cfg.cameras:
        configs[SIDE_WORKER[camera.side]] = inspection_config(cfg, camera)
    configs["cleanup"] = cleanup_config(cfg)
    configs["mail"] = mail_config(cfg)
    configs["plc"] = plc_config(cfg)
    configs["ui"] = ui_config(cfg)
    if cfg.transfer is not None:                 # only on a site with a transfer section
        configs["transfer"] = transfer_config(cfg)
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
