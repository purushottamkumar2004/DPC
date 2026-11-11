"""
Setup mode (S1; D-103 phases 0-2).

A fresh machine has the code, the boot service and the UI, but no site.yaml yet: the person sets
the site up in the UI wizard. Until then the Supervisor runs ONLY the `ui` worker, from the
built-in list below (no workers.yaml needed). `supervisor.py setup-finish`, or the UI writing
<root>/run/setup_finish, validates site.yaml + workers.yaml and switches to normal mode, which
starts every program.

The mode is decided at every Supervisor start (a reboot keeps it):
    <root>/config/SETUP_MODE present          -> setup   (setup not finished; only setup-finish removes it)
    <root>/config/site.yaml missing           -> setup   (fresh machine; the marker is written)
    <root>/config/workers.yaml missing/broken -> setup   (nothing to supervise yet; no marker)
    site.yaml invalid, no marker              -> normal  (a finished site with a broken site.yaml keeps
                                                          inspecting with the last rendered files and an
                                                          error, as before S1: a typo must not stop production
                                                          after a reboot)
    otherwise                                 -> normal
A failed setup-finish writes the marker, so the site stays in setup mode across reboots too.

Files (<root>/run, site user only, no values): setup_finish (request; empty or {"id": ...}),
setup_finish.result.json ({id, ok, mode, problems}), mode.json ({mode, reason, since}).
"""

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional, Tuple

from core.config import ConfigError, ConfigurationManager
from core.site_env import SiteEnv, SiteEnvError

SETUP_MARKER = "SETUP_MODE"
REQUEST_FILE = "setup_finish"
RESULT_FILE = "setup_finish.result.json"
MODE_FILE = "mode.json"


@dataclass
class Paths:
    root: str
    config_dir: str
    site_yaml: str
    workers_file: str
    run_dir: str

    @classmethod
    def from_site_env(cls, site_env: SiteEnv, workers_file: Optional[str] = None) -> "Paths":
        """<root> is the folder above <root>/config/site.yaml (CONTRACTS 4.1)."""
        site_yaml = site_env.site_yaml_path() or os.path.expanduser("~/INSIGHTZZ/config/site.yaml")
        config_dir = os.path.dirname(site_yaml)
        root = os.path.dirname(config_dir)
        return cls(root=root, config_dir=config_dir, site_yaml=site_yaml,
                   workers_file=os.path.abspath(workers_file) if workers_file else os.path.join(config_dir, "workers.yaml"),
                   run_dir=os.path.join(root, "run"))

    @property
    def marker(self) -> str:
        return os.path.join(self.config_dir, SETUP_MARKER)


# ---------------------------------------------------------------- the decision
def decide_mode(paths: Paths, site_env: SiteEnv, record: bool = False) -> Tuple[str, str]:
    """("setup" | "normal", reason). record=True (only `run`): write the marker on a fresh machine,
    so the decision survives until setup-finish. Commands that only look never change state."""
    if os.path.exists(paths.marker):
        return "setup", f"{SETUP_MARKER} present (setup not finished)"
    if not os.path.exists(paths.site_yaml):
        if record:
            write_marker(paths)
        return "setup", "no site.yaml yet (fresh machine)"
    problem = workers_file_problem(paths.workers_file)
    if problem:
        return "setup", problem
    return "normal", "site.yaml and workers.yaml present"


def workers_file_problem(workers_file: str) -> Optional[str]:
    if not os.path.isfile(workers_file):
        return f"no workers.yaml at {workers_file}"
    config = ConfigurationManager(workers_file, os.path.dirname(workers_file))
    try:
        config.load()
    except ConfigError as e:
        return f"workers.yaml: {e}"
    return None


def validate_for_normal_mode(paths: Paths, site_env: SiteEnv) -> List[str]:
    """What setup-finish checks; [] = ready. Texts hold no secret (loader messages)."""
    problems = []
    try:
        site_env.load()
    except SiteEnvError as e:
        problems.append(f"site.yaml: {e}")
    problem = workers_file_problem(paths.workers_file)
    if problem:
        problems.append(problem)
    else:
        config = ConfigurationManager(paths.workers_file, os.path.dirname(paths.workers_file))
        config.load()
        if "ui" not in {w.id for w in config.workers}:
            problems.append("workers.yaml has no enabled `ui` worker: the UI would stop after the switch")
    return problems


# ---------------------------------------------------------------- the UI-only workers list
def setup_workers_config(paths: Paths) -> ConfigurationManager:
    """The built-in list for setup mode: only the UI (health `process`, D-097). Its settings
    come from rendered/ui.json when one exists, else the UI's safe defaults."""
    raw = {
        "mqtt": {"enabled": True, "host": "localhost", "port": 1883, "topic_prefix": "dpc"},
        "runtime": {"root": paths.run_dir},
        "log_dir": os.path.join(paths.root, "logs"),
        "workers": [{
            "id": "ui", "type": "ui", "mode": "service", "restart_cost": "normal",
            "command": "python3 run_ui.py", "working_directory": os.path.join(paths.root, "code", "ui"),
            "stop_signal": "SIGTERM", "stop_timeout_seconds": 10,
            "restart": {"max_retries": 1000, "retry_delay_seconds": 5, "reset_after_seconds": 300},
            "health": {"type": "process"},
        }],
    }
    config = ConfigurationManager("<built-in setup-mode list>", paths.root)
    config.load_dict(raw)
    return config


# ---------------------------------------------------------------- files
def write_marker(paths: Paths):
    os.makedirs(paths.config_dir, exist_ok=True)
    if not os.path.exists(paths.marker):
        _write(paths.marker, "setup not finished: run `supervisor.py setup-finish` (or Apply in the UI)\n")


def remove_marker(paths: Paths):
    try:
        os.remove(paths.marker)
    except FileNotFoundError:
        pass


def write_mode_file(run_dir: str, mode: str, reason: str):
    os.makedirs(run_dir, exist_ok=True)
    _write(os.path.join(run_dir, MODE_FILE),
           json.dumps({"mode": mode, "reason": reason, "since": time.strftime("%Y-%m-%dT%H:%M:%S")}))


def read_mode_file(run_dir: str) -> Optional[dict]:
    try:
        with open(os.path.join(run_dir, MODE_FILE)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def take_request(run_dir: str) -> Optional[dict]:
    """The UI's (or the command's) setup_finish request, removed from disk; None when there is none."""
    path = os.path.join(run_dir, REQUEST_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            text = f.read().strip()
        data = json.loads(text) if text else {}
    except (OSError, ValueError):
        data = {}
    try:
        os.remove(path)
    except OSError:
        pass
    request_id = data.get("id") if isinstance(data, dict) else None
    return {"id": request_id if isinstance(request_id, str) else None}


def write_result(run_dir: str, result: dict):
    os.makedirs(run_dir, exist_ok=True)
    _write(os.path.join(run_dir, RESULT_FILE), json.dumps(result, indent=1))


def send_request(paths: Paths) -> str:
    request_id = uuid.uuid4().hex
    os.makedirs(paths.run_dir, exist_ok=True)
    _write(os.path.join(paths.run_dir, REQUEST_FILE), json.dumps({"id": request_id}))
    return request_id


def wait_result(paths: Paths, request_id: str, timeout_s: float) -> Optional[dict]:
    deadline = time.time() + timeout_s
    path = os.path.join(paths.run_dir, RESULT_FILE)
    while time.time() < deadline:
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("id") == request_id:
                return data
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    return None


def _write(path: str, text: str):
    tmp = f"{path}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
