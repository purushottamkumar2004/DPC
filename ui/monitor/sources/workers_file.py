"""The list of programs from <root>/config/workers.yaml (read-only; it holds no secrets).

Needed because a disabled worker (e.g. mail at Tadipatri) publishes no status: without the
file the UI could not tell "switched off" from "missing". Re-read when the file changes.
"""
import logging
import os
import threading

log = logging.getLogger("dpc.ui.workers")

_cache = {"key": None, "workers": []}
_lock = threading.Lock()


def configured_workers(path: str):
    """[{id, type, enabled, mode}] in file order; [] when the file is missing or invalid."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (path, st.st_mtime_ns, st.st_size)
    with _lock:
        if _cache["key"] == key:
            return [dict(w) for w in _cache["workers"]]
    workers = []
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for w in data.get("workers") or []:
            if isinstance(w, dict) and w.get("id"):
                workers.append({"id": str(w["id"]), "type": str(w.get("type") or ""),
                                "enabled": w.get("enabled", True) is not False,
                                "mode": w.get("mode") or "service"})
    except Exception as e:
        log.warning("workers file %s not readable: %s", path, e)
        workers = []
    with _lock:
        _cache.update(key=key, workers=workers)
    return [dict(w) for w in workers]
