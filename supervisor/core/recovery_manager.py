"""
RecoveryManager - decides what to do AFTER the monitor says a worker failed.

    failure -> stop worker -> WAITING_RESTART (retry_delay) -> start again
            -> if it keeps failing, after max_retries -> FAILED (and we stop trying)

The wait is NOT a sleep: the worker is just marked WAITING_RESTART and the
monitor loop comes back to it later, so one broken worker never delays the
checks of the others.
"""

import logging
import time

from core.process_manager import ProcessManager, Worker, WorkerStatus

log = logging.getLogger("supervisor.recovery")


class RecoveryManager:
    def __init__(self, process_manager: ProcessManager, mqtt):
        self.pm = process_manager
        self.mqtt = mqtt

    # ------------------------------------------------------------------
    def handle_failure(self, worker: Worker, reason: str):
        policy = worker.config.restart
        log.warning("[%s] failure: %s", worker.id, reason)
        self.mqtt.publish_event(worker.id, "worker_failure", reason=reason,
                                exit_code=worker.exit_code)

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
                                    retry=worker.retry_count, pid=worker.pid)
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
