#!/usr/bin/env python3
"""
DPC Supervisor - starts, monitors and restarts independent worker processes.

It does not know what the workers do. It only knows: command, health, restart policy.

Usage
-----
  python3 supervisor.py run                 start everything and keep monitoring
  python3 supervisor.py check-config        validate workers.yaml and exit
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
from health.basic_health_check import build_health_check       # noqa: E402

log = logging.getLogger("supervisor")
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config", "workers.yaml")


# ======================================================================
# The Supervisor
# ======================================================================
class Supervisor:
    def __init__(self, config: ConfigurationManager):
        self.settings = config.settings
        self.mqtt = MqttClient(self.settings.mqtt)
        self.heartbeats = {}                  # worker_id -> time of last MQTT heartbeat
        self.commands = queue.Queue()         # commands from MQTT, handled in the main loop
        self.running = True

        mqtt_env = {"DPC_MQTT_HOST": self.settings.mqtt.host,
                    "DPC_MQTT_PORT": str(self.settings.mqtt.port)}
        self.process_manager = ProcessManager(self.settings.log_dir, extra_env=mqtt_env)
        self.runtime = RuntimeManager(self.settings.runtime_root)
        self.recovery = RecoveryManager(self.process_manager, self.mqtt)

        # one Worker object per enabled worker in the config
        self.workers = {}
        for wc in config.workers:
            w = Worker(config=wc)
            w.health_check = build_health_check(wc, self.heartbeats)
            self.workers[wc.id] = w

        self.monitor = Monitor(self.workers, self.process_manager, self.recovery,
                               self.mqtt, self.settings.monitoring)

    # ------------------------------------------------------------------
    def run(self):
        log.info("Supervisor starting with %d worker(s): %s",
                 len(self.workers), ", ".join(self.workers) or "-")

        self.mqtt.start()
        self.mqtt.subscribe(self.mqtt.worker_topic("+", "health"), self._on_health)
        self.mqtt.subscribe(self.mqtt.worker_topic("+", "command"), self._on_command)

        # 1. create folders  2. start workers
        for worker in self.workers.values():
            self.runtime.prepare(worker.config)
        for worker in self.workers.values():
            self._start(worker)
            time.sleep(self.settings.monitoring.startup_stagger_seconds)

        # 3. main loop: handle commands often, run health checks every interval
        interval = self.settings.monitoring.check_interval_seconds
        next_check = time.time() + interval
        while self.running:
            self._handle_pending_commands()
            if time.time() >= next_check:
                self.monitor.check_all()
                next_check = time.time() + interval
            self.monitor.publish_changes()
            time.sleep(0.5)

        self.shutdown()

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
    def _start(self, worker: Worker) -> bool:
        ok = self.process_manager.start_worker(worker)
        if ok:
            self.mqtt.publish_event(worker.id, "worker_started", pid=worker.pid)
        else:
            self.recovery.handle_failure(worker, worker.message or "could not start process")
        return ok

    # ---- MQTT callbacks (run in MQTT thread - only touch thread-safe things) ----
    def _on_health(self, topic, payload):
        worker_id = topic.split("/")[-2]
        if payload.get("healthy", True):
            self.heartbeats[worker_id] = time.time()

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
        if command == "start":
            worker.retry_count = 0
            self._start(worker)
        elif command == "stop":
            self.process_manager.stop_worker(worker)      # status STOPPED -> monitor ignores it
            worker.message = f"stopped by command {reason}".strip()
            self.mqtt.publish_event(worker.id, "worker_stopped", reason=reason)
        elif command == "restart":
            worker.retry_count = 0
            if self.process_manager.restart_worker(worker):
                self.mqtt.publish_event(worker.id, "worker_restarted", reason=reason, pid=worker.pid)
            else:
                self.recovery.handle_failure(worker, worker.message or "could not start process")
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


def cmd_run(config):
    setup_logging(config.settings.log_dir)
    sup = Supervisor(config)
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
    args = parser.parse_args()

    config = ConfigurationManager(args.config, BASE_DIR)
    try:
        config.load()
    except ConfigError as e:
        print(f"Configuration error: {e}")
        return 2

    if args.action == "check-config":
        print(f"OK - {len(config.workers)} enabled worker(s): {', '.join(w.id for w in config.workers)}")
        return 0
    if args.action == "run":
        cmd_run(config)
        return 0
    if args.action == "status":
        return cmd_status(config)
    if not args.worker_id:
        parser.error(f"'{args.action}' needs a worker_id")
    return cmd_worker_command(config, args.action, args.worker_id)


if __name__ == "__main__":
    sys.exit(main())
