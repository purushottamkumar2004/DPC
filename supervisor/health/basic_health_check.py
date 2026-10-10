"""
The three built-in health checks. Pick one per worker in workers.yaml:

  process        just "is the process alive" (always true here - the monitor
                 checks aliveness itself).
  file_activity  a file/folder must have been modified recently.
                 Camera:    watch the live image  .../LEFT/IMG_1.jpg
                 Algorithm: watch its log file, or the folder it writes to.
                 Needs NO change to the existing scripts.
  heartbeat      the worker publishes {"healthy": true} to
                 dpc/worker/<id>/health, at least every max_age_seconds.
                 For new/modified workers that can talk MQTT.
"""

import os
import time

from health.base_health_check import HealthCheck, HealthResult


class ProcessOnlyCheck(HealthCheck):
    def check(self, worker) -> HealthResult:
        return HealthResult(True)


class FileActivityCheck(HealthCheck):
    def __init__(self, paths, max_age_seconds):
        self.paths = paths
        self.max_age = max_age_seconds

    def check(self, worker) -> HealthResult:
        now = time.time()
        for path in self.paths:
            try:
                age = now - os.stat(path).st_mtime
            except OSError:
                return HealthResult(False, f"{path} does not exist")
            if age > self.max_age:
                return HealthResult(False, f"{path} not updated for {age:.0f}s (limit {self.max_age:.0f}s)")
        return HealthResult(True)


class HeartbeatCheck(HealthCheck):
    def __init__(self, heartbeats: dict, max_age_seconds):
        self.heartbeats = heartbeats       # worker_id -> time the supervisor last heard it
        self.max_age = max_age_seconds

    def check(self, worker) -> HealthResult:
        last = self.heartbeats.get(worker.id)
        # a heartbeat from before the latest (re)start does not count
        if last is None or (worker.started_at and last < worker.started_at):
            return HealthResult(False, "no heartbeat received since start")
        age = time.time() - last
        if age > self.max_age:
            return HealthResult(False, f"last heartbeat {age:.0f}s ago (limit {self.max_age:.0f}s)")
        return HealthResult(True)


def build_health_check(worker_config, heartbeats: dict) -> HealthCheck:
    h = worker_config.health
    if h.type == "file_activity":
        return FileActivityCheck(h.path, h.max_age_seconds)
    if h.type == "heartbeat":
        return HeartbeatCheck(heartbeats, h.max_age_seconds)
    return ProcessOnlyCheck()
