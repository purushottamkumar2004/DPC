"""Generate the Supervisor's workers.yaml from site.yaml (S2, D-103 Apply).

    from common.config.workers_file import generate_workers, write_workers_file
    data = generate_workers(cfg, mail_enabled=True)        # dict in the shape of
                                                            # supervisor/config/workers.basler-2cam.yaml
    write_workers_file(cfg, path=None, mail_enabled=True)  # <root>/config/workers.yaml, atomic

What a site gets (only what site.yaml says it has):
  - one capture per camera, by its driver: basler -> workers/frame_capture_basler/FrameCapture_Basler.py,
    flir -> workers/frame_capture/FrameCapture.py;
  - one inspection per side, following that side's camera (D-068);
  - cleanup, plc, ui always; mail always, enabled or disabled (mail_enabled; a site without a
    mail section gets it disabled); transfer only with a transfer section (D-105).
The values (restart, health, timeouts) are the ones of supervisor/config/workers.basler-2cam.yaml,
with every path under the site's paths.root. No secrets: the MQTT login stays in site.yaml (F11).
The result is checked with `supervisor.py check-config -c <file> --site-config <site.yaml>`.
"""

import datetime
import os
from typing import Any, Dict, List, Optional

import yaml

from common.config.site_config import SiteConfig

HEADER = ("# DPC Supervisor workers file, generated from site.yaml by common/config/workers_file.py at {at}.\n"
          "# Programs follow the site's cameras and sections (S2); values as\n"
          "# supervisor/config/workers.basler-2cam.yaml. Regenerate rather than edit; no secrets in here.\n")

CAPTURE_PROGRAM = {   # driver -> (folder under code/workers, command)
    "basler": ("frame_capture_basler", "python3 FrameCapture_Basler.py"),
    "flir": ("frame_capture", "python3 FrameCapture.py"),
}
SIDE_WORKER = {"LEFT": "inspection_left", "RIGHT": "inspection_right"}


def _service(worker_id: str, folder: str, command: str, stop_timeout: int, enabled: bool = True) -> Dict[str, Any]:
    """A service program like cleanup, mail, plc, ui, transfer (folder = its absolute code folder)."""
    entry = {"id": worker_id, "type": worker_id, "mode": "service", "restart_cost": "normal",
             "command": command, "working_directory": folder,
             "stop_signal": "SIGTERM", "stop_timeout_seconds": stop_timeout, "health": {"type": "process"}}
    if not enabled:
        entry["enabled"] = False
    return entry


def _captures(cfg: SiteConfig) -> List[Dict[str, Any]]:
    entries = []
    for cam in cfg.cameras:
        folder, command = CAPTURE_PROGRAM[cam.driver]
        entries.append({
            "id": f"frame_capture_{cam.id.lower()}", "type": "frame_capture", "command": command,
            "working_directory": os.path.join(cfg.paths.code, "workers", folder),
            "stop_signal": "SIGTERM", "stop_timeout_seconds": 15,
            "restart": {"max_retries": 30, "retry_delay_seconds": 2, "reset_after_seconds": 300},
            "health": {"type": "file_activity", "path": os.path.join(cfg.paths.frame_dir(cam.id, "live"), "IMG_1.jpg"),
                       "max_age_seconds": 60, "startup_grace_seconds": 30},
        })
    return entries


def _inspections(cfg: SiteConfig) -> List[Dict[str, Any]]:
    entries = []
    for cam in cfg.cameras:
        entries.append({
            "id": SIDE_WORKER[cam.side], "type": "inspection", "follows": f"frame_capture_{cam.id.lower()}",
            "restart_cost": "high", "command": "python3 inspection.py",
            "working_directory": os.path.join(cfg.paths.code, "workers", "inspection"),
            "stop_timeout_seconds": 20,
            "health": {"type": "queue_lag", "path": cfg.paths.frame_dir(cam.id, "INF"),
                       "max_oldest_age_seconds": 120, "require_heartbeat": False, "max_age_seconds": 60,
                       "startup_grace_seconds": 180},
        })
    return entries


def generate_workers(cfg: SiteConfig, mail_enabled: bool = True) -> Dict[str, Any]:
    """The workers.yaml content for this site (a dict, in file order)."""
    code = os.path.join(cfg.paths.code, "workers")
    workers = _captures(cfg) + _inspections(cfg)
    workers.append(_service("cleanup", os.path.join(code, "cleanup"), "python3 cleanup.py", 15))
    workers.append(_service("mail", os.path.join(code, "mail"), "python3 mail_worker.py", 15,
                            enabled=mail_enabled and cfg.mail is not None))
    workers.append(_service("plc", os.path.join(code, "plc"), "python3 plc_worker.py", 20))
    workers.append(_service("ui", os.path.join(cfg.paths.code, "ui"), "python3 run_ui.py", 10))
    if cfg.transfer is not None:
        workers.append(_service("transfer", os.path.join(code, "transfer"), "python3 transfer.py", 30))
    return {
        "mqtt": {"enabled": True, "host": cfg.mqtt.host, "port": cfg.mqtt.port, "topic_prefix": "dpc"},
        "monitoring": {"check_interval_seconds": 2, "worker_timeout_seconds": 10, "high_cost_timeout_seconds": 120,
                       "startup_stagger_seconds": 1, "start_after_timeout_seconds": 120,
                       "status_publish_interval_seconds": 30},
        "models": {"auto_update": "off", "check_interval_hours": 6},
        "restart_policy": {"max_retries": 3, "retry_delay_seconds": 5, "reset_after_seconds": 300},
        "runtime": {"root": cfg.paths.run},
        "log_dir": cfg.paths.logs,
        "workers": workers,
    }


def workers_yaml_text(cfg: SiteConfig, mail_enabled: bool = True) -> str:
    at = datetime.datetime.now().isoformat(timespec="seconds")
    return HEADER.format(at=at) + yaml.safe_dump(generate_workers(cfg, mail_enabled), sort_keys=False,
                                                 default_flow_style=False)


def write_workers_file(cfg: SiteConfig, path: Optional[str] = None, mail_enabled: bool = True) -> str:
    """Write <root>/config/workers.yaml (or `path`) atomically; returns the path. The previous file
    is kept as workers.yaml.bak. No secrets, so mode 644 like the template."""
    path = os.path.abspath(os.path.expanduser(path)) if path else os.path.join(cfg.paths.config, "workers.yaml")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = f"{path}.new-{os.getpid()}"
    with open(temp, "w", encoding="utf-8") as f:
        f.write(workers_yaml_text(cfg, mail_enabled))
    if os.path.isfile(path):
        os.replace(path, path + ".bak")
    os.replace(temp, path)
    return path
