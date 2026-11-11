"""PLC test functions for the UI setup wizard (STATUS S5, D-103 step 4 "PLC").

Functions (each returns a result dict, never raises for PLC trouble):
    check(t)                      read the given registers; writes NOTHING
    light_test(t, state, seconds) light on/off for `seconds`, then back to light_on_value
    pulse_test(t)                 one cleaning pulse: 1, wait cleaning_pulse_seconds, 0

CLI: through plc_worker.py (see its docstring), e.g.
    python3 plc_worker.py --check --json
    python3 plc_worker.py --light off --seconds 5 --ip 192.168.1.5 --light-register 4109 --json
    python3 plc_worker.py --pulse --ip 192.168.1.5 --cleaning-register 4106 --json

Settings: plc.json (env DPC_WORKER_CONFIG) when it exists, every value overridable on the
command line, so the wizard can test before site.yaml exists. IP and registers have NO
built-in defaults here: a test only touches what it was given.

Safety:
- Each test may write ONE register only: the light test the light register, the pulse test
  the cleaning register (the PlcLink write gate gets just that register; check gets none).
- The light is always put back to light_on_value and the cleaning register back to 0, also on
  SIGTERM / Ctrl-C during the wait (the CLI sets the stop event); failing that is reported as
  status "restore_failed" with a clear message.
- If the PLC service runs at the same time it rewrites the light within light_check_seconds,
  and a test pulse ending writes 0 even if the service's own pulse is running: run the tests
  in setup mode or with the service stopped for exact results.

Result: {"ok", "status", "action", "message", "plc": {type, ip, port}, "settings_from", ...}
Status (CLI exit code): ok (0), failed (1), restore_failed (1), bad_request (2), unreachable (3).
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from modbus_link import PlcLink, make_pymodbus_client

OK, FAILED, RESTORE_FAILED, BAD_REQUEST, UNREACHABLE = (
    "ok", "failed", "restore_failed", "bad_request", "unreachable")
EXIT_CODES = {OK: 0, FAILED: 1, RESTORE_FAILED: 1, BAD_REQUEST: 2, UNREACHABLE: 3}

MAX_LIGHT_SECONDS = 600          # a wizard test, not a way to leave the light off
MAX_PULSE_SECONDS = 120
RESTORE_BUDGET_S = 15            # keep trying to write the restore value this long
SUPPORTED_TYPES = ("modbus_tcp",)


@dataclass
class TestSettings:
    type: str = "modbus_tcp"
    ip: Optional[str] = None
    port: int = 502
    timeout_s: float = 3.0
    light_register: Optional[int] = None
    light_on_value: int = 1
    cleaning_register: Optional[int] = None
    conveyor_status_register: Optional[int] = None
    cleaning_pulse_seconds: float = 10.0
    settings_from: str = "command line"


class TestError(Exception):
    """A bad request (missing or invalid setting); becomes status bad_request."""


# ---------------------------------------------------------------- settings
FIELDS = ("type", "ip", "port", "timeout_s", "light_register", "light_on_value",
          "cleaning_register", "conveyor_status_register", "cleaning_pulse_seconds")


def build_test_settings(from_file: Optional[dict], overrides: dict) -> TestSettings:
    """from_file: the `plc` block of plc.json (or None); overrides: command-line values (None = not given)."""
    t = TestSettings()
    sources = []
    if from_file:
        for name in FIELDS:
            if from_file.get(name) is not None:
                setattr(t, name, from_file[name])
        sources.append("plc.json")
    given = {k: v for k, v in overrides.items() if k in FIELDS and v is not None}
    for name, value in given.items():
        setattr(t, name, value)
    if given:
        sources.append("command line")
    t.settings_from = " + ".join(sources) or "command line"
    _validate(t)
    return t


def _validate(t: TestSettings):
    if t.type not in SUPPORTED_TYPES:
        raise TestError(f"PLC type {t.type!r} is not supported (only {', '.join(SUPPORTED_TYPES)})")
    if not t.ip or not isinstance(t.ip, str):
        raise TestError("no PLC IP given (--ip)")
    for name in ("port", "light_register", "cleaning_register", "conveyor_status_register"):
        value = getattr(t, name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
            raise TestError(f"{name} must be a whole number 0..65535, got {value!r}")
    if t.light_on_value not in (0, 1) or isinstance(t.light_on_value, bool):
        raise TestError(f"light_on_value must be 0 or 1, got {t.light_on_value!r}")
    for name in ("timeout_s", "cleaning_pulse_seconds"):
        value = getattr(t, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise TestError(f"{name} must be a number > 0, got {value!r}")
    if t.light_register is not None and t.light_register == t.cleaning_register:
        raise TestError(f"light_register and cleaning_register are both {t.light_register}")


# ---------------------------------------------------------------- result helpers
def _result(t: Optional[TestSettings], action: str, status: str, message: str, **fields) -> dict:
    result = {"ok": status == OK, "status": status, "action": action, "message": message,
              "plc": None if t is None else {"type": t.type, "ip": t.ip, "port": t.port},
              "settings_from": None if t is None else t.settings_from}
    result.update(fields)
    return result


def bad_request(action: str, message: str) -> dict:
    return _result(None, action, BAD_REQUEST, message)


class _Runner:
    """Shared plumbing: one link with a one-register write gate, a stop event, a write log."""

    def __init__(self, t: TestSettings, allowed, log, client_factory, clock, stop_event):
        self.t = t
        self.log = log or _quiet_logger()
        self.clock = clock
        self.stop = stop_event or threading.Event()
        self.link = PlcLink(t.ip, t.port, t.timeout_s, allowed=allowed, log=self.log,
                            client_factory=client_factory or make_pymodbus_client, clock=clock)
        self.writes = []

    def connect(self) -> bool:
        return self.link.ensure_connected()

    def write(self, register, value, reason) -> bool:
        ok = self.link.write(register, value, f"wizard test: {reason}")
        self.writes.append({"register": register, "value": value, "reason": reason, "written": ok,
                            "read_back": self.link.read(register) if ok else None})
        return ok

    def wait(self, seconds) -> bool:
        """Wait, or stop early on SIGTERM / Ctrl-C. True when the full time passed."""
        return not self.stop.wait(seconds)

    def write_until_done(self, register, value, reason, sleep) -> bool:
        """The restore write: retried (with the link's reconnect) for up to RESTORE_BUDGET_S."""
        deadline = self.clock() + RESTORE_BUDGET_S
        while True:
            if self.write(register, value, reason):
                return True
            if self.clock() >= deadline:
                return False
            sleep(1.0)

    def close(self):
        self.link.close()


def _quiet_logger():
    log = logging.getLogger("plc.test.quiet")
    if not log.handlers:
        log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


# ---------------------------------------------------------------- the three tests
def check(t: TestSettings, log=None, client_factory: Optional[Callable] = None,
          clock: Callable = time.monotonic) -> dict:
    """Read every configured register (light, cleaning, conveyor status). Writes nothing."""
    names = (("light", t.light_register), ("cleaning", t.cleaning_register),
             ("conveyor_status", t.conveyor_status_register))
    wanted = [(n, r) for n, r in names if r is not None]
    if not wanted:
        return _result(t, "check", BAD_REQUEST, "no register given to read")
    r = _Runner(t, allowed=(), log=log, client_factory=client_factory, clock=clock, stop_event=None)
    started = clock()
    try:
        if not r.connect():
            return _result(t, "check", UNREACHABLE, f"PLC {t.ip}:{t.port} not reachable")
        registers = {}
        for name, register in wanted:
            registers[name] = {"register": register, "value": r.link.read(register)}
        unread = [n for n, v in registers.items() if v["value"] is None]
        status = OK if not unread else FAILED
        message = "all registers read" if not unread else f"could not read: {', '.join(unread)}"
        light = registers.get("light", {}).get("value")
        return _result(t, "check", status, message, registers=registers,
                       light_on_value=t.light_on_value,
                       light_is_on=None if light is None else light == t.light_on_value,
                       duration_s=round(clock() - started, 2))
    finally:
        r.close()


def light_test(t: TestSettings, state: str, seconds: float, log=None,
               client_factory: Optional[Callable] = None, clock: Callable = time.monotonic,
               sleep: Callable = time.sleep, stop_event: Optional[threading.Event] = None) -> dict:
    """Switch the light on or off, hold `seconds`, then always write light_on_value again."""
    if t.light_register is None:
        return _result(t, "light", BAD_REQUEST, "no light register given (--light-register)")
    if state not in ("on", "off"):
        return _result(t, "light", BAD_REQUEST, f"light state must be on or off, got {state!r}")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) \
            or not 0 < seconds <= MAX_LIGHT_SECONDS:
        return _result(t, "light", BAD_REQUEST, f"seconds must be > 0 and <= {MAX_LIGHT_SECONDS}")
    on, off = t.light_on_value, 1 - t.light_on_value
    target = on if state == "on" else off
    r = _Runner(t, allowed={t.light_register}, log=log, client_factory=client_factory, clock=clock,
                stop_event=stop_event)
    started = clock()
    try:
        if not r.connect():
            return _result(t, "light", UNREACHABLE, f"PLC {t.ip}:{t.port} not reachable")
        before = r.link.read(t.light_register)
        if not r.write(t.light_register, target, f"light {state}"):
            restored = r.write_until_done(t.light_register, on, "light back on (after failed write)", sleep)
            return _result(t, "light", FAILED if restored else RESTORE_FAILED,
                           f"could not switch the light {state}" +
                           ("" if restored else "; the light may be OFF: check the PLC"),
                           value_before=before, writes=r.writes, restored=restored)
        held_full_time = r.wait(seconds)
        restored = r.write_until_done(t.light_register, on, "light back to light_on_value", sleep)
        fields = dict(state=state, seconds=seconds, held_full_time=held_full_time, value_before=before,
                      writes=r.writes, restored=restored, duration_s=round(clock() - started, 2))
        if not restored:
            return _result(t, "light", RESTORE_FAILED,
                           f"light switched {state} but NOT put back to {on}: the light may be OFF; "
                           "check the PLC or start the PLC service", **fields)
        note = "" if held_full_time else " (stopped early)"
        return _result(t, "light", OK, f"light {state} for {seconds:g} s, then back to {on}{note}", **fields)
    finally:
        r.close()


def pulse_test(t: TestSettings, log=None, client_factory: Optional[Callable] = None,
               clock: Callable = time.monotonic, sleep: Callable = time.sleep,
               stop_event: Optional[threading.Event] = None) -> dict:
    """One cleaning pulse exactly like the service: 1, wait cleaning_pulse_seconds, 0."""
    if t.cleaning_register is None:
        return _result(t, "pulse", BAD_REQUEST, "no cleaning register given (--cleaning-register)")
    if t.cleaning_pulse_seconds > MAX_PULSE_SECONDS:
        return _result(t, "pulse", BAD_REQUEST, f"cleaning_pulse_seconds must be <= {MAX_PULSE_SECONDS}")
    r = _Runner(t, allowed={t.cleaning_register}, log=log, client_factory=client_factory, clock=clock,
                stop_event=stop_event)
    started = clock()
    try:
        if not r.connect():
            return _result(t, "pulse", UNREACHABLE, f"PLC {t.ip}:{t.port} not reachable")
        if not r.write(t.cleaning_register, 1, "cleaning test pulse start"):
            ended = r.write_until_done(t.cleaning_register, 0, "cleaning off (after failed start)", sleep)
            return _result(t, "pulse", FAILED if ended else RESTORE_FAILED,
                           "could not start the cleaning pulse" +
                           ("" if ended else "; cleaning may be ON: check the PLC"),
                           writes=r.writes, ended=ended)
        held_full_time = r.wait(t.cleaning_pulse_seconds)
        ended = r.write_until_done(t.cleaning_register, 0, "cleaning test pulse end", sleep)
        fields = dict(seconds=t.cleaning_pulse_seconds, held_full_time=held_full_time, writes=r.writes,
                      ended=ended, duration_s=round(clock() - started, 2))
        if not ended:
            return _result(t, "pulse", RESTORE_FAILED,
                           "cleaning started but 0 NOT written: cleaning may be ON; check the PLC", **fields)
        note = "" if held_full_time else " (stopped early)"
        return _result(t, "pulse", OK, f"cleaning pulse of {t.cleaning_pulse_seconds:g} s done{note}", **fields)
    finally:
        r.close()


# ---------------------------------------------------------------- text output for people
def format_text(result: dict) -> str:
    lines = [f"{result['action']}: {result['status'].upper()} - {result['message']}"]
    if result.get("plc"):
        p = result["plc"]
        lines.append(f"PLC {p['type']} {p['ip']}:{p['port']} (settings from {result['settings_from']})")
    for name, item in (result.get("registers") or {}).items():
        lines.append(f"  {name:16} register {item['register']}: {item['value']}")
    if "light_on_value" in result:
        lines.append(f"  light_on_value: {result['light_on_value']}  light is on: {result.get('light_is_on')}")
    for w in result.get("writes") or []:
        lines.append(f"  write {w['register']} = {w['value']} ({w['reason']}): "
                     f"{'ok' if w['written'] else 'FAILED'}, read back {w['read_back']}")
    return "\n".join(lines)
