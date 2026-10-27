"""
ProcessManager - starts, stops and inspects worker processes.

It only knows how to run a command. It has no idea what the command does
(camera, algorithm, PLC...). All state about a worker lives in a `Worker`
object so every other module can read it in one place.
"""

import logging
import os
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from core.config import WorkerConfig
from core.site_env import SiteEnv, SiteEnvError

log = logging.getLogger("supervisor.process")

MAX_WORKER_LOG_BYTES = 50 * 1024 * 1024  # worker stdout log is renamed to .old above this size


class WorkerStatus(str, Enum):
    STARTING = "STARTING"              # launched, inside startup grace period
    RUNNING = "RUNNING"                # alive and healthy
    UNHEALTHY = "UNHEALTHY"            # health failed for too long, about to be restarted
    WAITING_RESTART = "WAITING_RESTART"  # stopped, waiting retry_delay before restart
    STOPPED = "STOPPED"                # stopped on purpose (command or shutdown)
    FAILED = "FAILED"                  # gave up after max_retries


@dataclass
class Worker:
    """Configuration + runtime state of one managed worker."""
    config: WorkerConfig
    health_check: object = None            # set by supervisor.py

    status: WorkerStatus = WorkerStatus.STOPPED
    process: Optional[subprocess.Popen] = None
    pid: Optional[int] = None
    started_at: Optional[float] = None
    restart_count: int = 0                 # total restarts since supervisor start
    retry_count: int = 0                   # consecutive failed recoveries (resets when healthy)
    last_restart_at: Optional[float] = None
    exit_code: Optional[int] = None
    log_path: Optional[str] = None
    failing_since: Optional[float] = None  # when the current run of failed checks began
    healthy_since: Optional[float] = None
    restart_at: Optional[float] = None     # when WAITING_RESTART should end
    message: str = ""
    # ---- V2: oneshot / periodic runs ----
    next_run_at: Optional[float] = None    # periodic only; None = not scheduled (e.g. stopped by command)
    completed_runs: int = 0                # runs that ended with exit code 0
    consecutive_failures: int = 0          # periodic only: failed runs in a row
    first_started_at: Optional[float] = None   # first launch since supervisor start (start_after waits)
    heartbeat: Optional[object] = None     # last Heartbeat (health/heartbeat.py), for the status
    # ---- P5 ----
    paused_by: Optional[str] = None        # D-068: stopped because the worker it follows stopped
    config_pending: bool = False           # 6f: rendered config changed, restart still to do (restart_cost high)
    # ---- F21 ----
    last_error: Optional[dict] = None      # newest last_error.json read for this worker (kept until a newer one)
    restart_error: Optional[dict] = None   # the error that caused the restart now waiting (for worker_restarted)

    @property
    def id(self) -> str:
        return self.config.id


def _iso(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None


class ProcessManager:
    def __init__(self, log_dir: str, extra_env: Optional[dict] = None, site_env: Optional[SiteEnv] = None):
        self.worker_log_dir = os.path.join(log_dir, "workers")
        os.makedirs(self.worker_log_dir, exist_ok=True)
        self.extra_env = extra_env or {}
        self.site_env = site_env or SiteEnv()
        self.rendered_dir: Optional[str] = None     # <root>/config/rendered, set by the Supervisor (6f)

    # ------------------------------------------------------------------
    def start_worker(self, worker: Worker) -> bool:
        """Launch the worker command. Returns True if the process was created."""
        if self.is_running(worker):
            return True

        cfg = worker.config
        if not os.path.isdir(cfg.working_directory):
            worker.message = f"working directory not found: {cfg.working_directory}"
            log.error("[%s] %s", cfg.id, worker.message)
            return False

        env = dict(os.environ)
        env.update(self.extra_env)
        if cfg.env_from_site:
            try:
                site_vars = self.site_env.variables()
            except SiteEnvError as e:
                worker.message = f"site.yaml: {e}"        # the text holds no secret value
                log.error("[%s] cannot start: %s", cfg.id, worker.message)
                return False
            env.update(site_vars)
            log.info("[%s] env_from_site: %d variable(s) exported: %s",
                     cfg.id, len(site_vars), ", ".join(sorted(site_vars)))     # names only
        worker_config = self.worker_config_path(cfg.id)
        if worker_config:
            env["DPC_WORKER_CONFIG"] = worker_config      # D-062: the worker reads its rendered JSON
        env.update(cfg.env)                    # what workers.yaml says for this worker wins

        worker.log_path = os.path.join(self.worker_log_dir, f"{cfg.id}.log")
        self._rotate_if_big(worker.log_path)
        env["PYTHONUNBUFFERED"] = "1"          # so print() shows up in the log immediately
        env["DPC_WORKER_ID"] = cfg.id

        try:
            with open(worker.log_path, "ab") as log_file:
                log_file.write(f"\n=== start {_iso(time.time())} : {cfg.command} ===\n".encode())
                log_file.flush()
                # start_new_session -> the worker gets its own process group, so
                # we can stop it together with any children it spawned, and
                # Ctrl+C on the supervisor does not hit the workers directly.
                proc = subprocess.Popen(
                    shlex.split(cfg.command),
                    cwd=cfg.working_directory,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except (OSError, ValueError) as e:
            worker.message = f"cannot start: {e}"
            log.error("[%s] %s", cfg.id, worker.message)
            return False

        worker.process = proc
        worker.pid = proc.pid
        worker.started_at = time.time()
        worker.config_pending = False          # a fresh process has read the current rendered file
        if worker.first_started_at is None:
            worker.first_started_at = worker.started_at
        worker.exit_code = None
        worker.failing_since = None
        worker.healthy_since = None
        worker.restart_at = None
        worker.message = ""
        worker.status = WorkerStatus.STARTING
        log.info("[%s] started, pid=%s", cfg.id, proc.pid)
        return True

    # ------------------------------------------------------------------
    def worker_config_path(self, worker_id: str) -> Optional[str]:
        """<rendered>/<worker_id>.json if the renderer made one for this worker, else None."""
        if not self.rendered_dir:
            return None
        path = os.path.join(self.rendered_dir, f"{worker_id}.json")
        return path if os.path.isfile(path) else None

    def stop_worker(self, worker: Worker):
        """Ask the worker to stop (configured signal), then force-kill if it ignores us."""
        proc = worker.process
        if proc is not None and proc.poll() is None:
            sig = getattr(signal, worker.config.stop_signal)
            log.info("[%s] stopping pid=%s with %s", worker.id, proc.pid, worker.config.stop_signal)
            self._signal_group(proc, sig)
            try:
                proc.wait(timeout=worker.config.stop_timeout_seconds)
            except subprocess.TimeoutExpired:
                log.warning("[%s] did not stop in %ss, sending SIGKILL",
                            worker.id, worker.config.stop_timeout_seconds)
                self._signal_group(proc, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    log.error("[%s] pid=%s will not die", worker.id, proc.pid)

        if proc is not None:
            worker.exit_code = proc.poll()
        worker.process = None
        worker.pid = None
        worker.status = WorkerStatus.STOPPED

    def restart_worker(self, worker: Worker) -> bool:
        self.stop_worker(worker)
        ok = self.start_worker(worker)
        worker.restart_count += 1
        worker.last_restart_at = time.time()
        return ok

    # ------------------------------------------------------------------
    def is_running(self, worker: Worker) -> bool:
        proc = worker.process
        if proc is None:
            return False
        code = proc.poll()
        if code is None:
            return True
        worker.exit_code = code          # remember how it ended
        return False

    def get_status(self, worker: Worker) -> dict:
        return {
            "worker_id": worker.id,
            "type": worker.config.type,
            "status": worker.status.value,
            "pid": worker.pid,
            "started_at": _iso(worker.started_at),
            "restart_count": worker.restart_count,
            "last_restart_at": _iso(worker.last_restart_at),
            "exit_code": worker.exit_code,
            "log_file": worker.log_path,
            "message": worker.message,
            "timestamp": _iso(time.time()),
            # V2, append-only (CONTRACTS 8): older consumers ignore these
            "mode": worker.config.mode,
            "restart_cost": worker.config.restart_cost,
            "next_run_at": _iso(worker.next_run_at),
            "consecutive_failures": worker.consecutive_failures,
            "heartbeat": dict(worker.heartbeat.info) if worker.heartbeat is not None else None,
            # P5, append-only
            "follows": worker.config.follows,
            "paused_by": worker.paused_by,
            "config_pending": worker.config_pending,
            # F21, append-only
            "last_error": worker.last_error,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _signal_group(proc: subprocess.Popen, sig):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except ProcessLookupError:
            pass                          # already gone

    @staticmethod
    def _rotate_if_big(path: str):
        try:
            if os.path.getsize(path) > MAX_WORKER_LOG_BYTES:
                os.replace(path, path + ".old")
        except OSError:
            pass
