"""Settings of the PLC service (STATUS "PLC service", D-024), read ONLY from its rendered JSON.

File: env DPC_WORKER_CONFIG = <root>/config/rendered/plc.json (CONTRACTS 13, D-062).

Rules:
- The file itself must exist. Without it the program stops (last_error, exit 1): falling back
  to built-in IP and registers could write Tadipatri registers to another site's PLC.
- A key missing INSIDE the file gets a built-in default equal to the Tadipatri value, with ONE
  warning per key (same approach as inspection F22 and cleanup). Defaults live only in DEFAULTS.
- `mqtt` absent: no MQTT (no status, no commands, one warning). `db` absent: the
  CAMERA_PLC_STATUS.PLC lamp is not written (one warning).
- A key that is present but invalid stops the program with the key's name.
"""

import json
import os
from dataclasses import dataclass, field
from typing import List, Optional

WORKER_ID = "plc"
CODE_DIR = os.path.dirname(os.path.abspath(__file__))

# Tadipatri values (docs/SITES.md). 1 = ON, 0 = OFF for light and cleaning (developer, 2026-11-02).
DEFAULTS = {
    "plc.type": "modbus_tcp",
    "plc.ip": "192.168.1.5",
    "plc.port": 502,
    "plc.timeout_s": 3.0,
    "plc.light_register": 4109,
    "plc.light_on_value": 1,
    "plc.cleaning_register": 4106,
    "plc.conveyor_status_register": 4098,
    "plc.cleaning_pulse_seconds": 10.0,
    "plc.light_check_seconds": 60.0,        # new: read-verify the light, rewrite if it differs
    "plc.status_interval_seconds": 10.0,    # new: conveyor status read + MQTT status
    "plc.cleaning_cooldown_seconds": 600.0, # new: minimum gap between AUTOMATIC pulses
    "plc.auto_cleaning": False,             # new: act on `poor_view` events (off until MASTER decides)
    "log.backup_count": 5,
}
SUPPORTED_TYPES = ("modbus_tcp",)


class ConfigError(Exception):
    pass


@dataclass
class MqttSettings:
    host: str
    port: int
    user: Optional[str] = None
    password: Optional[str] = field(default=None, repr=False)


@dataclass
class DbSettings:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)


@dataclass
class Settings:
    worker_id: str
    root: str
    logs: str
    state: str
    backup_count: int
    plc_type: str
    ip: str
    port: int
    timeout_s: float
    light_register: int
    light_on_value: int
    cleaning_register: int
    conveyor_status_register: int
    cleaning_pulse_seconds: float
    light_check_seconds: float
    status_interval_seconds: float
    cleaning_cooldown_seconds: float
    auto_cleaning: bool
    mqtt: Optional[MqttSettings]
    db: Optional[DbSettings]
    warnings: List[str] = field(default_factory=list)

    @property
    def writable_registers(self):
        """The ONLY registers this program may ever write."""
        return {self.light_register, self.cleaning_register}

    def secrets(self) -> List[str]:
        values = []
        if self.mqtt and self.mqtt.password:
            values.append(self.mqtt.password)
        if self.db and self.db.password:
            values.append(self.db.password)
        return values


def root_from_code_location() -> str:
    """<root>/code/workers/plc -> <root> (CONTRACTS 4.1); used only to place last_error."""
    return os.path.dirname(os.path.dirname(os.path.dirname(CODE_DIR)))


def read_raw_config() -> dict:
    path = os.environ.get("DPC_WORKER_CONFIG", "")
    if not path:
        raise ConfigError("DPC_WORKER_CONFIG is not set (plc.json not rendered yet)")
    if not os.path.isfile(path):
        raise ConfigError(f"rendered config not found: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot read {path}: {e}")
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a JSON object")
    return data


class _Reader:
    """Reads dotted keys; collects one warning per key that falls back to DEFAULTS."""

    def __init__(self, data: dict):
        self.data = data
        self.warnings: List[str] = []

    def raw(self, key: str):
        node = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def get(self, key: str, kind):
        value = self.raw(key)
        if value is None:
            value = DEFAULTS[key]
            self.warnings.append(f"plc.json: '{key}' missing, using default {value!r}")
        if kind is bool:
            if not isinstance(value, bool):
                raise ConfigError(f"'{key}' must be true or false")
            return value
        if kind in (int, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"'{key}' must be a number")
            if kind is int and int(value) != value:
                raise ConfigError(f"'{key}' must be an integer")
            return kind(value)
        if not isinstance(value, str) or not value:
            raise ConfigError(f"'{key}' must be text")
        return value


def _register(r: _Reader, key: str) -> int:
    value = r.get(key, int)
    if not 0 <= value <= 65535:
        raise ConfigError(f"'{key}' must be 0..65535, got {value}")
    return value


def _positive(r: _Reader, key: str) -> float:
    value = r.get(key, float)
    if value <= 0:
        raise ConfigError(f"'{key}' must be > 0, got {value}")
    return value


def _mqtt(data: dict) -> Optional[MqttSettings]:
    m = data.get("mqtt")
    if m is None:
        return None
    if not isinstance(m, dict) or not isinstance(m.get("host"), str):
        raise ConfigError("'mqtt' must be {host, port, user, password}")
    port = m.get("port", 1883)
    if isinstance(port, bool) or not isinstance(port, int):
        raise ConfigError("'mqtt.port' must be an integer")
    return MqttSettings(m["host"], port, m.get("user") or None, m.get("password") or None)


def _db(data: dict) -> Optional[DbSettings]:
    d = data.get("db")
    if d is None:
        return None
    try:
        return DbSettings(str(d["host"]), int(d.get("port", 3306)), str(d["name"]),
                          str(d["user"]), str(d.get("password") or ""))
    except (KeyError, TypeError, ValueError):
        raise ConfigError("'db' must be {host, port, name, user, password}")


def build_settings(data: dict) -> Settings:
    r = _Reader(data)
    paths = data.get("paths") if isinstance(data.get("paths"), dict) else {}
    root = paths.get("root") or root_from_code_location()
    if not paths.get("root"):
        r.warnings.append(f"plc.json: 'paths.root' missing, using {root}")

    plc_type = r.get("plc.type", str)
    if plc_type not in SUPPORTED_TYPES:
        raise ConfigError(f"'plc.type' {plc_type!r} is not supported (only {SUPPORTED_TYPES})")
    light_on_value = r.get("plc.light_on_value", int)
    if light_on_value not in (0, 1):
        raise ConfigError(f"'plc.light_on_value' must be 0 or 1, got {light_on_value}")

    s = Settings(
        worker_id=data.get("worker_id") or WORKER_ID,
        root=root,
        logs=paths.get("logs") or os.path.join(root, "logs"),
        state=paths.get("state") or os.path.join(root, "data", "state"),
        backup_count=r.get("log.backup_count", int),
        plc_type=plc_type,
        ip=r.get("plc.ip", str),
        port=_register(r, "plc.port"),
        timeout_s=_positive(r, "plc.timeout_s"),
        light_register=_register(r, "plc.light_register"),
        light_on_value=light_on_value,
        cleaning_register=_register(r, "plc.cleaning_register"),
        conveyor_status_register=_register(r, "plc.conveyor_status_register"),
        cleaning_pulse_seconds=_positive(r, "plc.cleaning_pulse_seconds"),
        light_check_seconds=_positive(r, "plc.light_check_seconds"),
        status_interval_seconds=_positive(r, "plc.status_interval_seconds"),
        cleaning_cooldown_seconds=r.get("plc.cleaning_cooldown_seconds", float),
        auto_cleaning=r.get("plc.auto_cleaning", bool),
        mqtt=_mqtt(data),
        db=_db(data),
    )
    if s.light_register == s.cleaning_register:
        raise ConfigError(f"light_register and cleaning_register are both {s.light_register}")
    if s.conveyor_status_register in s.writable_registers:
        r.warnings.append(f"conveyor_status_register {s.conveyor_status_register} equals a register "
                          "this program writes; the status read returns that register's value")
    if s.mqtt is None:
        r.warnings.append("plc.json: no 'mqtt' block: no status, heartbeat or commands over MQTT")
    if s.db is None:
        r.warnings.append("plc.json: no 'db' block: CAMERA_PLC_STATUS.PLC is not written")
    s.warnings = r.warnings
    return s


def secrets_in(data: dict) -> List[str]:
    """Secret values in a raw config, for last_error scrubbing even when building fails."""
    found = []
    for block in ("mqtt", "db"):
        value = (data.get(block) or {}).get("password") if isinstance(data.get(block), dict) else None
        if isinstance(value, str) and value:
            found.append(value)
    return found
