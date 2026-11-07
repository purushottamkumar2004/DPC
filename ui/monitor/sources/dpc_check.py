"""Runs the commissioning check `scripts/dpc_check.py` (CONTRACTS 10) on request.

One run at a time, in a background thread, with a timeout; the page polls the result. The
script is read-only and prints no secret values; its lines are still masked.
"""
import os
import subprocess
import sys
import threading
import time

from monitor.secrets import mask

TIMEOUT_S = 120


class DpcCheckRunner:
    def __init__(self, code_dir: str, site_yaml: str, python=sys.executable, popen=subprocess.Popen):
        self.script = os.path.join(code_dir, "scripts", "dpc_check.py")
        self.site_yaml = site_yaml
        self.python, self._popen = python, popen
        self._lock = threading.Lock()
        self.state = {"running": False, "started_at": None, "finished_at": None, "lines": [],
                      "exit_code": None, "error": ""}

    def start(self) -> bool:
        """False if a run is already going."""
        with self._lock:
            if self.state["running"]:
                return False
            self.state = {"running": True, "started_at": time.time(), "finished_at": None, "lines": [],
                          "exit_code": None, "error": ""}
        threading.Thread(target=self._run, name="dpc-check", daemon=True).start()
        return True

    def _run(self):
        lines, code, error = [], None, ""
        if not os.path.isfile(self.script):
            error = f"dpc-check not found at {self.script}"
        else:
            try:
                proc = self._popen([self.python, self.script, self.site_yaml], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, cwd=os.path.dirname(self.script))
                try:
                    out, _ = proc.communicate(timeout=TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, _ = proc.communicate()
                    error = f"dpc-check did not finish within {TIMEOUT_S} s and was stopped."
                lines = [mask(ln) for ln in (out or "").splitlines() if ln.strip()]
                code = proc.returncode
            except Exception as e:
                error = f"dpc-check could not be started: {e}"
        with self._lock:
            self.state.update(running=False, finished_at=time.time(), lines=lines, exit_code=code, error=error)

    def snapshot(self) -> dict:
        with self._lock:
            state = dict(self.state)
            state["lines"] = [parse_line(ln) for ln in state["lines"]]
            return state


def parse_line(line: str) -> dict:
    """'PASS disk 41 % used' -> {grade: 'PASS', text: line}."""
    first = line.split(" ", 1)[0]
    grade = first if first in ("PASS", "WARN", "FAIL", "SKIP", "RESULT") else ""
    return {"grade": grade, "text": line}


_runner = None


def get_runner() -> DpcCheckRunner:
    global _runner
    if _runner is None:
        from dpc_ui.ui_config import get_config
        cfg = get_config()
        _runner = DpcCheckRunner(cfg.paths["code"], cfg.paths["site_yaml"])
    return _runner


def set_runner(runner):
    global _runner
    _runner = runner
