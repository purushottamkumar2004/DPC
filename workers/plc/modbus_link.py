"""The one connection to the PLC (CODE_AUDIT 7: the old scripts opened a new client per call
and never closed it).

- One client object, created once and reconnected with backoff after a failure.
- An unreachable PLC is not an error of this program: calls return None/False, the caller
  carries on, and the outage is logged once (plus a reminder every OUTAGE_REMINDER_S).
- Every write goes through write(): a register outside `allowed` raises ValueError
  (a programming error, so the program stops with last_error instead of writing it).
- The Modbus calls are the old ones: write_register(address, value) and
  read_holding_registers(address, count) with the library's default unit id.
"""

import time
from typing import Callable, Iterable, Optional

BACKOFF_S = (1, 2, 5, 10, 30)       # wait before reconnect attempt n (last value repeats)
OUTAGE_REMINDER_S = 600             # "still unreachable" line every 10 min, not every retry


def make_pymodbus_client(ip: str, port: int, timeout_s: float):
    """The real client (pymodbus 3.x; imported here so tests run without pymodbus)."""
    from pymodbus.client import ModbusTcpClient
    return ModbusTcpClient(ip, port=port, timeout=timeout_s)


class PlcLink:
    def __init__(self, ip: str, port: int, timeout_s: float, allowed: Iterable[int], log,
                 client_factory: Callable = make_pymodbus_client, clock: Callable = time.monotonic):
        self.address = f"{ip}:{port}"
        self.allowed = frozenset(allowed)
        self.log = log
        self.clock = clock
        self.client = client_factory(ip, port, timeout_s)   # created ONCE, reused
        self.connected = False
        self.ever_connected = False
        self._attempts = 0
        self._next_attempt = 0.0
        self._down_since: Optional[float] = None
        self._last_reminder = 0.0
        self._last_error_text = None
        self._fresh = False                          # set on each successful (re)connect

    # ---------------- connection ----------------
    def ensure_connected(self) -> bool:
        """Connect if needed and if the backoff allows it now. Returns True when connected."""
        if self.connected:
            return True
        now = self.clock()
        if now < self._next_attempt:
            return False
        ok = False
        try:
            ok = bool(self.client.connect())
        except Exception as e:                       # pymodbus raises on some socket errors
            self._note_error(f"connect failed: {e}")
        if ok:
            self._on_connected(now)
            return True
        self._on_failed_attempt(now)
        return False

    def just_reconnected(self) -> bool:
        """True once after each successful (re)connect; the service re-asserts the light then."""
        flag = self._fresh
        self._fresh = False
        return flag

    def _on_connected(self, now):
        if self._down_since is not None and self.ever_connected:
            self.log.info(f"PLC {self.address} reachable again after {now - self._down_since:.0f} s")
        elif not self.ever_connected:
            self.log.info(f"PLC {self.address} connected")
        self.connected = True
        self.ever_connected = True
        self._fresh = True
        self._attempts = 0
        self._down_since = None
        self._last_error_text = None

    def _on_failed_attempt(self, now):
        wait = BACKOFF_S[min(self._attempts, len(BACKOFF_S) - 1)]
        self._attempts += 1
        self._next_attempt = now + wait
        if self._down_since is None:
            self._down_since = now
            self._last_reminder = now
            self.log.warning(f"PLC {self.address} unreachable; retrying (backoff up to {BACKOFF_S[-1]} s)")
        elif now - self._last_reminder >= OUTAGE_REMINDER_S:
            self._last_reminder = now
            self.log.warning(f"PLC {self.address} still unreachable for {now - self._down_since:.0f} s")

    def _drop(self, reason: str):
        """The connection is broken: close it; the next ensure_connected() retries."""
        self._note_error(reason)
        self.connected = False
        try:
            self.client.close()
        except Exception:
            pass
        self._on_failed_attempt(self.clock())

    def _note_error(self, text: str):
        if text != self._last_error_text:             # no flood of identical lines
            self.log.debug(text)
            self._last_error_text = text

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass
        self.connected = False

    # ---------------- register access ----------------
    def read(self, register: int) -> Optional[int]:
        """Value of one holding register, or None (not connected / no answer / PLC error)."""
        if not self.ensure_connected():
            return None
        try:
            result = self.client.read_holding_registers(address=register, count=1)
        except Exception as e:
            self._drop(f"read {register} failed: {e}")
            return None
        if _is_connection_failure(result):
            self._drop(f"read {register}: no answer ({result})")
            return None
        if result.isError():
            self.log.error(f"PLC refused read of register {register}: {result}")
            return None
        return int(result.registers[0])

    def write(self, register: int, value: int, reason: str) -> bool:
        """Write one holding register; logs register, value and reason at INFO when it worked."""
        if register not in self.allowed:
            raise ValueError(f"refusing to write register {register}: not in the configured set "
                             f"{sorted(self.allowed)}")
        if not self.ensure_connected():
            return False
        try:
            result = self.client.write_register(address=register, value=int(value))
        except Exception as e:
            self._drop(f"write {register}={value} failed: {e}")
            return False
        if _is_connection_failure(result):
            self._drop(f"write {register}={value}: no answer ({result})")
            return False
        if result.isError():
            self.log.error(f"PLC refused write {register}={value} ({reason}): {result}")
            return False
        self.log.info(f"write register {register} = {value} ({reason})")
        return True


def _is_connection_failure(result) -> bool:
    """pymodbus may hand back an exception object (e.g. ModbusIOException) instead of raising."""
    return result is None or isinstance(result, Exception)
