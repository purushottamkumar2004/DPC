"""
ConfigurationManager - reads workers.yaml (or .json) and turns it into
plain Python objects.

The rest of the Supervisor never touches YAML directly. Later, if you want
to read the configuration from Django/MySQL, only this file changes:
just make load() build the same WorkerConfig / Settings objects from the DB.
"""

import json
import os
import signal
from dataclasses import dataclass, field, fields
from typing import Dict, List, Optional

try:
    import yaml
except ImportError:  # only needed for .yaml files
    yaml = None


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


# ----------------------------------------------------------------------
# Plain data containers
# ----------------------------------------------------------------------
@dataclass
class RestartPolicy:
    enabled: bool = True
    max_retries: int = 3               # after this many failed recoveries -> FAILED
    retry_delay_seconds: float = 5.0   # wait before each restart
    reset_after_seconds: float = 300.0 # healthy this long -> retry counter goes back to 0


@dataclass
class HealthConfig:
    type: str = "process"              # process | file_activity | heartbeat | queue_lag
    path: Optional[object] = None      # file_activity / queue_lag: a file/dir, or a list of them
    max_age_seconds: float = 30.0      # file_activity / heartbeat: how old is "too old"
    startup_grace_seconds: float = 30.0  # ignore health failures this long after a start
    # queue_lag (D-020): the oldest file in the queue folder (INF) may be this old
    max_oldest_age_seconds: Optional[float] = None
    # queue_lag + heartbeat (D-049, Q1): when true the worker is healthy only if the queue
    # lag is small AND a fresh heartbeat arrived (max_age_seconds is the heartbeat limit)
    require_heartbeat: bool = False


@dataclass
class WorkerConfig:
    id: str
    type: str
    command: str
    enabled: bool = True
    working_directory: Optional[str] = None
    runtime_dir: Optional[str] = None            # folder name under runtime root (default: worker id)
    directories: List[str] = field(default_factory=list)
    stop_signal: str = "SIGTERM"                 # use SIGINT for scripts that only catch Ctrl+C
    stop_timeout_seconds: float = 10.0
    env: Dict[str, str] = field(default_factory=dict)
    restart: RestartPolicy = field(default_factory=RestartPolicy)
    health: HealthConfig = field(default_factory=HealthConfig)
    # ---- V2 (D-019, D-020, D-049, CONTRACTS 1 and 9) ----
    mode: str = "service"              # service | oneshot | periodic
    interval_s: Optional[float] = None  # periodic only: seconds between the START of two runs
    restart_cost: str = "normal"       # normal | high (algorithms: a restart loses wheel/plate state)
    start_after: List[str] = field(default_factory=list)   # worker ids that must be ready first
    env_from_site: bool = False        # export the DPC_* variables from site.yaml (CONTRACTS 9)
    # ---- P5 ----
    follows: Optional[str] = None      # D-068: run only while this worker is healthy (inspection follows its capture)
    run_timeout_seconds: Optional[float] = None   # F10, oneshot/periodic only: a run longer than this is stopped


@dataclass
class MonitoringSettings:
    check_interval_seconds: float = 5.0
    worker_timeout_seconds: float = 10.0     # a failed check must persist this long before restart
    startup_stagger_seconds: float = 1.0     # pause between launching workers at startup
    status_publish_interval_seconds: float = 30.0
    # restart_cost: high workers must fail this long before a restart (D-020)
    high_cost_timeout_seconds: float = 120.0
    # a worker with start_after waits at most this long for its dependencies, then starts anyway
    start_after_timeout_seconds: float = 120.0
    # 6f: "HH:MM-HH:MM" (may cross midnight). Inside it, restart_cost: high workers whose rendered
    # config changed are restarted; outside it they wait (or `reload --confirm`). None = no window.
    reload_quiet_window: Optional[str] = None


@dataclass
class MqttSettings:
    enabled: bool = True
    host: str = "localhost"
    port: int = 1883
    username: Optional[str] = None
    password: Optional[str] = None
    topic_prefix: str = "dpc"


@dataclass
class Settings:
    monitoring: MonitoringSettings
    mqtt: MqttSettings
    runtime_root: str
    log_dir: str


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _build(cls, data, where):
    """Create a dataclass from a dict, rejecting unknown keys (catches typos)."""
    data = data or {}
    unknown = set(data) - {f.name for f in fields(cls)}
    if unknown:
        raise ConfigError(f"{where}: unknown option(s): {', '.join(sorted(unknown))}")
    return cls(**data)


def _expand_home(value: str) -> str:
    """'~' or '~/...' in an env value becomes the home folder, like every path in this file.
    A worker gets the value as-is from its environment and would otherwise see a literal '~'
    (e.g. SOME_FILE: ~/INSIGHTZZ/data/x). Nothing else is expanded."""
    return os.path.expanduser(value) if value == "~" or value.startswith("~/") else value


def parse_quiet_window(text):
    """"02:00-03:30" -> ((2, 0), (3, 30)), or None when it is not that shape."""
    try:
        start, end = str(text).split("-")
        parts = []
        for hhmm in (start, end):
            hh, mm = hhmm.strip().split(":")
            hh, mm = int(hh), int(mm)
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                return None
            parts.append((hh, mm))
        return parts[0], parts[1]
    except ValueError:
        return None


def _require_positive_number(value, where):
    # bool is an int in Python: "interval_s: true" must not pass as 1
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{where}: must be a number greater than 0, got {value!r}")


class ConfigurationManager:
    VALID_HEALTH_TYPES = ("process", "file_activity", "heartbeat", "queue_lag")
    VALID_MODES = ("service", "oneshot", "periodic")
    VALID_RESTART_COSTS = ("normal", "high")

    def __init__(self, config_path: str, base_dir: str):
        self.config_path = config_path
        self.base_dir = base_dir          # relative paths in the config are relative to this
        self.settings: Optional[Settings] = None
        self.workers: List[WorkerConfig] = []   # enabled workers only, in file order
        self.start_order: List[WorkerConfig] = []  # the same workers, start_after respected

    # ------------------------------------------------------------------
    def load(self):
        raw = self._read_file()

        self.settings = Settings(
            monitoring=_build(MonitoringSettings, raw.get("monitoring"), "monitoring"),
            mqtt=_build(MqttSettings, raw.get("mqtt"), "mqtt"),
            runtime_root=self._abs((raw.get("runtime") or {}).get("root", "runtime")),
            log_dir=self._abs(raw.get("log_dir", "logs")),
        )

        restart_defaults = raw.get("restart_policy") or {}
        _build(RestartPolicy, restart_defaults, "restart_policy")  # validate only

        self._check_monitoring(self.settings.monitoring)
        self._check_mqtt(raw.get("mqtt") or {})

        self.workers = []
        all_ids = set()                 # enabled AND disabled: a start_after may name a disabled one
        for i, item in enumerate(raw.get("workers") or []):
            worker = self._parse_worker(item, restart_defaults, i)
            if worker.id in all_ids:
                raise ConfigError(f"Duplicate worker id: {worker.id}")
            all_ids.add(worker.id)
            if worker.enabled:
                self.workers.append(worker)

        self.start_order = self._resolve_start_order(all_ids)

    # ------------------------------------------------------------------
    @staticmethod
    def _check_monitoring(m: MonitoringSettings):
        for name in ("check_interval_seconds", "worker_timeout_seconds", "high_cost_timeout_seconds",
                     "start_after_timeout_seconds"):
            _require_positive_number(getattr(m, name), f"monitoring.{name}")
        if m.high_cost_timeout_seconds < m.worker_timeout_seconds:
            raise ConfigError("monitoring.high_cost_timeout_seconds must not be shorter than "
                              "worker_timeout_seconds (a high-cost restart is the slower one)")
        if m.reload_quiet_window is not None and parse_quiet_window(m.reload_quiet_window) is None:
            raise ConfigError(f"monitoring.reload_quiet_window must look like \"02:00-03:30\", "
                              f"got {m.reload_quiet_window!r}")

    @staticmethod
    def _check_mqtt(raw_mqtt: dict):
        """F11: the Supervisor's MQTT login is mqtt.user / mqtt.password in site.yaml (a secret
        belongs there, D-048). workers.yaml keeps enabled, host, port and topic_prefix only."""
        for key in ("username", "password"):
            if raw_mqtt.get(key) not in (None, ""):
                raise ConfigError(f"mqtt.{key} is no longer read from workers.yaml: put the broker login in "
                                  f"site.yaml (mqtt.user, mqtt.password) and remove it here")

    def _resolve_start_order(self, all_ids: set) -> List[WorkerConfig]:
        """File order, except that a worker comes after everything in its start_after.
        A start_after entry naming a disabled worker is ignored (a site can switch a worker
        off without editing the others). Unknown ids and loops are errors."""
        enabled = {w.id: w for w in self.workers}
        for w in self.workers:
            for dep in w.start_after:
                if dep == w.id:
                    raise ConfigError(f"worker '{w.id}': start_after cannot list the worker itself")
                if dep not in all_ids:
                    raise ConfigError(f"worker '{w.id}': start_after names unknown worker '{dep}'")
            w.start_after = [d for d in w.start_after if d in enabled]
            self._check_follows(w, enabled, all_ids)

        order: List[WorkerConfig] = []
        done = set()
        visiting: List[str] = []

        def visit(worker: WorkerConfig):
            if worker.id in done:
                return
            if worker.id in visiting:
                loop = visiting[visiting.index(worker.id):] + [worker.id]
                raise ConfigError("start_after/follows loop: " + " -> ".join(loop))
            visiting.append(worker.id)
            for dep in worker.start_after + ([worker.follows] if worker.follows else []):
                visit(enabled[dep])
            visiting.pop()
            done.add(worker.id)
            order.append(worker)

        for w in self.workers:
            visit(w)
        return order

    @staticmethod
    def _check_follows(w: WorkerConfig, enabled: dict, all_ids: set):
        """follows names one enabled service worker, and the follower is a service too
        (following a oneshot, or pausing a periodic job, has no meaning)."""
        if w.follows is None:
            return
        where = f"worker '{w.id}': follows"
        if w.follows == w.id:
            raise ConfigError(f"{where} cannot name the worker itself")
        if w.follows not in all_ids:
            raise ConfigError(f"{where} names unknown worker '{w.follows}'")
        if w.follows not in enabled:
            raise ConfigError(f"{where} names '{w.follows}', which is disabled: this worker could never start")
        if w.mode != "service" or enabled[w.follows].mode != "service":
            raise ConfigError(f"{where} only works between two service workers")

    # ------------------------------------------------------------------
    def _read_file(self) -> dict:
        if not os.path.isfile(self.config_path):
            raise ConfigError(f"Config file not found: {self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as f:
            if self.config_path.lower().endswith(".json"):
                return json.load(f) or {}
            if yaml is None:
                raise ConfigError("PyYAML is not installed (pip install pyyaml)")
            return yaml.safe_load(f) or {}

    def _abs(self, path: Optional[str]) -> Optional[str]:
        if path is None:
            return None
        path = os.path.expanduser(str(path))
        return path if os.path.isabs(path) else os.path.normpath(os.path.join(self.base_dir, path))

    def _parse_worker(self, item: dict, restart_defaults: dict, index: int) -> WorkerConfig:
        where = f"workers[{index}]"
        item = dict(item or {})
        for required in ("id", "type", "command"):
            if not item.get(required):
                raise ConfigError(f"{where}: '{required}' is required")
        where = f"worker '{item['id']}'"

        # restart: true/false  OR  a dict that overrides the global restart_policy
        restart = item.pop("restart", None)
        if restart is None:
            restart = {}
        elif isinstance(restart, bool):
            restart = {"enabled": restart}
        item["restart"] = _build(RestartPolicy, {**restart_defaults, **restart}, where + " restart")

        item["health"] = _build(HealthConfig, item.get("health"), where + " health")
        worker = _build(WorkerConfig, item, where)

        # ---- validation / path resolving ----
        if worker.health.type not in self.VALID_HEALTH_TYPES:
            raise ConfigError(f"{where}: health.type must be one of {self.VALID_HEALTH_TYPES}")
        if worker.health.type == "file_activity":
            if not worker.health.path:
                raise ConfigError(f"{where}: health.path is required for file_activity")
            paths = worker.health.path
            paths = paths if isinstance(paths, list) else [paths]
            worker.health.path = [self._abs(p) for p in paths]
        if worker.health.type == "queue_lag":
            if not worker.health.path:
                raise ConfigError(f"{where}: health.path (the queue folder, e.g. INF) is required for queue_lag")
            _require_positive_number(worker.health.max_oldest_age_seconds,
                                     f"{where}: health.max_oldest_age_seconds (required for queue_lag)")
            paths = worker.health.path
            paths = paths if isinstance(paths, list) else [paths]
            worker.health.path = [self._abs(p) for p in paths]
        elif worker.health.max_oldest_age_seconds is not None or worker.health.require_heartbeat:
            raise ConfigError(f"{where}: max_oldest_age_seconds and require_heartbeat only apply "
                              f"to health.type queue_lag")
        if not isinstance(worker.health.require_heartbeat, bool):
            raise ConfigError(f"{where}: health.require_heartbeat must be true or false")
        if not hasattr(signal, worker.stop_signal):
            raise ConfigError(f"{where}: unknown stop_signal '{worker.stop_signal}'")

        # ---- V2 keys ----
        if worker.mode not in self.VALID_MODES:
            raise ConfigError(f"{where}: mode must be one of {self.VALID_MODES}, got {worker.mode!r}")
        if worker.mode == "periodic":
            _require_positive_number(worker.interval_s, f"{where}: interval_s (required for mode periodic)")
        elif worker.interval_s is not None:
            raise ConfigError(f"{where}: interval_s only applies to mode periodic")
        if worker.restart_cost not in self.VALID_RESTART_COSTS:
            raise ConfigError(f"{where}: restart_cost must be one of {self.VALID_RESTART_COSTS}, "
                              f"got {worker.restart_cost!r}")
        if not isinstance(worker.env_from_site, bool):
            raise ConfigError(f"{where}: env_from_site must be true or false")
        deps = worker.start_after
        if isinstance(deps, str):                       # start_after: one_id  is allowed
            deps = [deps]
        if not isinstance(deps, list) or not all(isinstance(d, str) and d for d in deps):
            raise ConfigError(f"{where}: start_after must be a worker id or a list of worker ids")
        worker.start_after = list(dict.fromkeys(deps))   # drop duplicates, keep order
        if worker.follows is not None and (not isinstance(worker.follows, str) or not worker.follows):
            raise ConfigError(f"{where}: follows must be one worker id")
        if worker.run_timeout_seconds is not None:
            if worker.mode == "service":
                raise ConfigError(f"{where}: run_timeout_seconds only applies to mode oneshot or periodic")
            _require_positive_number(worker.run_timeout_seconds, f"{where}: run_timeout_seconds")

        worker.working_directory = self._abs(worker.working_directory) or self.base_dir
        worker.env = {str(k): _expand_home(str(v)) for k, v in (worker.env or {}).items()}
        return worker
