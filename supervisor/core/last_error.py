"""
Why a worker stopped (D-073 D7, CONTRACTS 2, STATUS F21).

A worker that exits on an error first writes <root>/run/<worker_id>.last_error.json:
    {"at": ..., "type": "ValueError", "message": ..., "where": "file.py:123", "traceback": [...]}
and deletes a stale one at its own start (inspection does this since P7; other workers with
their next change). When a worker process ends, the Supervisor reads the file and adds it
as `last_error` to the worker_failure event, the worker_restarted event that follows, the
worker status, and supervisor.log.

What reaches MQTT and the log is checked here, whatever the worker wrote:
  * only a file written during the run that just ended counts (older = an earlier run);
  * only the five known fields, each type-checked and cut short;
  * the secret values of site.yaml are replaced by *** a second time, in case a worker
    does not scrub its own messages.
"""

import json
import logging
import os
from typing import Callable, Iterable, Optional

log = logging.getLogger("supervisor.last_error")

MAX_FILE_BYTES = 64 * 1024
MAX_TEXT = {"at": 40, "type": 100, "message": 500, "where": 200}
MAX_TRACEBACK_LINES = 12
MAX_TRACEBACK_LINE = 300
CLOCK_SLACK_S = 1.0          # file mtime vs the Supervisor's start time of the process


def last_error_path(run_dir: str, worker_id: str) -> str:
    return os.path.join(run_dir, f"{worker_id}.last_error.json")


def read_last_error(run_dir: str, worker_id: str, since: Optional[float],
                    secrets: Iterable[str] = ()) -> Optional[dict]:
    """The checked last_error of the run that started at `since`, or None."""
    path = last_error_path(run_dir, worker_id)
    try:
        info = os.stat(path)
        if since and info.st_mtime < since - CLOCK_SLACK_S:
            return None                               # left over from an earlier run
        if info.st_size > MAX_FILE_BYTES:
            return {"type": "unknown", "message": f"{os.path.basename(path)} is too big to read"}
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"type": "unknown", "message": f"{os.path.basename(path)} could not be read"}
    if not isinstance(data, dict):
        return None
    return _checked(data, [s for s in secrets if s]) or None


def _checked(data: dict, secrets: list) -> dict:
    def scrub(text: str) -> str:
        for secret in secrets:
            text = text.replace(secret, "***")
        return text

    out = {}
    for name, limit in MAX_TEXT.items():
        value = data.get(name)
        if isinstance(value, str) and value:
            out[name] = scrub(value)[:limit]
    lines = data.get("traceback")
    if isinstance(lines, list):
        lines = [scrub(line)[:MAX_TRACEBACK_LINE] for line in lines if isinstance(line, str)]
        if lines:
            out["traceback"] = lines[-MAX_TRACEBACK_LINES:]
    return out


def summary(error: dict) -> str:
    """'ValueError: camera table missing (inspection.py:123)' for log lines."""
    text = f"{error.get('type', 'error')}: {error.get('message', '')}".rstrip(": ")
    return f"{text} ({error['where']})" if error.get("where") else text


class LastErrorReader:
    """Callable for the RecoveryManager: worker -> checked last_error or None."""

    def __init__(self, run_dir: str, secrets: Callable[[], Iterable[str]] = lambda: ()):
        self.run_dir = run_dir
        self.secrets = secrets

    def __call__(self, worker) -> Optional[dict]:
        if not os.path.exists(last_error_path(self.run_dir, worker.id)):
            return None                               # the common case: no site.yaml read needed
        return read_last_error(self.run_dir, worker.id, worker.started_at, self.secrets())
