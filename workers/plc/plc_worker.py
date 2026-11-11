"""PLC service (STATUS "PLC service", D-024, automation cases A8 light and A10 cleaning).

The ONE program on a site that talks to the PLC. Replaces the one-shot light.py and
cleaning_mechanism.py (same writes, same pulse); settings only from rendered plc.json.

Run as Supervisor worker `plc` (mode service, restart_cost normal):
    python3 plc_worker.py
Test functions for the setup wizard (STATUS S5, plc_test.py; add --json for the UI):
    python3 plc_worker.py --check                       read light, cleaning, conveyor registers; writes NOTHING
    python3 plc_worker.py --light on|off --seconds N    light on/off for N s, then back to light_on_value
    python3 plc_worker.py --pulse                       one cleaning test pulse (1, cleaning_pulse_seconds, 0)
  Settings: plc.json (env DPC_WORKER_CONFIG) if present, each overridable on the command line:
    --type --ip --port --timeout-s --light-register --light-on-value --cleaning-register
    --conveyor-status-register --cleaning-pulse-seconds   (no built-in IP or registers)
  Exit codes: 0 ok, 1 failed / restore failed, 2 bad request, 3 PLC unreachable.
  These talk to the PLC directly. --clean-pulse instead asks the RUNNING service (MQTT):
    python3 plc_worker.py --clean-pulse [--reason T]

Exit codes: 0 after SIGTERM/SIGINT; 1 on a config error or an unexpected error, after
writing <root>/run/plc.last_error.json (D-073 D7, D-079). An unreachable PLC is NOT an error.
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

CODE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(CODE_DIR))
sys.path.insert(0, REPO_ROOT)                     # for common.last_error
sys.path.insert(0, CODE_DIR)

from common.last_error import clear_last_error, write_last_error   # noqa: E402
from modbus_link import PlcLink                                     # noqa: E402
import plc_test                                                     # noqa: E402
from plc_db import PlcLamp                                          # noqa: E402
from plc_mqtt import PlcMqtt                                        # noqa: E402
from plc_service import PlcService                                  # noqa: E402
from plc_settings import (WORKER_ID, ConfigError, build_settings,   # noqa: E402
                          read_raw_config, root_from_code_location, secrets_in)

VERSION = "plc-1"
TICK_S = 0.2
HEARTBEAT_S = 10
_stop = threading.Event()


def _on_signal(signum, _frame):
    _stop.set()


def setup_logging(logs_dir: str, backup_count: int) -> logging.Logger:
    os.makedirs(logs_dir, exist_ok=True)
    log = logging.getLogger("plc")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    handler = TimedRotatingFileHandler(os.path.join(logs_dir, "plc.log"), when="midnight",
                                       backupCount=backup_count, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    log.addHandler(handler)
    return log


def run(s, log, link_factory=None, sleep=None) -> int:
    link_factory = link_factory or PlcLink      # looked up at call time (tests replace it)
    sleep = sleep or _stop.wait
    link = link_factory(s.ip, s.port, s.timeout_s, s.writable_registers, log)
    bus = PlcMqtt(s, None, VERSION, log)
    service = PlcService(s, link, log, publish_event=bus.publish_event)
    bus.service = service
    lamp = PlcLamp(s.db, log)

    service.start()
    bus.start()
    log.info(f"PLC service started: {s.plc_type} {s.ip}:{s.port}, light {s.light_register} "
             f"(on = {s.light_on_value}), cleaning {s.cleaning_register} "
             f"({s.cleaning_pulse_seconds:g} s pulse), conveyor status {s.conveyor_status_register}, "
             f"auto cleaning {'on' if s.auto_cleaning else 'off'}")
    last_status = last_heartbeat = 0.0
    try:
        while not _stop.is_set():
            service.tick()
            now = service.clock()
            if now - last_status >= s.status_interval_seconds:
                last_status = now
                bus.publish_status(service.snapshot())
            if now - last_heartbeat >= HEARTBEAT_S:
                last_heartbeat = now
                bus.publish_heartbeat()
            lamp.update(link.connected)
            sleep(TICK_S)
        log.info("stop requested")
        service.stop()
        bus.publish_status(service.snapshot())
        return 0
    finally:
        bus.stop()
        link.close()


def _parse_args(argv):
    parser = argparse.ArgumentParser(description="DPC PLC service and PLC test functions")
    tests = parser.add_argument_group("test functions (talk to the PLC directly)")
    tests.add_argument("--check", action="store_true", help="read the registers, write nothing")
    tests.add_argument("--light", choices=("on", "off"), help="switch the light, then back to light_on_value")
    tests.add_argument("--seconds", type=float, default=5.0, help="how long --light holds (default 5)")
    tests.add_argument("--pulse", action="store_true", help="one cleaning test pulse")
    tests.add_argument("--json", action="store_true", help="print the result as JSON (for the UI)")
    plc = parser.add_argument_group("PLC settings (override plc.json)")
    plc.add_argument("--type", dest="type")
    plc.add_argument("--ip")
    plc.add_argument("--port", type=int)
    plc.add_argument("--timeout-s", dest="timeout_s", type=float)
    plc.add_argument("--light-register", dest="light_register", type=int)
    plc.add_argument("--light-on-value", dest="light_on_value", type=int)
    plc.add_argument("--cleaning-register", dest="cleaning_register", type=int)
    plc.add_argument("--conveyor-status-register", dest="conveyor_status_register", type=int)
    plc.add_argument("--cleaning-pulse-seconds", dest="cleaning_pulse_seconds", type=float)
    parser.add_argument("--clean-pulse", action="store_true", help="ask the RUNNING service for a pulse (MQTT)")
    parser.add_argument("--reason", default="manual (CLI)")
    args = parser.parse_args(argv)
    actions = [a for a in ("check", "light", "pulse", "clean_pulse") if getattr(args, a)]
    if len(actions) > 1:
        parser.error("give only one of --check, --light, --pulse, --clean-pulse")
    return args, (actions[0] if actions else None)


def _optional_plc_json():
    """(raw config or None, error text or None). For the tests plc.json is optional."""
    path = os.environ.get("DPC_WORKER_CONFIG", "")
    if not path or not os.path.isfile(path):
        return None, None
    try:
        return read_raw_config(), None
    except ConfigError as e:
        return None, str(e)


def _test_logger(raw):
    """Writes of the tests go to <root>/logs/plc_test.log when the logs folder is known."""
    paths = (raw or {}).get("paths") or {}
    logs = paths.get("logs") or os.path.join(root_from_code_location(), "logs")
    log = logging.getLogger("plc.test")
    log.setLevel(logging.INFO)
    log.propagate = False
    if not log.handlers:
        if os.path.isdir(logs):
            handler = RotatingFileHandler(os.path.join(logs, "plc_test.log"), maxBytes=5 * 1024 * 1024,
                                          backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
            log.addHandler(handler)
        else:
            log.addHandler(logging.NullHandler())
    return log


def run_test(args, action) -> dict:
    """--check / --light / --pulse: build the settings, run the test, return the result."""
    raw, file_error = _optional_plc_json()
    if file_error:
        return plc_test.bad_request(action, f"plc.json: {file_error}")
    overrides = {name: getattr(args, name) for name in plc_test.FIELDS}
    try:
        t = plc_test.build_test_settings((raw or {}).get("plc"), overrides)
    except plc_test.TestError as e:
        return plc_test.bad_request(action, str(e))
    log = _test_logger(raw)
    if action == "check":
        return plc_test.check(t, log=log)
    stop = threading.Event()                 # SIGTERM / Ctrl-C ends the wait and restores at once
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if action == "light":
        return plc_test.light_test(t, args.light, args.seconds, log=log, stop_event=stop)
    return plc_test.pulse_test(t, log=log, stop_event=stop)


def main(argv=None) -> int:
    args, action = _parse_args(argv)
    if action in ("check", "light", "pulse"):
        result = run_test(args, action)
        print(json.dumps(result, indent=1) if args.json else plc_test.format_text(result))
        return plc_test.EXIT_CODES[result["status"]]

    raw = {}
    root = root_from_code_location()
    worker_id = WORKER_ID
    try:
        raw = read_raw_config()
        s = build_settings(raw)
        root, worker_id = s.root, s.worker_id
    except ConfigError as e:
        if action == "clean_pulse":
            print(f"config error: {e}")
            return 1
        clear_last_error(root, worker_id)
        write_last_error(root, worker_id, e, secrets=secrets_in(raw))
        return 1

    if action == "clean_pulse":
        if s.mqtt is None:
            print("no 'mqtt' block in plc.json: cannot reach the service")
            return 1
        PlcMqtt(s, None, VERSION, logging.getLogger("plc.cli")).send_command_once("cli", args.reason)
        print("clean_pulse command sent; see logs/plc.log")
        return 0

    clear_last_error(s.root, s.worker_id)
    log = setup_logging(s.logs, s.backup_count)
    for warning in s.warnings:
        log.warning(warning)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    try:
        return run(s, log)
    except Exception as e:
        log.exception("unexpected error, exiting (the Supervisor restarts the service)")
        write_last_error(s.root, s.worker_id, e, secrets=s.secrets())
        return 1


if __name__ == "__main__":
    sys.exit(main())
