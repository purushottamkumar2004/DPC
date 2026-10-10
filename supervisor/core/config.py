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
    type: str = "process"              # process | file_activity | heartbeat
    path: Optional[object] = None      # file_activity: a file/dir, or a list of them
    max_age_seconds: float = 30.0      # file_activity / heartbeat: how old is "too old"
    startup_grace_seconds: float = 30.0  # ignore health failures this long after a start


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


@dataclass
class MonitoringSettings:
    check_interval_seconds: float = 5.0
    worker_timeout_seconds: float = 10.0     # a failed check must persist this long before restart
    startup_stagger_seconds: float = 1.0     # pause between launching workers at startup
    status_publish_interval_seconds: float = 30.0


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


class ConfigurationManager:
    VALID_HEALTH_TYPES = ("process", "file_activity", "heartbeat")

    def __init__(self, config_path: str, base_dir: str):
        self.config_path = config_path
        self.base_dir = base_dir          # relative paths in the config are relative to this
        self.settings: Optional[Settings] = None
        self.workers: List[WorkerConfig] = []   # enabled workers only, in file order

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

        self.workers = []
        seen = set()
        for i, item in enumerate(raw.get("workers") or []):
            worker = self._parse_worker(item, restart_defaults, i)
            if worker.id in seen:
                raise ConfigError(f"Duplicate worker id: {worker.id}")
            seen.add(worker.id)
            if worker.enabled:
                self.workers.append(worker)

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
        if not hasattr(signal, worker.stop_signal):
            raise ConfigError(f"{where}: unknown stop_signal '{worker.stop_signal}'")

        worker.working_directory = self._abs(worker.working_directory) or self.base_dir
        worker.env = {str(k): str(v) for k, v in (worker.env or {}).items()}
        return worker
