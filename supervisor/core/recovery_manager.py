"""
RecoveryManager - decides what to do AFTER the monitor says a worker failed.

    failure -> stop worker -> WAITING_RESTART (retry_delay) -> start again
            -> if it keeps failing, after max_retries -> FAILED (and we stop trying)

The wait is NOT a sleep: the worker is just marked WAITING_RESTART and the
monitor loop comes back to it later, so one broken worker never delays the
checks of the others.

Modes (D-019): a process that ended on its own is judged by the worker's mode.
    service   any exit is a failure                    -> retry policy, then FAILED
    oneshot   exit 0 = done, ends as STOPPED, never restarted
              exit != 0 = a failure                    -> retry policy, then FAILED
    periodic  exit 0 = run done, next run in interval_s
              exit != 0 = a failed run: event + counter, and the NEXT RUN is the retry.
              It never goes FAILED: a PLC that is down for ten minutes must not switch
              the light check off for good on a 24x7 site.
"""

import logging
import time

from core.last_error import summary
from core.process_manager import ProcessManager, Worker, WorkerStatus

log = logging.getLogger("supervisor.recovery")


class RecoveryManager:
    def __init__(self, process_manager: ProcessManager, mqtt, last_error_reader=None):
        self.pm = process_manager
        self.mqtt = mqtt
        self.read_last_error = last_error_reader or (lambda worker: None)   # F21

    # ------------------------------------------------------------------
    def handle_exit(self, worker: Worker):
        """The process is gone and nobody asked it to stop (called by the Monitor)."""
        mode = worker.config.mode
        code = worker.exit_code
        if mode != "service" and code == 0:
            self._run_completed(worker)
            return
        error = self._take_last_error(worker)
        if mode == "periodic":
            self.periodic_run_failed(worker, f"run exited with code {code}", last_error=error)
        else:
            self.handle_failure(worker, f"process exited (exit code {code})", last_error=error)

    def _take_last_error(self, worker: Worker):
        """F21: the worker's own account of why it stopped; logged and kept for the status."""
        try:
            error = self.read_last_error(worker)
        except Exception as e:                       # a bad file must never stop the recovery
            log.warning("[%s] could not read last_error.json (%s)", worker.id, type(e).__name__)
            return None
        if error:
            worker.last_error = error
            log.warning("[%s] last error: %s", worker.id, summary(error))
            for line in error.get("traceback", []):
                log.info("[%s]   %s", worker.id, line)
        return error

    def handle_run_timeout(self, worker: Worker):
        """F10: a oneshot/periodic run took longer than run_timeout_seconds. The process is
        stopped (stop_signal, then kill) and the run counts as failed, as if it had exited
        non-zero: a oneshot follows the retry policy, a periodic job waits for its next run."""
        reason = f"run took longer than {worker.config.run_timeout_seconds:.0f}s (stopped)"
        if worker.config.mode == "periodic":
            self.periodic_run_failed(worker, reason)
        else:
            self.handle_failure(worker, reason)

    def handle_start_failure(self, worker: Worker):
        """The process could not even be created (missing script, bad working directory...)."""
        reason = worker.message or "could not start process"
        if worker.config.mode == "periodic":
            self.periodic_run_failed(worker, reason, started=False)
        else:
            self.handle_failure(worker, reason)

    def periodic_run_failed(self, worker: Worker, reason: str, started: bool = True, last_error=None):
        log.warning("[%s] failure: %s", worker.id, reason)
        self.mqtt.publish_event(worker.id, "worker_failure", reason=reason, exit_code=worker.exit_code,
                                **_with_error(last_error))
        self.pm.stop_worker(worker)
        worker.consecutive_failures += 1
        self._schedule_next_run(worker, started)
        worker.message = (f"last run failed ({reason}), {worker.consecutive_failures} in a row; "
                          f"next run {self._next_run_text(worker)}")

    def _run_completed(self, worker: Worker):
        code = worker.exit_code
        self.pm.stop_worker(worker)               # clears the dead process; status STOPPED
        worker.completed_runs += 1
        worker.retry_count = 0
        worker.consecutive_failures = 0
        if worker.config.mode == "periodic":
            self._schedule_next_run(worker, started=True)
            worker.message = f"run done; next run {self._next_run_text(worker)}"
            log.info("[%s] %s", worker.id, worker.message)
        else:
            worker.message = "completed (exit code 0)"
            log.info("[%s] %s", worker.id, worker.message)
            self.mqtt.publish_event(worker.id, "worker_stopped", reason="completed", exit_code=code)

    def _schedule_next_run(self, worker: Worker, started: bool):
        """Runs are spaced from the START of the previous run, and never overlap."""
        now = time.time()
        base = worker.started_at if started and worker.started_at else now
        worker.next_run_at = max(base + worker.config.interval_s, now)

    @staticmethod
    def _next_run_text(worker: Worker) -> str:
        return time.strftime("at %H:%M:%S", time.localtime(worker.next_run_at))

    # ------------------------------------------------------------------
    def handle_failure(self, worker: Worker, reason: str, last_error=None):
        policy = worker.config.restart
        log.warning("[%s] failure: %s", worker.id, reason)
        self.mqtt.publish_event(worker.id, "worker_failure", reason=reason,
                                exit_code=worker.exit_code, **_with_error(last_error))
        worker.restart_error = last_error    # repeated in the worker_restarted event that follows

        self.pm.stop_worker(worker)          # make sure a hung process is really gone
        worker.healthy_since = None
        worker.failing_since = None

        if not policy.enabled:
            self._give_up(worker, f"{reason} (restart disabled)")
            return
        if worker.retry_count >= policy.max_retries:
            self._give_up(worker, f"{reason} (gave up after {policy.max_retries} retries)")
            return

        worker.retry_count += 1
        worker.restart_at = time.time() + policy.retry_delay_seconds
        worker.status = WorkerStatus.WAITING_RESTART
        worker.message = f"{reason}; restart {worker.retry_count}/{policy.max_retries} " \
                         f"in {policy.retry_delay_seconds:.0f}s"
        log.info("[%s] %s", worker.id, worker.message)

    def restart_if_due(self, worker: Worker):
        """Called every monitor tick for workers in WAITING_RESTART."""
        if time.time() < (worker.restart_at or 0):
            return
        log.info("[%s] restarting (attempt %d)", worker.id, worker.retry_count)
        if self.pm.restart_worker(worker):
            self.mqtt.publish_event(worker.id, "worker_restarted",
                                    retry=worker.retry_count, pid=worker.pid, **_with_error(worker.restart_error))
            worker.restart_error = None
        else:
            self.handle_failure(worker, worker.message or "could not start process")

    def note_healthy(self, worker: Worker):
        """A worker that stays healthy for a while earns its retries back."""
        if worker.retry_count == 0 or worker.healthy_since is None:
            return
        if time.time() - worker.healthy_since >= worker.config.restart.reset_after_seconds:
            log.info("[%s] stable for %.0fs, retry counter reset",
                     worker.id, worker.config.restart.reset_after_seconds)
            worker.retry_count = 0

    # ------------------------------------------------------------------
    def _give_up(self, worker: Worker, reason: str):
        worker.status = WorkerStatus.FAILED
        worker.message = reason
        log.error("[%s] FAILED: %s", worker.id, reason)
        self.mqtt.publish_event(worker.id, "worker_failed", reason=reason)


def _with_error(last_error) -> dict:
    """{"last_error": {...}} for an event, or nothing (append-only, CONTRACTS 2)."""
    return {"last_error": last_error} if last_error else {}
