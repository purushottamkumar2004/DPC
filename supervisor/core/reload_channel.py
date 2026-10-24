"""
ReloadChannel - how `supervisor.py reload` reaches the Supervisor that is already running.

    supervisor.py reload [--confirm]            or            systemctl reload dpc-supervisor
        1. writes <run>/reload_request.json  {"id": ..., "confirm": true|false}   (skipped by systemctl)
        2. sends SIGHUP to the pid in <run>/supervisor.pid
        3. waits for <run>/reload_result.json with the same id and prints it

    Supervisor: the SIGHUP handler only sets a flag; the main loop takes the request,
    renders, restarts what changed, and writes the result.

Why not MQTT: a reload must work with the broker down (D-034), and only the site user
can signal the Supervisor or write into <run>/. The files hold worker ids, statuses and
the loader's error text, never a config value.
"""

import json
import os
import signal
import time
import uuid
from typing import Optional

PID_FILE = "supervisor.pid"
REQUEST_FILE = "reload_request.json"
RESULT_FILE = "reload_result.json"


class ReloadError(Exception):
    """The running Supervisor could not be reached. The text is meant for the person."""


class ReloadChannel:
    def __init__(self, run_dir: str):
        self.run_dir = run_dir

    def _path(self, name: str) -> str:
        return os.path.join(self.run_dir, name)

    # ---------------- Supervisor side ----------------
    def write_pid(self):
        os.makedirs(self.run_dir, exist_ok=True)
        _write_private(self._path(PID_FILE), str(os.getpid()))

    def remove_pid(self):
        try:
            with open(self._path(PID_FILE)) as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(self._path(PID_FILE))
        except OSError:
            pass

    def take_request(self) -> dict:
        """The waiting request, removed from disk. A plain SIGHUP (systemctl reload) has none:
        then it is an unconfirmed reload without an id."""
        path = self._path(REQUEST_FILE)
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        try:
            os.remove(path)
        except OSError:
            pass
        return {"id": data.get("id") if isinstance(data.get("id"), str) else None,
                "confirm": data.get("confirm") is True}

    def write_result(self, result: dict):
        _write_private(self._path(RESULT_FILE), json.dumps(result, indent=1))

    # ---------------- command-line side ----------------
    def running_pid(self) -> Optional[int]:
        """The pid of a live `supervisor.py run`, or None (no pid file, or a stale one)."""
        try:
            with open(self._path(PID_FILE)) as f:
                pid = int(f.read().strip())
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().split(b"\0")
        except (OSError, ValueError):
            return None
        is_supervisor = any(part.endswith(b"supervisor.py") for part in cmdline) and b"run" in cmdline
        return pid if is_supervisor else None

    def request(self, confirm: bool, timeout_s: float = 120.0) -> dict:
        pid = self.running_pid()
        if pid is None:
            raise ReloadError(f"the Supervisor is not running (no live pid in {self._path(PID_FILE)})")
        request_id = uuid.uuid4().hex
        _write_private(self._path(REQUEST_FILE), json.dumps({"id": request_id, "confirm": bool(confirm)}))
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError as exc:
            raise ReloadError(f"cannot signal the Supervisor (pid {pid}): {exc.strerror}") from None

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            result = self._read_result()
            if result and result.get("id") == request_id:
                return result
            time.sleep(0.2)
        raise ReloadError(f"no answer from the Supervisor within {timeout_s:.0f}s; "
                          f"see ~/INSIGHTZZ/logs/supervisor.log")

    def _read_result(self) -> Optional[dict]:
        try:
            with open(self._path(RESULT_FILE)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None


def _write_private(path: str, text: str):
    """Atomic write, readable by the site user only."""
    tmp = f"{path}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
