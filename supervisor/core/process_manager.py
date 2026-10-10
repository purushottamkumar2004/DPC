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

    @property
    def id(self) -> str:
        return self.config.id


def _iso(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None


class ProcessManager:
    def __init__(self, log_dir: str, extra_env: Optional[dict] = None):
        self.worker_log_dir = os.path.join(log_dir, "workers")
        os.makedirs(self.worker_log_dir, exist_ok=True)
        self.extra_env = extra_env or {}

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

        worker.log_path = os.path.join(self.worker_log_dir, f"{cfg.id}.log")
        self._rotate_if_big(worker.log_path)

        env = dict(os.environ)
        env.update(self.extra_env)
        env.update(cfg.env)
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
        worker.exit_code = None
        worker.failing_since = None
        worker.healthy_since = None
        worker.restart_at = None
        worker.message = ""
        worker.status = WorkerStatus.STARTING
        log.info("[%s] started, pid=%s", cfg.id, proc.pid)
        return True

    # ------------------------------------------------------------------
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
