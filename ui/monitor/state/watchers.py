"""Background checks of the UI process (started by run_ui.py, never by tests or manage.py):

* disk level every DISK_EVERY_S: a change of level (ok / delete / mail) is stored as an event
* camera ping every PING_EVERY_S (results shown on System health)
* event pruning every PRUNE_EVERY_S (ui.event_keep_days, ui.event_keep_rows)
Each step is guarded: an error is logged and the loop goes on.
"""
import logging
import threading
import time

from monitor.sources import system

log = logging.getLogger("dpc.ui.watchers")

DISK_EVERY_S = 30
PING_EVERY_S = 15
PRUNE_EVERY_S = 3600


class Watchers:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self._stop = threading.Event()
        self._due = {"disk": 0.0, "ping": 0.0, "prune": 0.0}

    def start(self):
        threading.Thread(target=self._run, name="ui-watchers", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(1.0):
            now = time.time()
            for name, every, step in (("disk", DISK_EVERY_S, self.check_disk),
                                      ("ping", PING_EVERY_S, self.ping_cameras),
                                      ("prune", PRUNE_EVERY_S, self.prune)):
                if now >= self._due[name]:
                    self._due[name] = now + every
                    try:
                        step()
                    except Exception:
                        log.exception("background check '%s' failed", name)

    def check_disk(self):
        disk = system.disk(self.cfg.paths["data"], self.cfg["cleanup"]["disk_percent"])
        if not disk["ok"]:
            return
        changed, previous, _ = self.store.transition("disk", disk["level"])
        if changed and not (previous is None and disk["level"] == "ok"):
            self.store.record("ui/disk", "disk", f"disk_{disk['level']}", {"percent": disk["percent"]})

    def ping_cameras(self):
        for cam in self.cfg["cameras"]:
            if cam.get("ip"):
                system.ping(cam["ip"])

    def prune(self):
        removed = self.store.prune(int(self.cfg["ui"].get("event_keep_days", 90)),
                                   int(self.cfg["ui"].get("event_keep_rows", 50000)))
        if removed:
            log.info("removed %d old events", removed)
