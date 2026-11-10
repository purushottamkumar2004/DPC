"""Read-only access to the rendered worker settings <config>/rendered/<worker_id>.json (CONTRACTS 13).

Those files hold passwords: callers take only the keys they need (checks, camera settings,
model folder, log file name, writes_live_wheel_counter) and never pass a whole file to a page.
"""
import json
import os
import threading

_cache = {}
_lock = threading.Lock()


def rendered(cfg, worker_id: str) -> dict:
    """The worker's rendered JSON ({} when missing or unreadable), re-read when the file changes."""
    path = os.path.join(cfg.paths["config"], "rendered", f"{worker_id}.json")
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (st.st_mtime_ns, st.st_size)
    with _lock:
        hit = _cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    with _lock:
        _cache[path] = (key, data)
    return data


CHECKS = (("bucket_clip", "Bolt check"), ("skidding", "Skidding"), ("wheel_missing", "Wheel missing"),
          ("cycle_restart", "Cycle restart"))


def checks(cfg, inspection_worker: str):
    """[(key, label, True/False/None)] from inspection_<side>.json `checks` (None = not known)."""
    values = rendered(cfg, inspection_worker).get("checks")
    values = values if isinstance(values, dict) else {}
    return [(key, label, (bool(values[key]) if key in values else None)) for key, label in CHECKS]


def camera_settings(cfg, capture_worker: str) -> dict:
    cam = rendered(cfg, capture_worker).get("camera")
    cam = cam if isinstance(cam, dict) else {}
    return {k: cam.get(k) for k in ("id", "side", "driver", "serial", "fps", "exposure_us", "rotate")}


def model_folder(cfg, inspection_worker: str):
    """(name, models_dir) of this side's model, or (None, None)."""
    model = rendered(cfg, inspection_worker).get("model")
    model = model if isinstance(model, dict) else {}
    return model.get("name"), model.get("models_dir")


def wheel_writer_side(cfg):
    """The side whose inspection writes wheel_counter_table (writes_live_wheel_counter), or None."""
    for side in cfg["sides"]:
        if rendered(cfg, side["inspection_worker"]).get("writes_live_wheel_counter"):
            return side["side"]
    return None
