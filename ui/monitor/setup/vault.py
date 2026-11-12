"""Secrets typed in the wizard, kept ONLY in this process's memory until Apply writes them into
site.yaml (D-103, D-107: never in the draft, never shown again). A UI restart forgets them; the
review then shows them as "not set" and Apply asks again."""
import threading

_lock = threading.Lock()
_values = {}


def put(key, value):
    with _lock:
        if value in (None, ""):
            _values.pop(key, None)
        else:
            _values[key] = str(value)


def get(key):
    with _lock:
        return _values.get(key)


def names():
    with _lock:
        return sorted(_values)


def all_values():
    with _lock:
        return dict(_values)


def clear():
    with _lock:
        _values.clear()
