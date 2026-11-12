"""Long-running wizard tools in the background, ONE at a time (D-103: bootstrap, grabs, measure,
self-test, packaging never block the page).

    job = start("cameras.search", "Searching for cameras", [python, tool, "list", "--json"])
    snapshot()  -> {"running": bool, "kind", "title", "elapsed_s", "result", "status", ...}
    last(kind)  -> the finished result of that kind (the step shows it after a reload)

A tool's stdout is parsed as JSON (the whole output, else its last line: model_selftest prints
text first). Secrets go in through env/stdin only (never argv: visible in `ps`). Nothing a tool
prints is shown raw on a page; callers turn results into plain words.
"""
import json
import os
import subprocess
import threading
import time

from monitor.secrets import mask

TIMEOUT_S = 900
_lock = threading.Lock()
_current = {"running": False}
_last = {}


class Busy(Exception):
    """Another tool is still running."""


def parse_output(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def _finish(kind, outcome):
    with _lock:
        _current.update(outcome, running=False, finished_at=time.time())
        _last[kind] = dict(_current)


def start(kind, title, cmd=None, env=None, stdin=None, timeout=TIMEOUT_S, cwd=None, func=None):
    """Start a tool (cmd) or a Python function (func() -> dict) in the background."""
    with _lock:
        if _current.get("running"):
            raise Busy(_current.get("title") or "another step")
        _current.clear()
        _current.update(running=True, kind=kind, title=title, started_at=time.time(), result=None,
                        exit_code=None, error="")

    def run():
        if func is not None:
            try:
                outcome = {"result": func(), "exit_code": 0}
            except Exception as e:                                   # shown in plain words by the step
                outcome = {"result": None, "exit_code": 1, "error": mask(str(e))[:500]}
            _finish(kind, outcome)
            return
        try:
            proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout,
                                  env=dict(os.environ, **(env or {})), cwd=cwd)
            result = parse_output(proc.stdout)
            lines = (proc.stderr or proc.stdout or "").strip().splitlines()
            error = "" if result is not None else (mask(lines[-1])[:300] if lines else "no answer")
            outcome = {"result": result, "exit_code": proc.returncode, "error": error}
        except subprocess.TimeoutExpired:
            outcome = {"result": None, "exit_code": None, "error": f"did not finish within {timeout} s and was stopped"}
        except OSError as e:
            outcome = {"result": None, "exit_code": None, "error": f"could not be started: {e}"}
        _finish(kind, outcome)

    threading.Thread(target=run, name=f"setup-{kind}", daemon=True).start()


def snapshot():
    with _lock:
        snap = dict(_current)
    if snap.get("running"):
        snap["elapsed_s"] = round(time.time() - snap["started_at"])
    return snap


def last(kind):
    with _lock:
        return dict(_last[kind]) if kind in _last else None


def forget(prefix=""):
    with _lock:
        for k in [k for k in _last if k.startswith(prefix)]:
            del _last[k]


def reset():
    """Tests: no current job and no results."""
    with _lock:
        _current.clear()
        _current["running"] = False
        _last.clear()


def wait(timeout=30.0):
    """Tests: block until the running job ends."""
    end = time.time() + timeout
    while time.time() < end:
        if not snapshot().get("running"):
            return True
        time.sleep(0.02)
    return False
