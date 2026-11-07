"""Live state shared by all request threads: the MQTT state cache (one per process) and the
frame-rate meter. run_ui.py creates and starts them; tests install their own with set_*()."""
from monitor.state.cache import StateCache

_cache = None


def get_cache() -> StateCache:
    global _cache
    if _cache is None:              # e.g. manage.py runserver: an empty, unconnected cache
        _cache = StateCache()
    return _cache


def set_cache(cache: StateCache):
    global _cache
    _cache = cache
