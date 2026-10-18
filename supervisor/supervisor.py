#!/usr/bin/env python3
"""
DPC Supervisor - starts, monitors and restarts independent worker processes.

It does not know what the workers do. It only knows: command, health, restart policy.

Usage
-----
  python3 supervisor.py run                 start everything and keep monitoring
  python3 supervisor.py check-config        validate workers.yaml (and site.yaml when a worker needs it) and exit
  python3 supervisor.py status              show worker status (via MQTT)
  python3 supervisor.py start   <worker_id> control a running supervisor (via MQTT)
  python3 supervisor.py stop    <worker_id>
  python3 supervisor.py restart <worker_id>
"""

import argparse
import logging
import os
import queue
import signal
import sys
import time
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from communication.mqtt_client import MqttClient, now_iso      # noqa: E402
from core.config import ConfigError, ConfigurationManager       # noqa: E402
from core.monitor import Monitor                                # noqa: E402
from core.process_manager import ProcessManager, Worker, WorkerStatus  # noqa: E402
from core.recovery_manager import RecoveryManager               # noqa: E402
from core.runtime_manager import RuntimeManager                 # noqa: E402
from core.site_env import SiteEnv                               # noqa: E402
from health.basic_health_check import build_health_check       # noqa: E402
from health.heartbeat import parse_heartbeat                    # noqa: E402

log = logging.getLogger("supervisor")
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config", "workers.yaml")


# ======================================================================
# The Supervisor
# ======================================================================
class Supervisor:
    def __init__(self, config: ConfigurationManager, mqtt=None, site_env: SiteEnv = None):
        self.settings = config.settings
        self.mqtt = mqtt or MqttClient(self.settings.mqtt)
        self.heartbeats = {}                  # worker_id -> Heartbeat of the last MQTT heartbeat
        self.commands = queue.Queue()         # commands from MQTT, handled in the main loop
        self.running = True

        mqtt_env = {"DPC_MQTT_HOST": self.settings.mqtt.host,
                    "DPC_MQTT_PORT": str(self.settings.mqtt.port)}
        self.process_manager = ProcessManager(self.settings.log_dir, extra_env=mqtt_env,
                                              site_env=site_env or SiteEnv())
        self.runtime = RuntimeManager(self.settings.runtime_root)
        self.recovery = RecoveryManager(self.process_manager, self.mqtt)

        # one Worker object per enabled worker, in START order (start_after respected);
        # shutdown walks the same dict backwards
        self.workers = {}
        for wc in config.start_order:
            w = Worker(config=wc)
            w.health_check = build_health_check(wc, self.heartbeats)
            self.workers[wc.id] = w

        self.monitor = Monitor(self.workers, self.process_manager, self.recovery,
                               self.mqtt, self.settings.monitoring)
        self._not_started = []                # worker ids still waiting for their turn at startup
        self._began_at = 0.0                  # when the staged start began
        self._next_launch_at = 0.0
        self._next_check = 0.0

    # ------------------------------------------------------------------
    def run(self):
        self.start()
        while self.running:
            self.tick()
            time.sleep(0.5)
        self.shutdown()

    def start(self):
        """Connect MQTT, create the folders and begin the staged start of the workers."""
        log.info("Supervisor starting with %d worker(s): %s",
                 len(self.workers), ", ".join(self.workers) or "-")

        self.mqtt.start()
        self.mqtt.subscribe(self.mqtt.worker_topic("+", "health"), self._on_health)
        self.mqtt.subscribe(self.mqtt.worker_topic("+", "command"), self._on_command)

        for worker in self.workers.values():
            self.runtime.prepare(worker.config)
        self._not_started = list(self.workers)
        self._began_at = time.time()
        self._next_check = time.time() + self.settings.monitoring.check_interval_seconds
        self._launch_ready_workers()

    def tick(self):
        """One pass of the main loop (every 0.5 s): commands, staged start, periodic runs,
        health checks every check_interval, status publishing."""
        self._handle_pending_commands()
        self._launch_ready_workers()
        self._start_due_periodic_runs()
        if time.time() >= self._next_check:
            self.monitor.check_all()
            self._next_check = time.time() + self.settings.monitoring.check_interval_seconds
        self.monitor.publish_changes()

    # ---- staged start (start_after) ----------------------------------------
    def _launch_ready_workers(self):
        """Launch at most one waiting worker per startup_stagger_seconds, in start order,
        once everything in its start_after is ready. Never blocks: the monitor keeps
        watching the workers that are already running while the others wait."""
        if not self._not_started or time.time() < self._next_launch_at:
            return
        for worker_id in self._not_started:
            worker = self.workers[worker_id]
            go, why = self._may_launch(worker)
            if not go:
                if why:
                    worker.message = why
                continue
            self._not_started.remove(worker_id)
            if why:                                        # started although a dependency is not ready
                log.warning("[%s] starting anyway: %s", worker_id, why)
            worker.message = ""
            self._start(worker, unmet=why)
            self._next_launch_at = time.time() + self.settings.monitoring.startup_stagger_seconds
            return

    def _may_launch(self, worker: Worker):
        """(go, text). text explains a wait, or an 'anyway' start (unready dependency)."""
        deps = [self.workers[d] for d in worker.config.start_after]
        if any(d.id in self._not_started for d in deps):
            return False, f"waiting for {', '.join(d.id for d in deps if d.id in self._not_started)} to start"
        waiting = [d for d in deps if not self._is_ready(d)]
        if not waiting:
            return True, ""
        names = ", ".join(d.id for d in waiting)
        if any(d.status == WorkerStatus.FAILED for d in waiting):
            return True, f"start_after not met, FAILED: {names}"
        since = max(d.first_started_at or self._began_at for d in deps)
        limit = self.settings.monitoring.start_after_timeout_seconds
        if time.time() - since >= limit:
            return True, f"start_after not ready after {limit:.0f}s: {names}"
        return False, f"waiting for {names} to be ready"

    @staticmethod
    def _is_ready(worker: Worker) -> bool:
        """service: healthy (RUNNING). oneshot / periodic: has finished a run with exit code 0."""
        if worker.config.mode == "service":
            return worker.status == WorkerStatus.RUNNING
        return worker.completed_runs > 0

    # ---- periodic workers -----------------------------------------------------
    def _start_due_periodic_runs(self):
        now = time.time()
        for worker in self.workers.values():
            if (worker.config.mode == "periodic" and worker.status == WorkerStatus.STOPPED
                    and worker.next_run_at is not None and now >= worker.next_run_at
                    and worker.id not in self._not_started):
                self._start(worker)

    def request_stop(self, *_):
        log.info("Stop requested")
        self.running = False

    def shutdown(self):
        log.info("Shutting down - stopping workers (reverse order)")
        for worker in reversed(list(self.workers.values())):
            self.process_manager.stop_worker(worker)
            worker.message = "supervisor shutdown"
        self.monitor.publish_changes(force=True)
        time.sleep(0.3)                       # let MQTT send the last statuses
        self.mqtt.stop()
        log.info("Supervisor stopped")

    # ------------------------------------------------------------------
    def _start(self, worker: Worker, unmet: str = "") -> bool:
        ok = self.process_manager.start_worker(worker)
        if ok:
            extra = {"start_after_unmet": unmet} if unmet else {}
            self.mqtt.publish_event(worker.id, "worker_started", pid=worker.pid, **extra)
        else:
            self.recovery.handle_start_failure(worker)
        return ok

    # ---- MQTT callbacks (run in MQTT thread - only touch thread-safe things) ----
    def _on_health(self, topic, payload):
        worker_id = topic.split("/")[-2]
        beat = parse_heartbeat(payload)              # old and extended payloads (D-034)
        if beat is None:
            log.warning("[%s] ignoring a health message that is not a heartbeat", worker_id)
            return
        self.heartbeats[worker_id] = beat
        worker = self.workers.get(worker_id)
        if worker is not None:
            worker.heartbeat = beat

    def _on_command(self, topic, payload):
        worker_id = topic.split("/")[-2]
        self.commands.put((worker_id, payload.get("command"), payload.get("reason", "")))

    # ---- commands (run in main thread) ----
    def _handle_pending_commands(self):
        while True:
            try:
                worker_id, command, reason = self.commands.get_nowait()
            except queue.Empty:
                return
            worker = self.workers.get(worker_id)
            if worker is None:
                log.warning("Command '%s' for unknown/disabled worker '%s'", command, worker_id)
                continue
            log.info("[%s] command: %s %s", worker_id, command, f"({reason})" if reason else "")
            try:
                self._run_command(worker, command, reason)
            except Exception:
                log.exception("[%s] command '%s' failed", worker_id, command)

    def _run_command(self, worker: Worker, command: str, reason: str):
        if worker.id in self._not_started and command in ("start", "restart"):
            self._not_started.remove(worker.id)           # the operator overrules start_after
        if command == "start":
            worker.retry_count = 0
            self._start(worker)
        elif command == "stop":
            self.process_manager.stop_worker(worker)      # status STOPPED -> monitor ignores it
            worker.next_run_at = None                     # a stopped periodic worker stays stopped
            if worker.id in self._not_started:
                self._not_started.remove(worker.id)
            worker.message = f"stopped by command {reason}".strip()
            self.mqtt.publish_event(worker.id, "worker_stopped", reason=reason)
        elif command == "restart":
            worker.retry_count = 0
            if self.process_manager.restart_worker(worker):
                self.mqtt.publish_event(worker.id, "worker_restarted", reason=reason, pid=worker.pid)
            else:
                self.recovery.handle_start_failure(worker)
        elif command == "status":
            self.monitor.publish_changes(force=True)
        else:
            log.warning("[%s] unknown command '%s'", worker.id, command)


# ======================================================================
# Command line
# ======================================================================
def setup_logging(log_dir: str):
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logfile = RotatingFileHandler(os.path.join(log_dir, "supervisor.log"),
                                  maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    logfile.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(logfile)
    logging.getLogger("paho").setLevel(logging.WARNING)


def cmd_run(config, site_env):
    setup_logging(config.settings.log_dir)
    sup = Supervisor(config, site_env=site_env)
    signal.signal(signal.SIGINT, sup.request_stop)
    signal.signal(signal.SIGTERM, sup.request_stop)
    sup.run()


def cmd_worker_command(config, command, worker_id):
    """start/stop/restart: publish an MQTT command to the running supervisor."""
    client = MqttClient(config.settings.mqtt, client_id=f"dpc-ctl-{os.getpid()}")
    client.start()
    if not client.wait_connected(3):
        print("Cannot reach the MQTT broker - is it running?")
        return 1
    client.publish(client.worker_topic(worker_id, "command"),
                   {"command": command, "reason": "manual (supervisor.py)", "timestamp": now_iso()})
    time.sleep(0.5)
    client.stop()
    print(f"Sent '{command}' to {worker_id}")
    return 0


def cmd_status(config):
    """Read the retained status messages the supervisor publishes."""
    client = MqttClient(config.settings.mqtt, client_id=f"dpc-ctl-{os.getpid()}")
    found = {}
    client.subscribe(client.worker_topic("+", "status"), lambda t, p: found.update({p["worker_id"]: p}))
    client.start()
    if not client.wait_connected(3):
        print("Cannot reach the MQTT broker - is it running?")
        return 1
    time.sleep(1.5)
    client.stop()
    if not found:
        print("No status received (is the supervisor running?)")
        return 1
    print(f"{'WORKER':<20}{'STATUS':<17}{'PID':<8}{'RESTARTS':<10}MESSAGE")
    for wid, s in sorted(found.items()):
        print(f"{wid:<20}{s['status']:<17}{str(s['pid'] or '-'):<8}{s['restart_count']:<10}{s['message']}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="DPC Supervisor")
    parser.add_argument("action", choices=["run", "check-config", "status", "start", "stop", "restart"])
    parser.add_argument("worker_id", nargs="?", help="needed for start/stop/restart")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    parser.add_argument("--site-config", default=None,
                        help="site.yaml for workers with env_from_site (default: $DPC_SITE_CONFIG, "
                             "then ~/INSIGHTZZ/config/site.yaml)")
    args = parser.parse_args()

    config = ConfigurationManager(args.config, BASE_DIR)
    try:
        config.load()
    except ConfigError as e:
        print(f"Configuration error: {e}")
        return 2

    site_env = SiteEnv(args.site_config)
    if args.action == "check-config":
        print(f"OK - {len(config.workers)} enabled worker(s) in start order: "
              f"{', '.join(w.id for w in config.start_order)}")
        users = [w.id for w in config.workers if w.env_from_site]
        if users:
            problem = site_env.check()        # prints the loader's message, never a value
            if problem:
                print(f"Site config error (needed by {', '.join(users)}): {problem}")
                return 2
            print(f"OK - site.yaml loads; DPC_* variables are exported to: {', '.join(users)}")
        return 0
    if args.action == "run":
        cmd_run(config, site_env)
        return 0
    if args.action == "status":
        return cmd_status(config)
    if not args.worker_id:
        parser.error(f"'{args.action}' needs a worker_id")
    return cmd_worker_command(config, args.action, args.worker_id)


if __name__ == "__main__":
    sys.exit(main())
