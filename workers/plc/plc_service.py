"""What the PLC service does, one tick at a time (no threads, no sleeps: easy to test).

Kept from the old scripts (workers/plc/light.py, cleaning_mechanism.py):
- Light (A8): write `light_on_value` to `light_register` (old light.py wrote 1; 1 = ON).
- Cleaning (A10): write 1 to `cleaning_register`, wait `cleaning_pulse_seconds`, write 0.

New (D-024, CODE_AUDIT 7):
- The light is written at start and after every reconnect (a PLC reboot used to leave it
  off), then read every `light_check_seconds` and rewritten only when it differs.
- The conveyor status register is read every `status_interval_seconds` (the old scripts
  declared it but never read it); its raw value is published, its meaning is the site's.
- A pulse is never left ON by us: the 0 is retried until it is written, a marker file in
  `<state>/plc/pulse_open.json` closes a pulse cut by a crash at the next start, and a
  stop request ends a running pulse at once.
- Pulse requests: "manual" (the MQTT command, today's behaviour: a person asks) is always
  accepted when no pulse runs; "auto" (`poor_view` event) only with `auto_cleaning: true`
  and after `cleaning_cooldown_seconds` since the last pulse.
"""

import datetime
import json
import os
import queue
import time
from typing import Callable, Optional

LIGHT_START, CLEAN_ON, CLEAN_OFF = "start", 1, 0


class PlcService:
    def __init__(self, settings, link, log, publish_event: Callable[[str, dict], None] = None,
                 clock: Callable = time.monotonic):
        self.s = settings
        self.link = link
        self.log = log
        self.clock = clock
        self.publish_event = publish_event or (lambda name, payload: None)
        self.requests = queue.Queue()        # filled by the MQTT thread, drained by tick()

        # light
        self.light_value: Optional[int] = None
        self.light_pending_reason: Optional[str] = None
        self.next_light_check = 0.0
        self.light_writes = 0
        # conveyor status
        self.conveyor_status: Optional[int] = None
        self.next_status_read = 0.0
        # cleaning pulse: idle -> on -> closing -> idle
        self.pulse_state = "idle"
        self.pulse_end_at = 0.0
        self.pulse_reason = ""
        self.last_pulse_end: Optional[float] = None
        self.last_pulse_at: Optional[str] = None     # wall-clock text, for the status payload

    # ---------------- start / stop ----------------
    @property
    def marker_path(self) -> str:
        return os.path.join(self.s.state, "plc", "pulse_open.json")

    def start(self):
        """Close a pulse an earlier run left open (we only ever close OUR pulse)."""
        if os.path.exists(self.marker_path):
            self.log.warning("found an open cleaning pulse from an earlier run: closing it")
            self.pulse_state = "closing"
            self.pulse_reason = "close pulse left open by an earlier run"

    def stop(self, budget_s: float = 10.0, sleep: Callable = time.sleep):
        """On SIGTERM: end a running pulse now (write 0), trying for at most budget_s."""
        if self.pulse_state == "idle":
            return
        self.pulse_state = "closing"
        self.pulse_reason = "stop requested"
        deadline = self.clock() + budget_s
        while self.pulse_state != "idle" and self.clock() < deadline:
            self._close_pulse()
            if self.pulse_state != "idle":
                sleep(0.5)
        if self.pulse_state != "idle":
            self.log.error("could not write 0 to the cleaning register before stopping; "
                           "the next start closes it (marker kept)")

    # ---------------- requests (any thread) ----------------
    def request_pulse(self, source: str, reason: str, automatic: bool = False):
        self.requests.put({"source": source, "reason": reason, "automatic": automatic})

    # ---------------- the loop body ----------------
    def tick(self):
        now = self.clock()
        connected = self.link.ensure_connected()
        if connected and self.link.just_reconnected():
            self.light_pending_reason = LIGHT_START if self.light_writes == 0 else "reconnect"
        self._pulse_step(now)
        self._handle_requests(now)
        if connected:
            self._light_step(now)
            self._status_step(now)

    # ---------------- light ----------------
    def _light_step(self, now):
        if self.light_pending_reason is None and now >= self.next_light_check:
            self.next_light_check = now + self.s.light_check_seconds
            value = self.link.read(self.s.light_register)
            if value is None:
                return
            self.light_value = value
            if value != self.s.light_on_value:
                self.light_pending_reason = f"verify: read {value}"
        if self.light_pending_reason is not None:
            reason = f"light on, {self.light_pending_reason}"
            if self.link.write(self.s.light_register, self.s.light_on_value, reason):
                self.light_writes += 1
                self.light_value = self.s.light_on_value
                self.next_light_check = now + self.s.light_check_seconds
                self.publish_event("light_on_written", {"register": self.s.light_register,
                                                        "value": self.s.light_on_value,
                                                        "reason": self.light_pending_reason})
                self.light_pending_reason = None

    @property
    def light_ok(self) -> Optional[bool]:
        return None if self.light_value is None else self.light_value == self.s.light_on_value

    # ---------------- conveyor status ----------------
    def _status_step(self, now):
        if now < self.next_status_read:
            return
        self.next_status_read = now + self.s.status_interval_seconds
        value = self.link.read(self.s.conveyor_status_register)
        if value is not None and value != self.conveyor_status:
            self.log.info(f"conveyor status register {self.s.conveyor_status_register} = {value}"
                          f" (was {self.conveyor_status})")
        if value is not None:
            self.conveyor_status = value

    # ---------------- cleaning pulse ----------------
    def _handle_requests(self, now):
        while True:
            try:
                req = self.requests.get_nowait()
            except queue.Empty:
                return
            self._try_start_pulse(now, req)

    def _try_start_pulse(self, now, req):
        label = f"{req['source']}: {req['reason']}"
        if self.pulse_state != "idle":
            self.log.info(f"cleaning request ignored, a pulse is already running ({label})")
            return
        if req["automatic"]:
            if not self.s.auto_cleaning:
                self.log.debug(f"automatic cleaning is off; ignored ({label})")
                return
            if self.last_pulse_end is not None and \
                    now - self.last_pulse_end < self.s.cleaning_cooldown_seconds:
                self.log.info(f"cleaning request ignored, cooldown {self.s.cleaning_cooldown_seconds:.0f} s"
                              f" not over ({label})")
                return
        if not self.link.connected:
            self.log.warning(f"cleaning request ignored, PLC not reachable ({label})")
            return
        self._write_marker(label)                     # before the 1: a crash after it must close it
        if not self.link.write(self.s.cleaning_register, CLEAN_ON, f"cleaning pulse start, {label}"):
            self._remove_marker()
            self.log.error(f"cleaning pulse not started, write failed ({label})")
            return
        self.pulse_state = "on"
        self.pulse_reason = label
        self.pulse_end_at = now + self.s.cleaning_pulse_seconds
        self.publish_event("cleaning_pulse_started", {"source": req["source"], "reason": req["reason"],
                                                      "seconds": self.s.cleaning_pulse_seconds})

    def _pulse_step(self, now):
        if self.pulse_state == "on" and now >= self.pulse_end_at:
            self.pulse_state = "closing"
        if self.pulse_state == "closing":
            self._close_pulse()

    def _close_pulse(self):
        reason = f"cleaning pulse end, {self.pulse_reason}"
        if self.link.write(self.s.cleaning_register, CLEAN_OFF, reason):
            self.pulse_state = "idle"
            self.last_pulse_end = self.clock()
            self.last_pulse_at = datetime.datetime.now().isoformat(timespec="seconds")
            self._remove_marker()
            self.publish_event("cleaning_pulse_ended", {"reason": self.pulse_reason})

    def _write_marker(self, label: str):
        os.makedirs(os.path.dirname(self.marker_path), exist_ok=True)
        tmp = self.marker_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"at": datetime.datetime.now().isoformat(timespec="seconds"),
                       "register": self.s.cleaning_register, "reason": label}, fh)
        os.replace(tmp, self.marker_path)

    def _remove_marker(self):
        try:
            os.remove(self.marker_path)
        except FileNotFoundError:
            pass

    # ---------------- status for MQTT / DB ----------------
    def snapshot(self) -> dict:
        return {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "connected": self.link.connected,
            "conveyor_status": self.conveyor_status,
            "light_value": self.light_value,
            "light_ok": self.light_ok,
            "cleaning_active": self.pulse_state != "idle",
            "last_pulse_at": self.last_pulse_at,
            "auto_cleaning": self.s.auto_cleaning,
        }
