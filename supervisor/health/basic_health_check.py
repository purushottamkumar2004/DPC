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
                 For new/modified workers that can talk MQTT. The extended
                 payload (pid, fps, ...) is accepted too (health/heartbeat.py).
  queue_lag      the oldest file in a queue folder (INF) must not be older than
                 max_oldest_age_seconds. An empty folder is healthy: an idle
                 algorithm is not a broken one. With require_heartbeat: true a
                 fresh heartbeat is needed as well (D-049, Q1).
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
        self.heartbeats = heartbeats       # worker_id -> Heartbeat (health/heartbeat.py)
        self.max_age = max_age_seconds

    def check(self, worker) -> HealthResult:
        beat = self.heartbeats.get(worker.id)
        # a heartbeat from before the latest (re)start does not count
        if beat is None or (worker.started_at and beat.received_at < worker.started_at):
            return HealthResult(False, "no heartbeat received since start")
        if not beat.healthy:
            return HealthResult(False, "worker reports itself unhealthy")
        age = time.time() - beat.received_at
        if age > self.max_age:
            return HealthResult(False, f"last heartbeat {age:.0f}s ago (limit {self.max_age:.0f}s)")
        return HealthResult(True)


class QueueLagCheck(HealthCheck):
    """Healthy while the oldest file of every queue folder is younger than the limit.

    File names are YYYY_MM_DD_HH_MM_SS_ffffff.jpg and sort in time order (CONTRACTS 4.1),
    so the oldest file is the smallest name: one stat() instead of one per file, which
    matters when a stuck algorithm leaves thousands of frames in INF. The age is the
    file's modification time (the frame's capture time; hard links share it).
    """
    STAT_RETRIES = 3          # the algorithm moves the oldest file away while we look at it

    def __init__(self, paths, max_oldest_age_seconds, heartbeat_check: HealthCheck = None):
        self.paths = paths
        self.max_age = max_oldest_age_seconds
        self.heartbeat_check = heartbeat_check

    def check(self, worker) -> HealthResult:
        problems = []
        for folder in self.paths:
            problem = self._folder_problem(folder)
            if problem:
                problems.append(problem)
        if self.heartbeat_check is not None:
            beat = self.heartbeat_check.check(worker)
            if not beat.healthy:
                problems.append(beat.message)
        return HealthResult(False, "; ".join(problems)) if problems else HealthResult(True)

    def _folder_problem(self, folder):
        now = time.time()
        try:
            names = sorted(n for n in os.listdir(folder) if n.endswith(".jpg"))
        except OSError:
            return f"{folder} cannot be read"
        for name in names[:self.STAT_RETRIES]:
            try:
                age = now - os.stat(os.path.join(folder, name)).st_mtime
            except OSError:
                continue                  # consumed just now: the queue is moving
            if age > self.max_age:
                return f"queue lag {age:.0f}s in {folder} (limit {self.max_age:.0f}s, {len(names)} file(s) waiting)"
            return None
        return None                       # empty, or everything we looked at was consumed


def build_health_check(worker_config, heartbeats: dict) -> HealthCheck:
    h = worker_config.health
    if h.type == "file_activity":
        return FileActivityCheck(h.path, h.max_age_seconds)
    if h.type == "heartbeat":
        return HeartbeatCheck(heartbeats, h.max_age_seconds)
    if h.type == "queue_lag":
        beat = HeartbeatCheck(heartbeats, h.max_age_seconds) if h.require_heartbeat else None
        return QueueLagCheck(h.path, h.max_oldest_age_seconds, beat)
    return ProcessOnlyCheck()
