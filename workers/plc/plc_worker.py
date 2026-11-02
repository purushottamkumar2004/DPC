"""PLC service (STATUS "PLC service", D-024, automation cases A8 light and A10 cleaning).

The ONE program on a site that talks to the PLC. Replaces the one-shot light.py and
cleaning_mechanism.py (same writes, same pulse); settings only from rendered plc.json.

Run as Supervisor worker `plc` (mode service, restart_cost normal):
    python3 plc_worker.py
Site tools (same settings, env DPC_WORKER_CONFIG):
    python3 plc_worker.py --check                  read light, cleaning, conveyor registers; writes NOTHING
    python3 plc_worker.py --clean-pulse [--reason T]  ask the running service for one pulse (MQTT)

Exit codes: 0 after SIGTERM/SIGINT; 1 on a config error or an unexpected error, after
writing <root>/run/plc.last_error.json (D-073 D7, D-079). An unreachable PLC is NOT an error.
"""

import argparse
import logging
import os
import signal
import sys
import threading
from logging.handlers import TimedRotatingFileHandler

CODE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(CODE_DIR))
sys.path.insert(0, REPO_ROOT)                     # for common.last_error
sys.path.insert(0, CODE_DIR)

from common.last_error import clear_last_error, write_last_error   # noqa: E402
from modbus_link import PlcLink                                     # noqa: E402
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


def check(s) -> int:
    """Read-only site check: prints the three registers, writes nothing."""
    log = logging.getLogger("plc.check")
    log.addHandler(logging.StreamHandler(sys.stdout))
    log.setLevel(logging.INFO)
    link = PlcLink(s.ip, s.port, s.timeout_s, allowed=(), log=log)   # allowed=() : no write possible
    if not link.ensure_connected():
        print(f"PLC {s.ip}:{s.port} not reachable")
        return 1
    for name, reg in (("light", s.light_register), ("cleaning", s.cleaning_register),
                      ("conveyor status", s.conveyor_status_register)):
        print(f"{name:16} register {reg}: {link.read(reg)}")
    print(f"light_on_value in config: {s.light_on_value}")
    link.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="DPC PLC service")
    parser.add_argument("--check", action="store_true", help="read the registers, write nothing")
    parser.add_argument("--clean-pulse", action="store_true", help="ask the running service for a pulse")
    parser.add_argument("--reason", default="manual (CLI)")
    args = parser.parse_args(argv)

    raw = {}
    root = root_from_code_location()
    worker_id = WORKER_ID
    try:
        raw = read_raw_config()
        s = build_settings(raw)
        root, worker_id = s.root, s.worker_id
    except ConfigError as e:
        if args.check or args.clean_pulse:
            print(f"config error: {e}")
            return 1
        clear_last_error(root, worker_id)
        write_last_error(root, worker_id, e, secrets=secrets_in(raw))
        return 1

    if args.check:
        return check(s)
    if args.clean_pulse:
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
