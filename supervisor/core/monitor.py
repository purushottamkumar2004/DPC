"""
Monitor - looks at every worker once per check interval.

For each worker:

  1. Process dead?               -> hand to RecoveryManager right away
                                    (a dead process is certain, no need to wait).
  2. Health check passes?        -> RUNNING, all good.
  3. Health check fails?         -> NOT an immediate restart:
        - inside the startup grace period  -> ignore (status STARTING)
        - first failure                    -> remember the time, check again later
        - still failing after worker_timeout_seconds -> UNHEALTHY -> recovery

Also publishes worker status to MQTT when it changes (and periodically).
"""

import logging
import time

from core.process_manager import ProcessManager, WorkerStatus

log = logging.getLogger("supervisor.monitor")

# Statuses the monitor leaves alone: stopped on purpose, or already given up on.
SKIPPED = (WorkerStatus.STOPPED, WorkerStatus.FAILED)


class Monitor:
    def __init__(self, workers: dict, process_manager: ProcessManager,
                 recovery, mqtt, settings):
        self.workers = workers
        self.pm = process_manager
        self.recovery = recovery
        self.mqtt = mqtt
        self.timeout = settings.worker_timeout_seconds
        self.high_cost_timeout = settings.high_cost_timeout_seconds
        self.publish_interval = settings.status_publish_interval_seconds
        self._last_published = {}
        self._last_full_publish = 0.0

    # ------------------------------------------------------------------
    def check_all(self):
        for worker in self.workers.values():
            try:
                self._check_worker(worker)
            except Exception:
                # A bug in one check must not kill monitoring of the others.
                log.exception("[%s] monitor error", worker.id)

    def _check_worker(self, worker):
        if worker.status in SKIPPED:
            return

        if worker.status == WorkerStatus.WAITING_RESTART:
            self.recovery.restart_if_due(worker)
            return

        # 1. process alive? (what an exit means depends on the mode: see RecoveryManager)
        if not self.pm.is_running(worker):
            self.recovery.handle_exit(worker)
            return

        # 2. oneshot/periodic run too long? (F10)
        now = time.time()
        limit = worker.config.run_timeout_seconds
        if limit and worker.started_at and now - worker.started_at > limit:
            log.warning("[%s] run still going after %.0fs (run_timeout_seconds %.0f): stopping it",
                        worker.id, now - worker.started_at, limit)
            self.recovery.handle_run_timeout(worker)
            return

        # 3. health check
        result = worker.health_check.check(worker)

        if result.healthy:
            if worker.status != WorkerStatus.RUNNING:
                log.info("[%s] healthy -> RUNNING", worker.id)
            worker.status = WorkerStatus.RUNNING
            worker.failing_since = None
            worker.message = ""
            if worker.healthy_since is None:
                worker.healthy_since = now
            self.recovery.note_healthy(worker)
            return

        # 4. unhealthy - be tolerant
        worker.healthy_since = None
        grace = worker.config.health.startup_grace_seconds
        if now - worker.started_at < grace:
            worker.status = WorkerStatus.STARTING
            worker.message = f"starting: {result.message}"
            return

        if worker.failing_since is None:
            worker.failing_since = now
            log.warning("[%s] health check failed: %s (will re-check)", worker.id, result.message)
            worker.message = result.message
            return

        failing_for = now - worker.failing_since
        timeout = self._timeout_for(worker)
        if failing_for >= timeout:
            worker.status = WorkerStatus.UNHEALTHY
            self.recovery.handle_failure(worker, f"unhealthy for {failing_for:.0f}s: {result.message}")
        else:
            log.info("[%s] still failing (%.0fs/%.0fs): %s",
                     worker.id, failing_for, timeout, result.message)

    def _timeout_for(self, worker) -> float:
        """A restart_cost: high worker (an algorithm: wheel counter and plate state are lost,
        up to a full revolution is not inspected) must fail longer before it is restarted."""
        return self.high_cost_timeout if worker.config.restart_cost == "high" else self.timeout

    # ------------------------------------------------------------------
    def publish_changes(self, force=False):
        """Publish status for workers that changed; refresh all every N seconds."""
        now = time.time()
        refresh = force or now - self._last_full_publish >= self.publish_interval
        for worker in self.workers.values():
            key = (worker.status, worker.pid, worker.restart_count, worker.message)
            if refresh or self._last_published.get(worker.id) != key:
                self.mqtt.publish_status(worker.id, self.pm.get_status(worker))
                self._last_published[worker.id] = key
        if refresh:
            self._last_full_publish = now
