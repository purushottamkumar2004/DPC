#!/usr/bin/env python3
"""
DPC Supervisor - starts, monitors and restarts independent worker processes.

It does not know what the workers do. It only knows: command, health, restart policy.

Usage
-----
  python3 supervisor.py run                 start everything and keep monitoring
  python3 supervisor.py check-config        validate workers.yaml (and site.yaml) and exit
  python3 supervisor.py reload [--confirm]  render site.yaml again; restart only the workers whose
                                            rendered config changed (--confirm: restart_cost high too)
  python3 supervisor.py model status [--json]                  per model: sides, current, previous, pinned, installed
  python3 supervisor.py model switch <name> <version> [--confirm]   verify, self-test, move current, restart its sides
  python3 supervisor.py model rollback <name> [--confirm]      back to the previous version (kept on disk)
  python3 supervisor.py model pin <name> <version> | model unpin <name>
  python3 supervisor.py status              show worker status (via MQTT)
  python3 supervisor.py start   <worker_id> control a running supervisor (via MQTT)
  python3 supervisor.py stop    <worker_id>
  python3 supervisor.py restart <worker_id>
"""

import argparse
import json
import logging
import os
import queue
import shlex
import shutil
import signal
import sys
import time
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from communication.mqtt_client import MqttClient, now_iso      # noqa: E402
from core.config import ConfigError, ConfigurationManager, parse_quiet_window  # noqa: E402
from core.models import DEFAULT_SELFTEST, ModelCommandError, ModelManager   # noqa: E402
from core.monitor import Monitor                                # noqa: E402
from core.last_error import LastErrorReader                     # noqa: E402
from core.process_manager import ProcessManager, Worker, WorkerStatus  # noqa: E402
from core.recovery_manager import RecoveryManager               # noqa: E402
from core.runtime_manager import RuntimeManager                 # noqa: E402
from core.reload_channel import ReloadChannel, ReloadError      # noqa: E402
from core.site_env import SiteEnv, SiteEnvError                 # noqa: E402
from health.basic_health_check import build_health_check       # noqa: E402
from health.heartbeat import parse_heartbeat                    # noqa: E402

log = logging.getLogger("supervisor")
EXAMPLE_CONFIG = os.path.join(BASE_DIR, "config", "workers.yaml")    # dummy workers, for development


# ======================================================================
# The Supervisor
# ======================================================================
class Supervisor:
    # a follower is paused when the worker it follows enters one of these (D-068)
    LEADER_DOWN = (WorkerStatus.WAITING_RESTART, WorkerStatus.UNHEALTHY, WorkerStatus.FAILED, WorkerStatus.STOPPED)

    def __init__(self, config: ConfigurationManager, mqtt=None, site_env: SiteEnv = None):
        self.settings = config.settings
        self.site_env = site_env or SiteEnv()
        # the only client that announces the Supervisor's presence (last-will, online/offline; F28)
        self.mqtt = mqtt or MqttClient(self.settings.mqtt, login=self._mqtt_login(), announce_presence=True)
        self.heartbeats = {}                  # worker_id -> Heartbeat of the last MQTT heartbeat
        self.commands = queue.Queue()         # commands from MQTT, handled in the main loop
        self.running = True

        mqtt_env = {"DPC_MQTT_HOST": self.settings.mqtt.host,
                    "DPC_MQTT_PORT": str(self.settings.mqtt.port)}
        self.process_manager = ProcessManager(self.settings.log_dir, extra_env=mqtt_env,
                                              site_env=self.site_env)
        self.runtime = RuntimeManager(self.settings.runtime_root)
        # F21: <root>/run/<id>.last_error.json; runtime.root is <root>/run on a site (CONTRACTS 4.1)
        self.recovery = RecoveryManager(self.process_manager, self.mqtt,
                                        LastErrorReader(self.settings.runtime_root, self.site_env.secret_values))

        # one Worker object per enabled worker, in START order (start_after respected);
        # shutdown walks the same dict backwards
        self.workers = {}
        for wc in config.start_order:
            w = Worker(config=wc)
            w.health_check = build_health_check(wc, self.heartbeats)
            self.workers[wc.id] = w

        self.disabled = list(config.disabled)          # F59: published once as DISABLED (retained)
        self.monitor = Monitor(self.workers, self.process_manager, self.recovery,
                               self.mqtt, self.settings.monitoring)
        self._not_started = []                # worker ids still waiting for their turn at startup
        self._began_at = 0.0                  # when the staged start began
        self._reload_requested = False        # set by SIGHUP; handled in the main loop
        self.reload_channel = None            # set by cmd_run (pid + request/result files in <run>/)
        self._leader_up = {}                  # follows: leader id -> RUNNING since it last went down
        self._next_launch_at = 0.0
        self._next_check = 0.0
        self._next_presence = 0.0            # F28: re-publish "online" every status interval

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
        self._render_at_start()
        self._not_started = list(self.workers)
        self._began_at = time.time()
        self._next_check = time.time() + self.settings.monitoring.check_interval_seconds
        self._launch_ready_workers()

    def tick(self):
        """One pass of the main loop (every 0.5 s): commands, staged start, periodic runs,
        health checks every check_interval, status publishing."""
        self._handle_pending_commands()
        if self._reload_requested:
            self._reload_requested = False
            self._serve_reload_request()
        self._launch_ready_workers()
        self._start_due_periodic_runs()
        if time.time() >= self._next_check:
            self.monitor.check_all()
            self._apply_follows()
            self._restart_pending_in_quiet_window()
            self._next_check = time.time() + self.settings.monitoring.check_interval_seconds
        self.monitor.publish_changes()
        self._publish_presence_if_due()

    def _publish_disabled(self):
        """F59: a retained status DISABLED for every `enabled: false` worker, with the same keys as
        any worker status, so the UI shows the program as switched off (and a worker that was
        disabled since the last run does not keep showing its old RUNNING status)."""
        for wc in self.disabled:
            status = self.process_manager.get_status(Worker(config=wc))
            status.update(status="DISABLED", message="disabled in workers.yaml (enabled: false)")
            self.mqtt.publish_status(wc.id, status)

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
        leader = self.workers.get(worker.config.follows) if worker.config.follows else None
        if leader is not None and leader.status != WorkerStatus.RUNNING:
            return False, f"follows {leader.id}: waiting until it is healthy ({leader.status.value})"
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

    # ---- follows (D-068: inspection follows its camera) ------------------------
    def _apply_follows(self):
        """Pause a follower when the worker it follows goes down (failure, restart after a
        failure, FAILED, or stopped); start it once when that worker is healthy again.

        "Goes down" means: was RUNNING, and is now in LEADER_DOWN. STARTING changes nothing,
        so a planned restart (reload, restart command: RUNNING -> STARTING -> RUNNING) pauses
        nothing, and a capture retrying while its camera is unplugged (WAITING_RESTART ->
        STARTING -> WAITING_RESTART ...) is one outage, not many. So nothing is started in a
        loop, and a follower the operator started during the outage is left alone."""
        for worker in self.workers.values():
            leader_id = worker.config.follows
            if not leader_id or worker.id in self._not_started:
                continue
            leader = self.workers[leader_id]
            went_down = leader.status in self.LEADER_DOWN and self._leader_up.get(leader_id, False)
            if went_down and self._is_active(worker):
                self._pause(worker, leader)
            elif leader.status == WorkerStatus.RUNNING and worker.paused_by == leader_id:
                self._resume(worker, leader)
        for worker in self.workers.values():
            leader = self.workers.get(worker.config.follows) if worker.config.follows else None
            if leader is None:
                continue
            if leader.status == WorkerStatus.RUNNING:
                self._leader_up[leader.id] = True
            elif leader.status in self.LEADER_DOWN:
                self._leader_up[leader.id] = False

    def _is_active(self, worker: Worker) -> bool:
        """Has a process, or is about to get one again (waiting for a restart)."""
        return self.process_manager.is_running(worker) or worker.status == WorkerStatus.WAITING_RESTART

    def _pause(self, worker: Worker, leader: Worker):
        reason = f"follows {leader.id}, which is {leader.status.value}"
        log.warning("[%s] pausing: %s", worker.id, reason)
        self.process_manager.stop_worker(worker)
        worker.paused_by = leader.id
        worker.retry_count = 0
        worker.failing_since = None
        worker.message = f"paused: {reason}"
        self.mqtt.publish_event(worker.id, "worker_stopped", reason=reason)

    def _resume(self, worker: Worker, leader: Worker):
        log.info("[%s] %s is healthy again: starting", worker.id, leader.id)
        worker.paused_by = None
        worker.message = ""
        self._start(worker, reason=f"follows {leader.id}: healthy again")

    # ---- rendered configs and reload (6f, D-062) ----------------------------------
    def _mqtt_login(self):
        """F11: the broker login is mqtt.user / mqtt.password in site.yaml. Without a readable
        site.yaml the Supervisor connects without a login (and keeps supervising, D-034)."""
        if not self.settings.mqtt.enabled:
            return None, None
        try:
            return self.site_env.mqtt_login()
        except SiteEnvError as e:
            log.warning("MQTT login not read (site.yaml: %s); connecting without a login", e)
            return None, None

    def _render_at_start(self):
        result = self.site_env.render()
        self.process_manager.rendered_dir = result.rendered_dir
        if result.ok:
            log.info("Rendered configs: %s", _change_summary(result.changes) or "none")
            return
        site_yaml = self.site_env.site_yaml_path()
        level = logging.ERROR if site_yaml and os.path.exists(site_yaml) else logging.WARNING
        log.log(level, "Configs not rendered (%s); workers get the files already in %s",
                result.error, result.rendered_dir)
        self._publish_supervisor_event("config_render_failed", error=result.error)

    def request_reload(self, *_):
        """SIGHUP handler: only a flag; the work is done in the main loop."""
        self._reload_requested = True

    def _serve_reload_request(self):
        request = self.reload_channel.take_request() if self.reload_channel else {
            "id": None, "confirm": False, "action": "reload", "workers": [], "reason": ""}
        if request["action"] == "restart_workers":           # P11: a model switch moved `current`
            report = {"ok": True, "error": None,
                      **self.restart_for_new_config(request["workers"], request["confirm"],
                                                    request["reason"] or "new config")}
        else:
            report = self.reload(confirm=request["confirm"])
        if self.reload_channel:
            self.reload_channel.write_result({"id": request["id"], **report})

    def reload(self, confirm: bool = False) -> dict:
        """Render site.yaml again and restart only the workers whose rendered file was written.
        restart_cost: high workers restart only with confirm or inside reload_quiet_window;
        otherwise they are marked config_pending. A site.yaml error changes nothing."""
        log.info("Reload requested%s", " (confirmed)" if confirm else "")
        result = self.site_env.render()
        report = {"ok": result.ok, "error": result.error, "changes": result.changes, "restarted": [],
                  "pending": [], "not_running": [], "not_supervised": [], "removed": result.ids("removed")}
        if not result.ok:
            log.error("Reload: nothing changed, site.yaml problem: %s", result.error)
            self._publish_supervisor_event("config_reload_failed", error=result.error)
            return report
        self.process_manager.rendered_dir = result.rendered_dir

        to_restart = set(result.ids("written"))
        if confirm:
            to_restart |= {w.id for w in self.workers.values() if w.config_pending}
        report.update(self.restart_for_new_config(sorted(to_restart), confirm, "config reload"))
        for worker_id in report["removed"]:
            if worker_id in self.workers:
                log.warning("[%s] its rendered config was removed; it keeps its old settings until it is "
                            "taken out of workers.yaml", worker_id)

        log.info("Reload done: %s; restarted: %s; waiting: %s", _change_summary(result.changes) or "no files",
                 ", ".join(report["restarted"]) or "-", ", ".join(report["pending"]) or "-")
        self._publish_supervisor_event("config_reloaded", **{k: v for k, v in report.items() if k != "error"})
        return report

    def restart_for_new_config(self, worker_ids, confirm: bool, reason: str) -> dict:
        """Restart workers whose config changed (rendered file, or a model version, P11), each once.
        restart_cost: high waits for the quiet window or confirm (D-070): it is marked
        config_pending and restarted later. A worker that is not running reads the new config
        at its next start; reload and model switches never start a stopped worker."""
        report = {"restarted": [], "pending": [], "not_running": [], "not_supervised": []}
        quiet = self._in_quiet_window()
        for worker_id in sorted(dict.fromkeys(worker_ids), key=self._start_position):
            worker = self.workers.get(worker_id)
            if worker is None:
                report["not_supervised"].append(worker_id)
            elif not self.process_manager.is_running(worker):
                report["not_running"].append(worker_id)
            elif worker.config.restart_cost == "high" and not (confirm or quiet):
                worker.config_pending = True
                # reload --confirm restarts every config_pending worker, also one pending after a model switch
                worker.message = f"{reason}: restart waiting for a quiet moment (or: supervisor.py reload --confirm)"
                report["pending"].append(worker_id)
            else:
                self._run_command(worker, "restart", reason)
                report["restarted"].append(worker_id)
        return report

    def _start_position(self, worker_id: str) -> int:
        order = list(self.workers)
        return order.index(worker_id) if worker_id in order else len(order)

    def _in_quiet_window(self, now: float = None) -> bool:
        window = self.settings.monitoring.reload_quiet_window
        if not window:
            return False
        (h1, m1), (h2, m2) = parse_quiet_window(window)
        t = time.localtime(time.time() if now is None else now)
        minute, start, end = t.tm_hour * 60 + t.tm_min, h1 * 60 + m1, h2 * 60 + m2
        return start <= minute < end if start <= end else (minute >= start or minute < end)

    def _restart_pending_in_quiet_window(self):
        pending = [w for w in self.workers.values() if w.config_pending]
        if not pending or not self._in_quiet_window():
            return
        for worker in pending:
            if self.process_manager.is_running(worker):
                log.info("[%s] quiet window: restarting for its new config", worker.id)
                self._run_command(worker, "restart", "config reload (quiet window)")
            worker.config_pending = False

    def _publish_supervisor_event(self, event: str, **data):
        """dpc/supervisor/event (append-only, CONTRACTS 2). Holds ids, statuses and safe error text only."""
        self.mqtt.publish(self.mqtt.topic("supervisor", "event"), {"event": event, "timestamp": now_iso(), **data})

    def _publish_presence_if_due(self):
        """F28: "online" (retained) again every status_publish_interval_seconds, so a stale
        "offline" on dpc/supervisor/status never outlives one interval while we run.
        F59: the DISABLED statuses go out with it. Only once connected: a QoS-0 message sent
        before the connection is up is dropped, so until then this retries every tick."""
        now = time.time()
        if now < self._next_presence:
            return
        connected = getattr(self.mqtt, "connected", None)          # threading.Event on the real client
        if connected is not None and not connected.is_set():
            return
        self.mqtt.publish_online()
        self._publish_disabled()
        self._next_presence = now + self.settings.monitoring.status_publish_interval_seconds

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
    def _start(self, worker: Worker, unmet: str = "", reason: str = "") -> bool:
        ok = self.process_manager.start_worker(worker)
        if ok:
            extra = {"start_after_unmet": unmet} if unmet else {}
            if reason:
                extra["reason"] = reason
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
        if command in ("start", "stop"):
            worker.paused_by = None                       # the operator decides from now on
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
    sup.reload_channel = ReloadChannel(config.settings.runtime_root)
    signal.signal(signal.SIGINT, sup.request_stop)
    signal.signal(signal.SIGTERM, sup.request_stop)
    signal.signal(signal.SIGHUP, sup.request_reload)      # supervisor.py reload, systemctl reload
    sup.reload_channel.write_pid()
    try:
        sup.run()
    finally:
        sup.reload_channel.remove_pid()


def cmd_reload(config, confirm):
    try:
        result = ReloadChannel(config.settings.runtime_root).request(confirm)
    except ReloadError as e:
        print(f"Reload not done: {e}")
        print("(To only render the files: python3 scripts/render_config.py)")
        return 3
    if not result["ok"]:
        print(f"site.yaml problem, nothing changed: {result['error']}")
        return 1
    changes = result["changes"]
    print(f"site.yaml OK: {_change_summary(changes) or 'no rendered files'}")
    lines = [("restarted", result["restarted"]),
             ("waiting for a quiet moment (reload --confirm restarts them now)", result["pending"]),
             ("not running now (they read the new file when they start)", result["not_running"]),
             ("written but not in workers.yaml", result["not_supervised"]),
             ("removed (worker keeps its old settings)", result["removed"])]
    for label, ids in lines:
        if ids:
            print(f"  {label}: {', '.join(ids)}")
    if not any(ids for _, ids in lines):
        print("  nothing to restart")
    return 0


def _change_summary(changes: dict) -> str:
    """{"cam1": "unchanged", "cam2": "written"} -> "1 written, 1 unchanged" (written first)."""
    statuses = list(changes.values())
    order = ["written", "unchanged", "removed"]
    order += sorted(set(statuses) - set(order))
    return ", ".join(f"{statuses.count(st)} {st}" for st in order if st in statuses)


def find_workers_file(site_env: SiteEnv):
    """F19: the workers file when -c is not given.

    On a site it is <root>/config/workers.yaml, the folder that also holds site.yaml
    (--site-config, then $DPC_SITE_CONFIG, then ~/INSIGHTZZ/config/site.yaml; CONTRACTS 4.1).
    The pid and reload files are then found through that file's runtime.root (<root>/run).
    The example with dummy workers is used only on a machine without that folder
    (development); a site folder without workers.yaml is an error, never the example.
    Returns (path, None) or (None, error text)."""
    site_yaml = site_env.site_yaml_path() or os.path.expanduser("~/INSIGHTZZ/config/site.yaml")
    config_dir = os.path.dirname(site_yaml)
    site_file = os.path.join(config_dir, "workers.yaml")
    if os.path.isfile(site_file):
        return site_file, None
    if os.path.isdir(config_dir):
        return None, (f"no workers.yaml in {config_dir}. Copy one there (for two Basler cameras: "
                      f"supervisor/config/workers.basler-2cam.yaml) or pass -c <file>.")
    print(f"(no {config_dir} on this machine: using the example {EXAMPLE_CONFIG})", file=sys.stderr)
    return EXAMPLE_CONFIG, None


def _cli_mqtt(config, site_env):
    """An MQTT client for the command line, with the broker login from site.yaml (F11)."""
    try:
        login = site_env.mqtt_login()
    except SiteEnvError as e:
        # stderr: a remark, not a result (stdout of `status --json` must stay pure JSON, F59)
        print(f"(site.yaml not read: {e}; trying the broker without a login)", file=sys.stderr)
        login = (None, None)
    # no announce_presence: a CLI call never touches dpc/supervisor/status (F28)
    return MqttClient(config.settings.mqtt, client_id=f"dpc-ctl-{os.getpid()}", login=login)


def cmd_worker_command(config, site_env, command, worker_id):
    """start/stop/restart: publish an MQTT command to the running supervisor."""
    client = _cli_mqtt(config, site_env)
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


def cmd_status(config, site_env, as_json=False):
    """Read the retained status messages the supervisor publishes.
    --json (F59, for the UI): {"supervisor": <dpc/supervisor/status> or null, "workers": {id: status}};
    errors as {"error": ...}. Exit 0 = statuses read, 1 = broker or Supervisor not reached."""
    client = _cli_mqtt(config, site_env)
    found, supervisor = {}, {}

    def on_worker_status(topic, payload):
        if isinstance(payload, dict) and isinstance(payload.get("worker_id"), str):
            found[payload["worker_id"]] = payload

    client.subscribe(client.worker_topic("+", "status"), on_worker_status)
    client.subscribe(client.topic("supervisor", "status"), lambda t, p: supervisor.update(p if isinstance(p, dict) else {}))
    client.start()
    if not client.wait_connected(3):
        return _status_error("Cannot reach the MQTT broker - is it running?", as_json)
    time.sleep(1.5)
    client.stop()
    if as_json:
        print(json.dumps({"supervisor": supervisor or None, "workers": dict(sorted(found.items()))}, indent=1))
        return 0 if found else 1
    if not found:
        print("No status received (is the supervisor running?)")
        return 1
    print(f"{'WORKER':<20}{'STATUS':<17}{'PID':<8}{'RESTARTS':<10}MESSAGE")
    for wid, s in sorted(found.items()):
        print(f"{wid:<20}{s['status']:<17}{str(s['pid'] or '-'):<8}{s['restart_count']:<10}{s['message']}")
    return 0


def _print_config_error(text, as_json):
    print(json.dumps({"error": text}) if as_json else text)


def _status_error(text, as_json) -> int:
    print(json.dumps({"error": text}) if as_json else text)
    return 1


# ======================================================================
# model commands (P11; D-056, D-060, D-061, D-086; CONTRACTS 12)
# ======================================================================
MODEL_ARGS = {"status": 0, "switch": 2, "rollback": 1, "pin": 2, "unpin": 1}


def cmd_model(argv) -> int:
    """`supervisor.py model ...`. The checks, the self-test and moving `current` happen here;
    restarting the inspection sides is asked of the running Supervisor (reload channel), which
    applies the restart_cost: high rules. Exit 0 done, 1 refused (nothing changed), 2 config error."""
    p = argparse.ArgumentParser(prog="supervisor.py model", description="Model versions, per model name")
    p.add_argument("command", choices=list(MODEL_ARGS))
    p.add_argument("args", nargs="*", help="switch/pin: <name> <version>; rollback/unpin: <name>")
    p.add_argument("--confirm", action="store_true",
                   help="restart the inspection sides now (they lose their wheel/plate state)")
    p.add_argument("--json", action="store_true", help="status as JSON")
    p.add_argument("-c", "--config", default=None)
    p.add_argument("--site-config", default=None)
    p.add_argument("--selftest-script", default=DEFAULT_SELFTEST, help=argparse.SUPPRESS)  # tests
    args = p.parse_args(argv)
    if len(args.args) != MODEL_ARGS[args.command]:
        p.error(f"'model {args.command}' needs {MODEL_ARGS[args.command]} argument(s)")

    as_json = args.json and args.command == "status"
    site_env = SiteEnv(args.site_config)
    config, problem = _load_workers_file(args.config, site_env)
    if problem:
        print(json.dumps({"error": f"Configuration error: {problem}"}) if as_json else f"Configuration error: {problem}")
        return 2
    try:
        manager = ModelManager(site_env.load(), config.settings.runtime_root, args.selftest_script,
                               _inspection_python(config))
    except SiteEnvError as e:
        print(json.dumps({"error": f"Site config error: {e}"}) if as_json else f"Site config error: {e}")
        return 2

    if args.command == "status":
        return _print_model_status(manager, config, args.json)

    name = args.args[0]
    try:
        with manager.lock():
            if args.command == "switch":
                print(f"Checking {name} {args.args[1]}: package, then the self-test (this can take a minute)...")
                result = manager.switch(name, args.args[1])
            elif args.command == "rollback":
                result = manager.rollback(name)
            elif args.command == "pin":
                result = manager.pin(name, args.args[1])
            else:
                result = manager.unpin(name)
    except ModelCommandError as e:
        print(f"Refused, nothing changed: {e}")
        if args.command in ("switch", "rollback"):
            _publish_model_event(config, site_env, "model_switch_failed", model=name, action=args.command,
                                 to=args.args[1] if args.command == "switch" else None, reason=str(e))
        return 1

    if args.command in ("pin", "unpin"):
        what = f"pinned to {result['version']}" if args.command == "pin" else f"unpinned (was {result['version']})"
        print(f"{name} {what}")
        if args.command == "pin" and result["current"] != result["version"]:
            print(f"  note: current is {result['current']}; a pinned model is never switched automatically")
        _publish_model_event(config, site_env, f"model_{args.command}ned", **result)
        return 0

    print(f"{name}: current is now {result['to']} (was {result['from'] or 'none'}); used by "
          f"{', '.join(result['sides'])}")
    restart = _restart_for_model(config, result, args.confirm)
    event = "model_switched" if args.command == "switch" else "model_rolled_back"
    result.pop("selftest", None)
    _publish_model_event(config, site_env, event, **result, **restart)
    return 0


def _load_workers_file(path, site_env):
    if path is None:
        path, problem = find_workers_file(site_env)
        if problem:
            return None, problem
    config = ConfigurationManager(path, BASE_DIR)
    try:
        config.load()
    except ConfigError as e:
        return None, str(e)
    return config, None


def _inspection_python(config) -> str:
    """The Python the inspection workers run with (first word of their command, found on PATH),
    so the self-test loads Detectron2 exactly as inspection does. Else this Python."""
    for worker in config.workers:
        if worker.id.startswith("inspection_"):
            first = shlex.split(worker.command)[0]
            if os.path.basename(first).startswith("python"):
                return shutil.which(first) or sys.executable
    return sys.executable


def _restart_for_model(config, result, confirm) -> dict:
    """Ask the running Supervisor to restart the sides that run this model (once each)."""
    try:
        report = ReloadChannel(config.settings.runtime_root).request(
            confirm, action="restart_workers", workers=result["workers"],
            reason=f"model {result['model']} {result['to']}")
    except ReloadError as e:
        print(f"  Supervisor not reached ({e}): the inspection sides use {result['to']} when they start")
        return {"restarted": [], "pending": [], "not_running": list(result["workers"])}
    labels = (("restarted", "restarted"),
              ("pending", "waiting for the quiet window (or: supervisor.py reload --confirm)"),
              ("not_running", "not running now (they load the new version when they start)"),
              ("not_supervised", "not in workers.yaml"))
    for key, label in labels:
        if report.get(key):
            print(f"  {label}: {', '.join(report[key])}")
    return {k: report.get(k, []) for k in ("restarted", "pending", "not_running")}


def _print_model_status(manager, config, as_json) -> int:
    rows = manager.status()
    if as_json:
        print(json.dumps({"auto_update": config.settings.models.auto_update, "models": rows}, indent=1))
        return 0
    print(f"auto_update: {config.settings.models.auto_update}")
    for row in rows:
        installed = ", ".join(f"{v['version']}{'' if v['ready'] else ' (not ready)'}" for v in row["installed"])
        print(f"{row['model']}  (sides {', '.join(row['sides'])}: {', '.join(row['workers'])})")
        print(f"  current: {row['current'] or '-'}   previous: {row['previous'] or '-'}   "
              f"pinned: {row['pinned'] or '-'}")
        print(f"  installed: {installed or '-'}")
        for problem in row.get("problems", []):
            print(f"  problem: {problem}")
    if not rows:
        print("no model in use (inspection.model in site.yaml)")
    return 0


def _publish_model_event(config, site_env, event, **data):
    """dpc/supervisor/event (CONTRACTS 2), best effort: the change is done even if the broker is down."""
    if not config.settings.mqtt.enabled:
        return
    client = _cli_mqtt(config, site_env)
    client.start()
    if not client.wait_connected(3):
        print(f"  (MQTT broker not reached: event {event} not published)")
        return
    client.publish(client.topic("supervisor", "event"), {"event": event, "timestamp": now_iso(), **data})
    time.sleep(0.5)
    client.stop()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "model":
        return cmd_model(sys.argv[2:])
    parser = argparse.ArgumentParser(description="DPC Supervisor")
    parser.add_argument("action", choices=["run", "check-config", "reload", "status", "start", "stop", "restart"])
    parser.add_argument("worker_id", nargs="?", help="needed for start/stop/restart")
    parser.add_argument("-c", "--config", default=None,
                        help="workers.yaml (default: <root>/config/workers.yaml next to site.yaml)")
    parser.add_argument("--site-config", default=None,
                        help="site.yaml (default: $DPC_SITE_CONFIG, then ~/INSIGHTZZ/config/site.yaml)")
    parser.add_argument("--confirm", action="store_true",
                        help="reload: also restart restart_cost: high workers now (inspection loses its wheel state)")
    parser.add_argument("--json", action="store_true", help="status: machine-readable output (for the UI)")
    args = parser.parse_args()

    site_env = SiteEnv(args.site_config)
    workers_file = args.config
    if workers_file is None:
        workers_file, problem = find_workers_file(site_env)
        if problem:
            _print_config_error(f"Configuration error: {problem}", args.json)
            return 2

    config = ConfigurationManager(workers_file, BASE_DIR)
    try:
        config.load()
    except ConfigError as e:
        _print_config_error(f"Configuration error: {e}", args.json)
        return 2

    if args.action == "check-config":
        print(f"OK - {workers_file}")
        print(f"OK - {len(config.workers)} enabled worker(s) in start order: "
              f"{', '.join(w.id for w in config.start_order)}")
        users = [w.id for w in config.workers if w.env_from_site]
        site_yaml = site_env.site_yaml_path()
        if users or (site_yaml and os.path.exists(site_yaml)):
            problem = site_env.check()        # prints the loader's message, never a value
            if problem:
                needed = f" (needed by {', '.join(users)})" if users else ""
                print(f"Site config error{needed}: {problem}")
                return 2
            print("OK - site.yaml loads (rendered configs, MQTT login)")
            if users:
                print(f"OK - DPC_* variables are exported to: {', '.join(users)}")
        else:
            print(f"Note - no site.yaml at {site_yaml}: nothing is rendered, MQTT connects without a login")
        return 0
    if args.action == "run":
        cmd_run(config, site_env)
        return 0
    if args.action == "reload":
        return cmd_reload(config, args.confirm)
    if args.action == "status":
        return cmd_status(config, site_env, args.json)
    if not args.worker_id:
        parser.error(f"'{args.action}' needs a worker_id")
    return cmd_worker_command(config, site_env, args.action, args.worker_id)


if __name__ == "__main__":
    sys.exit(main())
