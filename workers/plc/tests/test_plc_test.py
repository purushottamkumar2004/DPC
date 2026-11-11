"""Tests for the PLC test functions (STATUS S5: --check, --light, --pulse, --json).

Unit tests use the fake Modbus client from test_plc.py and a fake clock; the CLI tests run
plc_worker.py against pymodbus's own Modbus TCP server on 127.0.0.1 (skipped without pymodbus),
including SIGTERM during a light test.

Run from the repo root:
    python3 -m unittest discover -s workers/plc/tests -t . -v
"""
import io
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER_DIR = os.path.dirname(HERE)
sys.path.insert(0, WORKER_DIR)
sys.path.insert(0, HERE)

import plc_test as P                # noqa: E402
import plc_worker as W              # noqa: E402
from test_plc import (CLEAN, CONVEYOR, LIGHT, Clock, FakeModbusClient,  # noqa: E402
                      _free_port, _start_server)


class FakeStop:
    """Stands in for the stop event: records the wait, returns at once (or 'stopped')."""

    def __init__(self, stopped=False):
        self.waited = []
        self.stopped = stopped

    def wait(self, seconds):
        self.waited.append(seconds)
        return self.stopped

    def set(self):
        self.stopped = True


def settings(**overrides):
    given = {"ip": "192.0.2.10", "light_register": LIGHT, "cleaning_register": CLEAN,
             "conveyor_status_register": CONVEYOR}
    given.update(overrides)
    return P.build_test_settings(None, given)


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.fake = FakeModbusClient({LIGHT: 1, CLEAN: 0, CONVEYOR: 1})

    def factory(self, *_a):
        return self.fake

    def sleep(self, seconds):
        self.clock.advance(seconds)

    def light(self, t, state, seconds=5, stop=None):
        return P.light_test(t, state, seconds, client_factory=self.factory, clock=self.clock,
                            sleep=self.sleep, stop_event=stop or FakeStop())

    def pulse(self, t, stop=None):
        return P.pulse_test(t, client_factory=self.factory, clock=self.clock, sleep=self.sleep,
                            stop_event=stop or FakeStop())


# ---------------------------------------------------------------- settings
class SettingsTests(unittest.TestCase):
    def test_command_line_only(self):
        t = settings()
        self.assertEqual((t.ip, t.port, t.light_on_value, t.settings_from),
                         ("192.0.2.10", 502, 1, "command line"))

    def test_plc_json_values_overridden_by_the_command_line(self):
        t = P.build_test_settings({"ip": "10.0.0.1", "light_register": 1, "cleaning_register": 2,
                                   "light_on_value": 0}, {"ip": "10.0.0.9", "light_register": None})
        self.assertEqual((t.ip, t.light_register, t.light_on_value), ("10.0.0.9", 1, 0))
        self.assertEqual(t.settings_from, "plc.json + command line")

    def test_no_built_in_ip_or_registers(self):
        t = P.build_test_settings(None, {"ip": "192.0.2.10"})
        self.assertIsNone(t.light_register)
        self.assertIsNone(t.cleaning_register)
        with self.assertRaises(P.TestError):
            P.build_test_settings(None, {})

    def test_bad_values_are_refused(self):
        for bad in ({"light_on_value": 2}, {"cleaning_register": LIGHT}, {"port": 70000},
                    {"type": "s7"}, {"cleaning_pulse_seconds": 0}, {"light_register": "4109"}):
            with self.assertRaises(P.TestError, msg=bad):
                settings(**bad)


# ---------------------------------------------------------------- check
class CheckTests(Base):
    def test_reads_every_configured_register_and_writes_nothing(self):
        r = P.check(settings(), client_factory=self.factory, clock=self.clock)
        self.assertEqual(r["status"], "ok")
        self.assertEqual({k: v["value"] for k, v in r["registers"].items()},
                         {"light": 1, "cleaning": 0, "conveyor_status": 1})
        self.assertTrue(r["light_is_on"])
        self.assertEqual(self.fake.writes, [])

    def test_only_the_given_registers_are_read(self):
        r = P.check(P.build_test_settings(None, {"ip": "192.0.2.10", "conveyor_status_register": CONVEYOR}),
                    client_factory=self.factory, clock=self.clock)
        self.assertEqual(list(r["registers"]), ["conveyor_status"])

    def test_no_register_is_a_bad_request(self):
        r = P.check(P.build_test_settings(None, {"ip": "192.0.2.10"}), client_factory=self.factory)
        self.assertEqual(r["status"], "bad_request")

    def test_unreachable_plc(self):
        self.fake.reachable = False
        r = P.check(settings(), client_factory=self.factory, clock=self.clock)
        self.assertEqual((r["status"], r["ok"]), ("unreachable", False))
        self.assertEqual(P.EXIT_CODES[r["status"]], 3)


# ---------------------------------------------------------------- light
class LightTests(Base):
    def test_off_then_back_on_with_light_on_value_1(self):
        stop = FakeStop()
        r = self.light(settings(light_on_value=1), "off", seconds=7, stop=stop)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(self.fake.writes, [(LIGHT, 0), (LIGHT, 1)])
        self.assertEqual(stop.waited, [7])
        self.assertEqual([w["read_back"] for w in r["writes"]], [0, 1])

    def test_off_then_back_on_with_light_on_value_0(self):
        self.fake.registers[LIGHT] = 0
        r = self.light(settings(light_on_value=0), "off")
        self.assertEqual(self.fake.writes, [(LIGHT, 1), (LIGHT, 0)])
        self.assertTrue(r["restored"])

    def test_on_writes_the_on_value_and_keeps_it(self):
        self.fake.registers[LIGHT] = 0
        self.light(settings(), "on")
        self.assertEqual(self.fake.writes, [(LIGHT, 1), (LIGHT, 1)])

    def test_stop_during_the_wait_restores_at_once(self):
        r = self.light(settings(), "off", seconds=300, stop=FakeStop(stopped=True))
        self.assertEqual(r["status"], "ok")
        self.assertFalse(r["held_full_time"])
        self.assertEqual(self.fake.registers[LIGHT], 1)

    def test_restore_is_retried(self):
        original_wait = FakeStop.wait

        def drop_connection_then_wait(stop, seconds):
            self.fake.fail_writes = 2                  # first two restore attempts fail
            return original_wait(stop, seconds)

        with mock.patch.object(FakeStop, "wait", drop_connection_then_wait):
            r = self.light(settings(), "off")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(self.fake.registers[LIGHT], 1)

    def test_restore_failed_is_reported(self):
        class PlcGoneDuringWait(FakeStop):
            def wait(stop, seconds):
                self.fake.reachable = False
                return False

        r = self.light(settings(), "off", stop=PlcGoneDuringWait())
        self.assertEqual(r["status"], "restore_failed")
        self.assertIn("may be OFF", r["message"])
        self.assertEqual(P.EXIT_CODES[r["status"]], 1)

    def test_only_the_light_register_is_written(self):
        self.light(settings(), "off")
        self.assertEqual({reg for reg, _ in self.fake.writes}, {LIGHT})

    def test_bad_requests(self):
        self.assertEqual(self.light(settings(), "dim")["status"], "bad_request")
        self.assertEqual(self.light(settings(), "off", seconds=0)["status"], "bad_request")
        self.assertEqual(self.light(settings(), "off", seconds=P.MAX_LIGHT_SECONDS + 1)["status"], "bad_request")
        no_light = P.build_test_settings(None, {"ip": "192.0.2.10", "cleaning_register": CLEAN})
        self.assertEqual(self.light(no_light, "off")["status"], "bad_request")
        self.assertEqual(self.fake.writes, [])


# ---------------------------------------------------------------- pulse
class PulseTests(Base):
    def test_one_pulse_of_the_configured_time(self):
        stop = FakeStop()
        r = self.pulse(settings(cleaning_pulse_seconds=4), stop=stop)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(self.fake.writes, [(CLEAN, 1), (CLEAN, 0)])
        self.assertEqual(stop.waited, [4])

    def test_only_the_cleaning_register_is_written(self):
        self.pulse(settings())
        self.assertEqual({reg for reg, _ in self.fake.writes}, {CLEAN})

    def test_failed_end_is_reported(self):
        class PlcGoneDuringWait(FakeStop):
            def wait(stop, seconds):
                self.fake.reachable = False
                return False

        r = self.pulse(settings(), stop=PlcGoneDuringWait())
        self.assertEqual(r["status"], "restore_failed")
        self.assertIn("may be ON", r["message"])

    def test_bad_requests(self):
        no_clean = P.build_test_settings(None, {"ip": "192.0.2.10", "light_register": LIGHT})
        self.assertEqual(self.pulse(no_clean)["status"], "bad_request")
        self.assertEqual(self.pulse(settings(cleaning_pulse_seconds=P.MAX_PULSE_SECONDS + 1))["status"],
                         "bad_request")
        self.assertEqual(self.fake.writes, [])


# ---------------------------------------------------------------- CLI (in process, fake client)
class CliTests(Base):
    def run_cli(self, *argv, env=None):
        out = io.StringIO()
        with mock.patch.object(P, "make_pymodbus_client", self.factory), \
                mock.patch.dict(os.environ, env or {}, clear=False), \
                mock.patch.object(W.signal, "signal"), redirect_stdout(out):
            os.environ.pop("DPC_WORKER_CONFIG", None) if env is None else None
            code = W.main(list(argv))
        return code, out.getvalue()

    def test_check_json(self):
        code, out = self.run_cli("--check", "--json", "--ip", "192.0.2.10", "--light-register", str(LIGHT))
        result = json.loads(out)
        self.assertEqual((code, result["status"], result["registers"]["light"]["value"]), (0, "ok", 1))

    def test_bad_request_exit_2_and_valid_json(self):
        code, out = self.run_cli("--light", "off", "--json", "--ip", "192.0.2.10")
        self.assertEqual((code, json.loads(out)["status"]), (2, "bad_request"))

    def test_settings_from_plc_json(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "plc.json")
        with open(path, "w") as fh:
            json.dump({"paths": {"logs": tmp}, "plc": {"ip": "192.0.2.10", "light_register": LIGHT,
                                                       "cleaning_register": CLEAN}}, fh)
        code, out = self.run_cli("--check", "--json", env={"DPC_WORKER_CONFIG": path})
        result = json.loads(out)
        self.assertEqual((code, result["settings_from"]), (0, "plc.json"))
        self.assertEqual(set(result["registers"]), {"light", "cleaning"})

    def test_two_actions_are_refused(self):
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                mock.patch("sys.stderr", io.StringIO()):
            W.main(["--check", "--pulse", "--ip", "192.0.2.10"])


# ---------------------------------------------------------------- CLI against a real Modbus server
class RealServerCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import pymodbus  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("pymodbus not installed")
        logging.getLogger("pymodbus").setLevel(logging.ERROR)
        try:
            cls.block, _, cls.port = _start_server()
        except Exception as e:
            raise unittest.SkipTest(f"pymodbus test server not available: {e}")
        cls.base = ["--ip", "127.0.0.1", "--port", str(cls.port), "--light-register", str(LIGHT),
                    "--cleaning-register", str(CLEAN), "--conveyor-status-register", str(CONVEYOR)]

    def cli(self, *argv, timeout=30):
        env = {k: v for k, v in os.environ.items() if k != "DPC_WORKER_CONFIG"}
        proc = subprocess.run([sys.executable, os.path.join(WORKER_DIR, "plc_worker.py"), *argv, *self.base,
                               "--json"], env=env, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, json.loads(proc.stdout)

    def test_light_off_then_back_on(self):
        self.cli("--light", "on", "--seconds", "0.1")
        code, r = self.cli("--light", "off", "--seconds", "0.5")
        self.assertEqual((code, r["status"]), (0, "ok"))
        self.assertEqual([w["read_back"] for w in r["writes"]], [0, 1])
        code, r = self.cli("--check")
        self.assertEqual(r["registers"]["light"]["value"], 1)

    def test_pulse(self):
        code, r = self.cli("--pulse", "--cleaning-pulse-seconds", "0.5")
        self.assertEqual((code, r["status"]), (0, "ok"))
        self.assertEqual([(w["value"], w["read_back"]) for w in r["writes"]], [(1, 1), (0, 0)])
        self.assertGreaterEqual(r["duration_s"], 0.5)

    def test_sigterm_during_light_off_restores(self):
        env = {k: v for k, v in os.environ.items() if k != "DPC_WORKER_CONFIG"}
        proc = subprocess.Popen([sys.executable, os.path.join(WORKER_DIR, "plc_worker.py"), "--light", "off",
                                 "--seconds", "60", *self.base, "--json"],
                                env=env, stdout=subprocess.PIPE, text=True)
        time.sleep(2.0)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
        r = json.loads(out)
        self.assertEqual((proc.returncode, r["status"], r["held_full_time"]), (0, "ok", False))
        self.assertEqual(r["writes"][-1]["read_back"], 1)

    def test_unreachable_exit_3(self):
        env = {k: v for k, v in os.environ.items() if k != "DPC_WORKER_CONFIG"}
        proc = subprocess.run([sys.executable, os.path.join(WORKER_DIR, "plc_worker.py"), "--check", "--ip",
                               "127.0.0.1", "--port", str(_free_port()), "--light-register", str(LIGHT),
                               "--timeout-s", "0.5", "--json"],
                              env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual((proc.returncode, json.loads(proc.stdout)["status"]), (3, "unreachable"))


if __name__ == "__main__":
    unittest.main()
