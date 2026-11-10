"""Which log file the Programs page shows (F64).

Two tabs per program:
  program  the program's own log in <logs>/ (default): the name comes from the worker's rendered
           JSON (`log.file_name`, frame capture) where it exists, else from PROGRAM_LOG_NAMES;
  start    "Start and crash output": the Supervisor's capture of stdout/stderr
           (status `log_file` = logs/workers/<id>.log): start lines, warnings, tracebacks.
Older days: the rotated files next to it, <name>.YYYY-MM-DD (TimedRotatingFileHandler at midnight)
or <name>.YYYY-MM-DD_HH-MM-SS (cleanup's size-and-time rotation), newest first.

Only `log.file_name` and `paths.logs` are read from a rendered JSON; nothing else in it (it holds
passwords) is ever shown.
"""
import json
import logging
import os
import re

log = logging.getLogger("dpc.ui.logs")

# worker type (or id) -> file name in <logs>/, used when the rendered JSON names no file
PROGRAM_LOG_NAMES = {
    "plc": "plc.log",
    "mail": "mail.log",
    "cleanup": "cleanup.log",
    "ui": "ui.log",
}
ROTATED = re.compile(r"^\.(\d{4}-\d{2}-\d{2})(_\d{2}-\d{2}-\d{2})?$")
TABS = (("program", "Program log"), ("start", "Start and crash output"))


def _rendered(cfg, worker_id):
    from monitor.sources.rendered import rendered
    return rendered(cfg, worker_id)


def program_log_path(cfg, worker_id: str, wtype: str) -> str:
    """The program's own log file (it may not exist yet)."""
    rendered = _rendered(cfg, worker_id)
    logs_dir = ((rendered.get("paths") or {}).get("logs")) or cfg.paths["logs"]
    name = ((rendered.get("log") or {}).get("file_name") or "").strip()
    if name and os.path.basename(name) == name:
        return os.path.join(logs_dir, name)
    if wtype == "frame_capture" and worker_id.startswith("frame_capture_"):
        return os.path.join(logs_dir, f"FrameCapture_{worker_id[len('frame_capture_'):].upper()}.log")
    if wtype == "inspection":
        return os.path.join(logs_dir, f"{worker_id}.log")
    return os.path.join(logs_dir, PROGRAM_LOG_NAMES.get(wtype) or PROGRAM_LOG_NAMES.get(worker_id) or f"{worker_id}.log")


def older_days(path: str):
    """[(label, path)] of the rotated files of `path`, newest first."""
    if not path:
        return []
    folder, base = os.path.dirname(path), os.path.basename(path)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    found = []
    for name in names:
        if name.startswith(base + "."):
            match = ROTATED.match(name[len(base):])
            if match:
                label = match.group(1) + (" " + match.group(2)[1:].replace("-", ":") if match.group(2) else "")
                found.append((label, os.path.join(folder, name)))
    return sorted(found, reverse=True)


def choose(cfg, program: dict, tab: str = "", day: str = ""):
    """(tab, path, older days, tabs) for one program; unknown tab/day fall back to the defaults."""
    tab = tab if tab in dict(TABS) else "program"
    path = (program_log_path(cfg, program["id"], program["type"]) if tab == "program"
            else program.get("log_file") or "")
    days = older_days(path)
    if day:
        match = [p for label, p in days if label == day]
        path = match[0] if match else path
    return tab, path, days
