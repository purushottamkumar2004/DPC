"""Small file helpers shared by the transfer modules."""

import json
import os
import re
import shutil
from typing import Any, Optional

# Frame names from the capture (CONTRACTS 4.1, D-031): YYYY_MM_DD_HH_MM_SS_ffffff.jpg. They sort
# in time order as text, so "frames of one cycle" = names in [start plate frame, end plate frame).
FRAME_NAME = re.compile(r"^(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{6})\.jpg$")


def is_frame_name(name: str) -> bool:
    return FRAME_NAME.match(name) is not None


def frame_time_iso(name: Optional[str]) -> Optional[str]:
    """'2026_11_10_09_15_02_123456.jpg' -> '2026-11-10T09:15:02.123456' (the capture time)."""
    m = FRAME_NAME.match(name or "")
    if not m:
        return None
    y, mo, d, h, mi, s, us = m.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}.{us}"


def save_json(path: str, data: Any) -> None:
    """Atomic: write a temp file, flush it to disk, rename over the old one."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = f"{path}.tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def load_json(path: str, default: Any = None) -> Any:
    """The file's content, or default when it is missing or unreadable (never a crash: a broken
    state file means "start that job again", which is safe)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def dir_size(path: str) -> int:
    """Bytes of every file below path (a hard link counts in full: conservative for the cap)."""
    total = 0
    for folder, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total


def link_or_copy(source: str, target: str) -> bool:
    """Hard link (same filesystem, no extra disk), else copy. False when the source is gone
    (cleanup deleted it meanwhile). Frames are only read, never changed (Project rule 12)."""
    try:
        os.link(source, target)
        return True
    except FileExistsError:
        return True
    except FileNotFoundError:
        return False
    except OSError:                       # EXDEV (another filesystem) or links not supported
        try:
            shutil.copy2(source, target)
            return True
        except FileNotFoundError:
            return False


def remove_tree(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
