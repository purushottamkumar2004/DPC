"""Settings of the UI worker, read only from its rendered JSON (D-062, STATUS F58).

The Supervisor sets DPC_WORKER_CONFIG = <root>/config/rendered/ui.json. Until CONFIG renders
that file (F58), or when a key is missing, a safe default is used and ONE WARNING per missing
key is logged at start. Nothing site-specific is hardcoded here: every default is either
derived from `paths.root` or is a neutral value (empty DB name = "database not configured").

The rest of the UI asks `get_config()` for values; tests replace it with `set_config()`.
"""
import copy
import json
import logging
import os

log = logging.getLogger("dpc.ui.config")

DEFAULT_ROOT = "~/INSIGHTZZ"          # CONTRACTS 4.1 default; the real value comes from ui.json

# Top-level defaults. `paths` and `cameras`/`sides` are completed in _derive() from paths.root.
DEFAULTS = {
    "worker_id": "ui",
    "site": {"id": "", "name": "DPC"},
    "log": {"backup_count": 5},
    "ui": {"port": 8080, "refresh_seconds": 2, "event_keep_days": 90, "event_keep_rows": 50000},
    "db": {"host": "localhost", "port": 3306, "name": "", "user": "", "password": ""},
    "mqtt": {"host": "localhost", "port": 1883, "user": None, "password": None, "topic_prefix": "dpc"},
    "cameras": [{"id": "CAM1", "side": "LEFT", "serial": "", "ip": None},
                {"id": "CAM2", "side": "RIGHT", "serial": "", "ip": None}],
    "cleanup": {"disk_percent": {"delete": 75, "raw_delete": 85, "mail": 90, "inference_delete": 95}},
    "machine": {"total_wheel_count": None},
}

PATH_KEYS = ("root", "code", "config", "site_yaml", "workers_file", "logs", "run", "data", "frames", "state")


class UiConfig(dict):
    """The merged settings (a plain dict) plus a few helpers used everywhere."""

    @property
    def paths(self) -> dict:
        return self["paths"]

    def camera_for_side(self, side: str):
        for cam in self["cameras"]:
            if cam["side"] == side:
                return cam
        return None

    def secret_values(self):
        """Values that must never appear on a page (masked in logs and tracebacks too)."""
        values = [self["db"].get("password"), self["mqtt"].get("password")]
        return [v for v in values if isinstance(v, str) and len(v) >= 4]


def _merge(defaults, loaded, prefix, missing):
    """Recursively fill `loaded` from `defaults`; record every missing key path."""
    if not isinstance(loaded, dict):
        missing.append(prefix.rstrip("."))
        return copy.deepcopy(defaults)
    out = dict(loaded)
    for key, default in defaults.items():
        if key not in loaded:
            missing.append(prefix + key)
            out[key] = copy.deepcopy(default)
        elif isinstance(default, dict):
            out[key] = _merge(default, loaded[key], prefix + key + ".", missing)
    return out


def _derive(cfg, loaded_paths, missing):
    """Complete `paths`, `cameras` and `sides`. Every path defaults to <root>/<contract folder>."""
    paths = dict(loaded_paths) if isinstance(loaded_paths, dict) else {}
    root = os.path.expanduser(paths.get("root") or DEFAULT_ROOT)
    derived = {
        "root": root,
        "code": os.path.join(root, "code"),
        "config": os.path.join(root, "config"),
        "logs": os.path.join(root, "logs"),
        "run": os.path.join(root, "run"),
        "data": os.path.join(root, "data"),
    }
    derived["site_yaml"] = os.path.join(derived["config"], "site.yaml")
    derived["workers_file"] = os.path.join(derived["config"], "workers.yaml")
    derived["frames"] = os.path.join(derived["data"], "frames")
    derived["state"] = os.path.join(derived["data"], "state", "ui")
    for key in PATH_KEYS:
        if paths.get(key):
            paths[key] = os.path.expanduser(paths[key])
        else:
            missing.append("paths." + key)
            paths[key] = derived[key]
    cfg["paths"] = paths

    for cam in cfg["cameras"]:          # each camera: its capture worker (D-033) and its side
        cam.setdefault("ip", None)
        cam.setdefault("serial", "")
        cam.setdefault("worker", "frame_capture_" + cam["id"].lower())
    if "sides" not in cfg or not isinstance(cfg.get("sides"), list):
        missing.append("sides")
        cfg["sides"] = [{"side": cam["side"], "camera_id": cam["id"],
                         "inspection_worker": "inspection_" + cam["side"].lower()}
                        for cam in cfg["cameras"]]
    return cfg


def load(path=None) -> UiConfig:
    """Read ui.json (path or env DPC_WORKER_CONFIG); never raises for a missing or bad file."""
    path = path or os.environ.get("DPC_WORKER_CONFIG") or os.path.join(
        os.path.expanduser(DEFAULT_ROOT), "config", "rendered", "ui.json")
    loaded = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError("top level is not an object")
    except FileNotFoundError:
        log.warning("UI settings file %s not found (not rendered yet, F58): using safe defaults", path)
        loaded = {}
    except (OSError, ValueError) as e:
        log.warning("UI settings file %s unreadable (%s): using safe defaults", path, e)
        loaded = {}

    missing = []
    cfg = _merge(DEFAULTS, loaded, "", missing)
    cfg = _derive(cfg, loaded.get("paths"), missing)
    if loaded:   # a missing file was already reported once; do not list every key as well
        for key in missing:
            log.warning("ui.json has no '%s': using the default", key)
    result = UiConfig(cfg)
    result["_source"] = path
    result["_missing"] = missing
    return result


_config = None


def get_config() -> UiConfig:
    global _config
    if _config is None:
        _config = load()
    return _config


def set_config(cfg: UiConfig):
    """Tests and run_ui.py install a config explicitly."""
    global _config
    _config = cfg
