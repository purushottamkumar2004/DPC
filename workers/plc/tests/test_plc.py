"""Tests for the PLC service (workers/plc). No PLC, broker or DB needed: a fake Modbus client
(same calls as pymodbus: connect, close, read_holding_registers, write_register) and a fake
clock. One extra test talks to pymodbus's own Modbus TCP server on localhost when pymodbus
is installed (skipped otherwise).

Run from the repo root:
    python3 -m unittest discover -s workers/plc/tests -t . -v
"""
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(os.path.dirname(WORKER_DIR))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, WORKER_DIR)

import modbus_link as ML            # noqa: E402
import plc_settings as PS           # noqa: E402
import plc_worker as W              # noqa: E402
from modbus_link import PlcLink     # noqa: E402
from plc_db import PlcLamp          # noqa: E402
from plc_mqtt import PlcMqtt        # noqa: E402
from plc_service import PlcService  # noqa: E402

LIGHT, CLEAN, CONVEYOR = 4109, 4106, 4098


# ---------------------------------------------------------------- fakes
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class _Reply:
    def __init__(self, registers=None, error=False):
        self.registers = registers or []
        self._error = error

    def isError(self):
        return self._error


class FakeModbusClient:
    """Holding registers in a dict; `reachable` switches the 'network' on and off."""

    def __init__(self, registers=None):
        self.registers = dict(registers or {})
        self.reachable = True
        self.open = False
        self.writes = []            # (register, value) in order
        self.connect_calls = 0
        self.fail_writes = 0        # next n writes raise (cable pulled mid-pulse)

    def connect(self):
        self.connect_calls += 1
        self.open = self.reachable
        return self.open

    def close(self):
        self.open = False

    def _check(self):
        if not (self.reachable and self.open):
            raise ConnectionError("Connection unexpectedly closed")

    def read_holding_registers(self, address, count=1):
        self._check()
        return _Reply([self.registers.get(address + i, 0) for i in range(count)])

    def write_register(self, address, value):
        self._check()
        if self.fail_writes:
            self.fail_writes -= 1
            raise ConnectionError("broken pipe")
        self.registers[address] = value
        self.writes.append((address, value))
        return _Reply()


def raw_config(tmp, **plc_overrides):
    plc = {"type": "modbus_tcp", "ip": "192.0.2.10", "port": 502, "timeout_s": 3,
           "light_register": LIGHT, "light_on_value": 1, "cleaning_register": CLEAN,
           "conveyor_status_register": CONVEYOR, "cleaning_pulse_seconds": 10,
           "light_check_seconds": 60, "status_interval_seconds": 10,
           "cleaning_cooldown_seconds": 600, "auto_cleaning": False}
    plc.update(plc_overrides)
    return {"worker_id": "plc", "paths": {"root": tmp, "logs": os.path.join(tmp, "logs"),
                                          "state": os.path.join(tmp, "data", "state")},
            "log": {"backup_count": 3}, "plc": plc,
            "mqtt": {"host": "localhost", "port": 1883, "user": None, "password": None}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="plc_test_")
        self.log = logging.getLogger(f"plc_test_{id(self)}")
        self.log.setLevel(logging.DEBUG)
        self.clock = Clock()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, registers=None, events=None, **plc_overrides):
        self.s = PS.build_settings(raw_config(self.tmp, **plc_overrides))
        self.fake = FakeModbusClient(registers if registers is not None
                                     else {LIGHT: 0, CLEAN: 0, CONVEYOR: 1})
        self.link = PlcLink(self.s.ip, self.s.port, self.s.timeout_s, self.s.writable_registers,
                            self.log, client_factory=lambda *a: self.fake, clock=self.clock)
        publish = (lambda n, p: events.append((n, p))) if events is not None else None
        self.svc = PlcService(self.s, self.link, self.log, publish_event=publish, clock=self.clock)
        self.svc.start()
        return self.svc

    def run_for(self, seconds, step=0.2):
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.svc.tick()
            self.clock.advance(step)

    def assert_only_configured_writes(self):
        for register, _ in self.fake.writes:
            self.assertIn(register, {LIGHT, CLEAN})


# ---------------------------------------------------------------- settings
class SettingsTests(Base):
    def test_full_file_has_no_warnings_about_plc_keys(self):
        s = PS.build_settings(raw_config(self.tmp))
        self.assertEqual((s.ip, s.light_register, s.cleaning_register), ("192.0.2.10", LIGHT, CLEAN))
        self.assertFalse([w for w in s.warnings if "'plc." in w])

    def test_missing_keys_get_tadipatri_defaults_with_one_warning_each(self):
        raw = raw_config(self.tmp)
        raw["plc"] = {}
        s = PS.build_settings(raw)
        self.assertEqual(s.ip, "192.168.1.5")
        self.assertEqual(s.light_on_value, 1)
        self.assertEqual(s.cleaning_pulse_seconds, 10.0)
        plc_warnings = [w for w in s.warnings if "'plc." in w]
        self.assertEqual(len(plc_warnings), len([k for k in PS.DEFAULTS if k.startswith("plc.")]))

    def test_missing_file_is_a_config_error(self):
        with mock.patch.dict(os.environ, {"DPC_WORKER_CONFIG": os.path.join(self.tmp, "nope.json")}):
            with self.assertRaises(PS.ConfigError):
                PS.read_raw_config()

    def test_bad_values_stop_with_the_key_name(self):
        for overrides, key in (({"light_on_value": 2}, "light_on_value"),
                               ({"cleaning_register": LIGHT}, "light_register"),
                               ({"cleaning_pulse_seconds": 0}, "cleaning_pulse_seconds"),
                               ({"light_register": "4109"}, "light_register"),
                               ({"type": "s7"}, "plc.type")):
            with self.assertRaises(PS.ConfigError) as ctx:
                PS.build_settings(raw_config(self.tmp, **overrides))
            self.assertIn(key, str(ctx.exception))

    def test_conveyor_on_a_written_register_is_only_a_warning(self):
        s = PS.build_settings(raw_config(self.tmp, conveyor_status_register=CLEAN))
        self.assertTrue(any("conveyor_status_register" in w for w in s.warnings))


# ---------------------------------------------------------------- light (A8)
class LightTests(Base):
    def test_light_written_with_on_value_1(self):
        self.make(light_on_value=1)
        self.svc.tick()
        self.assertEqual(self.fake.writes, [(LIGHT, 1)])

    def test_light_written_with_on_value_0(self):
        self.make(registers={LIGHT: 1, CLEAN: 0, CONVEYOR: 1}, light_on_value=0)
        self.svc.tick()
        self.assertEqual(self.fake.writes, [(LIGHT, 0)])

    def test_light_verified_and_rewritten_only_when_it_differs(self):
        self.make()
        self.run_for(1)
        self.run_for(120)                       # two checks, light still on: no new write
        self.assertEqual(self.fake.writes, [(LIGHT, 1)])
        self.fake.registers[LIGHT] = 0          # someone / something switched it off
        self.run_for(61)
        self.assertEqual(self.fake.writes, [(LIGHT, 1), (LIGHT, 1)])

    def test_light_reasserted_after_reconnect(self):
        self.make()
        self.run_for(1)
        self.fake.reachable = False             # PLC reboot
        self.run_for(61)                        # light check notices the broken link
        self.fake.registers[LIGHT] = 0          # PLC came back with the light off
        self.fake.reachable = True
        self.run_for(40)
        self.assertEqual(self.fake.writes, [(LIGHT, 1), (LIGHT, 1)])
        self.assertTrue(self.svc.light_ok)


# ---------------------------------------------------------------- cleaning (A10)
class PulseTests(Base):
    def test_pulse_1_then_0_after_the_configured_time(self):
        events = []
        self.make(events=events, cleaning_pulse_seconds=7)
        self.svc.tick()
        self.fake.writes.clear()
        self.svc.request_pulse("operator", "dust")
        self.svc.tick()
        self.assertEqual(self.fake.writes, [(CLEAN, 1)])
        self.run_for(6.8)
        self.assertEqual(self.fake.writes, [(CLEAN, 1)])          # still ON before 7 s
        self.run_for(0.6)
        self.assertEqual(self.fake.writes, [(CLEAN, 1), (CLEAN, 0)])
        self.assertEqual([e[0] for e in events if e[0].startswith("cleaning")],
                         ["cleaning_pulse_started", "cleaning_pulse_ended"])
        self.assertFalse(os.path.exists(self.svc.marker_path))

    def test_zero_is_retried_until_written(self):
        self.make()
        self.svc.tick()
        self.svc.request_pulse("operator", "dust")
        self.svc.tick()
        self.fake.reachable = False                                # cable pulled during the pulse
        self.run_for(30)
        self.assertEqual(self.fake.registers[CLEAN], 1)
        self.assertTrue(os.path.exists(self.svc.marker_path))
        self.fake.reachable = True
        self.run_for(35)
        self.assertEqual(self.fake.registers[CLEAN], 0)
        self.assertFalse(os.path.exists(self.svc.marker_path))

    def test_pulse_left_open_by_a_crash_is_closed_at_start(self):
        self.make()
        self.svc.tick()
        self.svc.request_pulse("operator", "dust")
        self.svc.tick()                                            # 1 written, then "crash"
        fake = self.fake
        self.make(registers=fake.registers)                        # new run, same PLC
        self.svc.tick()
        self.assertEqual(self.fake.registers[CLEAN], 0)
        self.assertIn((CLEAN, 0), self.fake.writes)

    def test_no_pulse_writes_without_a_marker(self):
        self.make(registers={LIGHT: 1, CLEAN: 1, CONVEYOR: 1})     # operator pulse from the HMI
        self.run_for(5)
        self.assertNotIn(CLEAN, [r for r, _ in self.fake.writes])

    def test_stop_during_a_pulse_writes_0_at_once(self):
        self.make()
        self.svc.tick()
        self.svc.request_pulse("operator", "dust")
        self.svc.tick()
        self.svc.stop(sleep=lambda s: self.clock.advance(s))
        self.assertEqual(self.fake.writes[-1], (CLEAN, 0))

    def test_request_during_a_pulse_is_ignored(self):
        self.make()
        self.svc.tick()
        self.svc.request_pulse("operator", "a")
        self.svc.tick()
        self.svc.request_pulse("operator", "b")
        self.run_for(11)
        self.assertEqual([w for w in self.fake.writes if w[0] == CLEAN], [(CLEAN, 1), (CLEAN, 0)])

    def test_automatic_request_ignored_while_auto_cleaning_is_off(self):
        self.make(auto_cleaning=False)
        self.svc.tick()
        self.svc.request_pulse("inspection_left", "poor_view", automatic=True)
        self.run_for(1)
        self.assertNotIn(CLEAN, [r for r, _ in self.fake.writes])

    def test_automatic_requests_respect_the_cooldown(self):
        self.make(auto_cleaning=True, cleaning_cooldown_seconds=100)
        self.svc.tick()
        self.svc.request_pulse("inspection_left", "poor_view", automatic=True)
        self.run_for(11)                                           # first pulse done
        self.svc.request_pulse("inspection_left", "poor_view", automatic=True)
        self.run_for(11)                                           # inside the cooldown
        self.assertEqual(len([w for w in self.fake.writes if w == (CLEAN, 1)]), 1)
        self.clock.advance(100)
        self.svc.request_pulse("inspection_left", "poor_view", automatic=True)
        self.run_for(11)
        self.assertEqual(len([w for w in self.fake.writes if w == (CLEAN, 1)]), 2)

    def test_manual_request_while_plc_unreachable_is_dropped(self):
        self.make()
        self.fake.reachable = False
        self.svc.request_pulse("operator", "dust")
        self.run_for(5)
        self.fake.reachable = True
        self.run_for(40)
        self.assertNotIn(CLEAN, [r for r, _ in self.fake.writes])


# ---------------------------------------------------------------- unreachable PLC
class UnreachableTests(Base):
    def test_unreachable_plc_is_retried_without_crash_or_log_flood(self):
        self.make()
        self.fake.reachable = False
        with self.assertLogs(self.log, level="WARNING") as logs:
            self.run_for(300)                                      # 5 minutes down
            self.log.warning("marker")
        unreachable = [r for r in logs.output if "unreachable" in r]
        self.assertEqual(len(unreachable), 1)
        self.assertGreater(self.fake.connect_calls, 5)             # kept retrying ...
        self.assertLess(self.fake.connect_calls, 30)               # ... with backoff, not every tick
        self.assertFalse(self.svc.snapshot()["connected"])
        self.fake.reachable = True
        self.run_for(40)
        self.assertTrue(self.svc.snapshot()["connected"])
        self.assertEqual(self.fake.writes, [(LIGHT, 1)])

    def test_conveyor_status_read_and_in_the_snapshot(self):
        self.make(registers={LIGHT: 1, CLEAN: 0, CONVEYOR: 3})
        self.svc.tick()
        snap = self.svc.snapshot()
        self.assertEqual(snap["conveyor_status"], 3)
        self.assertTrue(snap["light_ok"])


# ---------------------------------------------------------------- write gate
class WriteGateTests(Base):
    def test_register_outside_the_config_is_refused(self):
        self.make()
        with self.assertRaises(ValueError):
            self.link.write(4097, 1, "test")
        with self.assertRaises(ValueError):
            self.link.write(CONVEYOR, 1, "test")                   # read-only register
        self.assertEqual(self.fake.writes, [])

    def test_whole_run_writes_only_light_and_cleaning(self):
        self.make(auto_cleaning=True, cleaning_cooldown_seconds=1)
        for _ in range(5):
            self.svc.request_pulse("x", "y", automatic=True)
            self.run_for(15)
        self.fake.registers[LIGHT] = 0
        self.run_for(70)
        self.assertTrue(self.fake.writes)
        self.assert_only_configured_writes()

    def test_check_mode_cannot_write(self):
        link = PlcLink("192.0.2.10", 502, 3, allowed=(), log=self.log,
                       client_factory=lambda *a: FakeModbusClient({LIGHT: 1}))
        with self.assertRaises(ValueError):
            link.write(LIGHT, 1, "test")


# ---------------------------------------------------------------- MQTT messages
class MqttTests(Base):
    def setUp(self):
        super().setUp()
        self.make(auto_cleaning=True)
        self.bus = PlcMqtt(self.s, self.svc, "test", self.log)

    def requests(self):
        out = []
        while not self.svc.requests.empty():
            out.append(self.svc.requests.get())
        return out

    def test_command_becomes_a_manual_request(self):
        self.bus.handle_message("dpc/plc/command", b'{"action":"clean_pulse","source":"ui","reason":"dust"}')
        self.assertEqual(self.requests(), [{"source": "ui", "reason": "dust", "automatic": False}])

    def test_poor_view_becomes_an_automatic_request(self):
        self.bus.handle_message("dpc/worker/inspection_left/event",
                                json.dumps({"worker_id": "inspection_left", "event": "poor_view",
                                            "side": "LEFT"}).encode())
        req = self.requests()
        self.assertEqual(len(req), 1)
        self.assertTrue(req[0]["automatic"])

    def test_other_messages_are_ignored(self):
        self.bus.handle_message("dpc/plc/command", b"not json")
        self.bus.handle_message("dpc/plc/command", b'{"action":"write","register":4097}')
        self.bus.handle_message("dpc/worker/inspection_left/event", b'{"event":"worker_started"}')
        self.bus.handle_message("dpc/worker/plc/event", b'{"event":"poor_view"}')
        self.assertEqual(self.requests(), [])


# ---------------------------------------------------------------- PLC lamp
class LampTests(Base):
    def test_lamp_written_on_change_and_then_every_interval(self):
        executed = []

        class Conn:
            def cursor(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, args):
                executed.append(args[0])

            def close(self):
                pass

        lamp = PlcLamp(object(), self.log, connect=lambda db: Conn(), clock=self.clock)
        lamp.update(True)
        lamp.update(True)
        lamp.update(False)
        self.clock.advance(121)
        lamp.update(False)
        self.assertEqual(executed, ["True", "False", "False"])

    def test_db_error_is_logged_once_and_retried_later(self):
        calls = []

        def broken(db):
            calls.append(1)
            raise OSError("db down")

        lamp = PlcLamp(object(), self.log, connect=broken, clock=self.clock)
        for _ in range(50):
            lamp.update(True)
            lamp.update(False)
        self.assertEqual(len(calls), 1)


# ---------------------------------------------------------------- program level
class ProgramTests(Base):
    def write_config(self, raw):
        path = os.path.join(self.tmp, "plc.json")
        with open(path, "w") as fh:
            json.dump(raw, fh)
        return path

    def test_config_error_writes_last_error_and_exits_1(self):
        raw = raw_config(self.tmp, light_on_value=5)
        with mock.patch.dict(os.environ, {"DPC_WORKER_CONFIG": self.write_config(raw)}), \
                mock.patch.object(W, "root_from_code_location", return_value=self.tmp):
            self.assertEqual(W.main([]), 1)
        with open(os.path.join(self.tmp, "run", "plc.last_error.json")) as fh:
            self.assertEqual(json.load(fh)["type"], "ConfigError")

    def test_unexpected_error_writes_last_error_and_exits_1(self):
        class Boom(PlcLink):
            def ensure_connected(self):
                raise RuntimeError("bug")

        raw = raw_config(self.tmp)
        raw.pop("mqtt")
        with mock.patch.dict(os.environ, {"DPC_WORKER_CONFIG": self.write_config(raw)}), \
                mock.patch.object(W, "PlcLink", lambda *a, **k: Boom(*a, client_factory=lambda *x: FakeModbusClient(), **k)), \
                mock.patch.object(W.signal, "signal"):
            self.assertEqual(W.main([]), 1)
        with open(os.path.join(self.tmp, "run", "plc.last_error.json")) as fh:
            self.assertEqual(json.load(fh)["message"], "bug")

    def test_run_stops_cleanly_and_closes_a_running_pulse(self):
        s = PS.build_settings(raw_config(self.tmp))
        s.mqtt = None
        fake = FakeModbusClient({LIGHT: 0, CLEAN: 0, CONVEYOR: 1})
        created = {}
        real_init = PlcService.__init__

        def capture_init(service, *a, **k):          # keep a handle on the service run() builds
            real_init(service, *a, **k)
            created["service"] = service

        ticks = {"n": 0}

        def sleep(_seconds):                         # tick 1: ask for a pulse; tick 3: SIGTERM
            ticks["n"] += 1
            if ticks["n"] == 1:
                created["service"].request_pulse("test", "stop test")
            if ticks["n"] == 3:
                W._stop.set()

        W._stop.clear()
        with mock.patch.object(PlcService, "__init__", capture_init):
            code = W.run(s, self.log, sleep=sleep,
                         link_factory=lambda *a, **k: PlcLink(*a, client_factory=lambda *x: fake, **k))
        W._stop.clear()
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes, [(LIGHT, 1), (CLEAN, 1), (CLEAN, 0)])
        self.assertFalse(fake.open)


# ---------------------------------------------------------------- real pymodbus, local server
class RealPymodbusTests(unittest.TestCase):
    """The real ModbusTcpClient against pymodbus's own TCP server on 127.0.0.1."""

    @classmethod
    def setUpClass(cls):
        try:
            import pymodbus  # noqa: F401
            from pymodbus.server import StartTcpServer  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("pymodbus not installed")
        try:
            logging.getLogger("pymodbus").setLevel(logging.ERROR)
            cls.store, cls.server_thread, cls.port = _start_server()
        except Exception as e:                        # API differs across pymodbus versions
            raise unittest.SkipTest(f"pymodbus test server not available: {e}")

    @classmethod
    def tearDownClass(cls):
        try:
            from pymodbus.server import ServerStop
            ServerStop()
        except Exception:
            pass

    def test_real_client_writes_and_reads_holding_registers(self):
        log = logging.getLogger("plc_real")
        link = PlcLink("127.0.0.1", self.port, 3, allowed={LIGHT, CLEAN}, log=log)
        self.assertTrue(link.write(LIGHT, 1, "test"))
        self.assertEqual(link.read(LIGHT), 1)
        self.assertTrue(link.write(CLEAN, 0, "test"))
        self.assertEqual(link.read(CLEAN), 0)
        link.close()

    def test_unreachable_port_returns_none_without_raising(self):
        log = logging.getLogger("plc_real")
        link = PlcLink("127.0.0.1", _free_port(), 0.5, allowed={LIGHT}, log=log)
        self.assertIsNone(link.read(LIGHT))
        self.assertFalse(link.write(LIGHT, 1, "test"))


def _free_port():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_server():
    import time
    from pymodbus.datastore import ModbusSequentialDataBlock
    from pymodbus.server import StartTcpServer
    try:                                              # pymodbus >= 3.10
        from pymodbus.datastore import ModbusDeviceContext, ModbusServerContext
        block = ModbusSequentialDataBlock(1, [0] * 5000)
        context = ModbusServerContext(devices=ModbusDeviceContext(hr=block), single=True)
    except ImportError:                               # older 3.x
        from pymodbus.datastore import ModbusServerContext, ModbusSlaveContext
        block = ModbusSequentialDataBlock(1, [0] * 5000)
        context = ModbusServerContext(slaves=ModbusSlaveContext(hr=block), single=True)
    port = _free_port()
    thread = threading.Thread(target=StartTcpServer, kwargs={"context": context,
                                                             "address": ("127.0.0.1", port)},
                              daemon=True)
    thread.start()
    time.sleep(1.0)
    return block, thread, port


if __name__ == "__main__":
    unittest.main()
