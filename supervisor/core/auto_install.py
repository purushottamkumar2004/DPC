"""
Automatic model install (F82; D-060, D-087, D-105).

The transfer worker downloads a model, packages it, verifies it and marks it ready; it never
touches `current`. It then publishes on dpc/worker/transfer/event:
    {"event": "model_downloaded", "name": <model>, "version": <version>, "worker_id": "transfer", ...}

With workers.yaml `models.auto_update: install` the Supervisor switches to that version, the same
way a person does: it runs `supervisor.py model switch <name> <version>` as a CHILD PROCESS, so
the lock, verify_package, the self-test, `previous`, set_current and the model_switched /
model_switch_failed events are exactly the D-087 ones, and the self-test (up to minutes on the
GPU) never blocks the Supervisor's main loop. The child runs WITHOUT --confirm: inspection
(restart_cost: high) restarts in the reload quiet window, else it waits as config_pending.

Not switched (logged, and a dpc/supervisor/event `model_auto_switch_skipped` for the UI):
    a pinned model (D-060: never switched automatically), a model not used on this site, a
    version that is already current. With auto_update `off` or `download` the event is only
    logged: a person switches. One switch at a time; further downloads wait in order.
"""

import collections
import logging
import os
import queue
import re
import subprocess
import sys
import time

from core.models import ModelCommandError, ModelManager, SELFTEST_TIMEOUT_S
from core.site_env import SiteEnvError

log = logging.getLogger("supervisor.auto_install")

SUPERVISOR_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "supervisor.py")
TRANSFER_EVENT_TOPIC = "worker/transfer/event"          # under the topic prefix (CONTRACTS 2)
CHILD_TIMEOUT_S = SELFTEST_TIMEOUT_S + 300               # the self-test plus verify and the restart request
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class AutoInstaller:
    def __init__(self, settings, site_env, run_dir: str, workers_file: str, publish_event, extra_args=()):
        """settings: the workers.yaml Settings; publish_event(event, **data) -> dpc/supervisor/event.
        extra_args are appended to the child's command line (tests: --selftest-script)."""
        self.settings = settings
        self.site_env = site_env
        self.run_dir = run_dir
        self.workers_file = os.path.abspath(workers_file)
        self.publish_event = publish_event
        self.extra_args = list(extra_args)
        self._inbox = queue.Queue()                      # filled by the MQTT thread
        self._waiting = collections.deque()              # (name, version) decided "switch", in order
        self._child = None
        self._child_job = None
        self._child_started = 0.0
        self._child_output = os.path.join(run_dir, "model_auto_switch.out")

    # ---------------------------------------------------------------- MQTT thread
    def on_transfer_event(self, topic, payload):
        """Only queues; every decision is taken in the main loop (tick)."""
        if isinstance(payload, dict) and payload.get("event") == "model_downloaded":
            self._inbox.put((payload.get("name"), payload.get("version")))

    # ---------------------------------------------------------------- main loop
    def tick(self):
        while True:
            try:
                name, version = self._inbox.get_nowait()
            except queue.Empty:
                break
            self._decide(name, version)
        if self._child is not None:
            self._check_child()
        if self._child is None and self._waiting:
            self._start_child(*self._waiting.popleft())

    def busy(self) -> bool:
        return self._child is not None or bool(self._waiting) or not self._inbox.empty()

    def stop(self):
        """Supervisor shutdown: end a running switch. Before set_current nothing has changed; after
        it the sides load the new version at their next start (both safe, D-087)."""
        if self._child is not None and self._child.poll() is None:
            log.warning("stopping the automatic switch of %s %s (Supervisor stops)", *self._child_job)
            self._child.terminate()
            try:
                self._child.wait(15)
            except subprocess.TimeoutExpired:
                self._child.kill()
            self._finish(None)

    # ---------------------------------------------------------------- steps
    def _decide(self, name, version):
        if not (isinstance(name, str) and isinstance(version, str) and _NAME_RE.match(name)
                and _NAME_RE.match(version)):
            log.warning("model_downloaded with an unusable name/version (%r, %r): ignored", name, version)
            return
        mode = self.settings.models.auto_update
        if mode != "install":
            log.info("model %s %s downloaded; models.auto_update is %s, so a person switches "
                     "(supervisor.py model switch %s %s)", name, version, mode, name, version)
            return
        reason = self._reason_not_to_switch(name, version)
        if reason:
            log.info("model %s %s downloaded, not switched automatically: %s", name, version, reason)
            self.publish_event("model_auto_switch_skipped", model=name, version=version, reason=reason)
            return
        if (name, version) not in self._waiting and self._child_job != (name, version):
            log.info("model %s %s downloaded: switching automatically (auto_update: install)", name, version)
            self._waiting.append((name, version))

    def _reason_not_to_switch(self, name, version):
        try:
            manager = ModelManager(self.site_env.load(), self.run_dir)
            model = manager.get(name)
        except SiteEnvError as e:
            return f"site.yaml not readable ({e})"
        except ModelCommandError as e:
            return str(e)
        state = manager.state(model)
        if state.get("pinned"):
            return f"pinned to {state['pinned']} (D-060: a pinned model is never switched automatically)"
        if state.get("current") == version:
            return "already the current version"
        return None

    def _start_child(self, name, version):
        cmd = [sys.executable, SUPERVISOR_PY, "model", "switch", name, version, "-c", self.workers_file]
        if self.site_env.site_config_path:
            cmd += ["--site-config", os.path.abspath(os.path.expanduser(self.site_env.site_config_path))]
        cmd += self.extra_args
        os.makedirs(self.run_dir, exist_ok=True)
        with open(self._child_output, "w") as out:
            self._child = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT,
                                           cwd=os.path.dirname(SUPERVISOR_PY), start_new_session=True)
        self._child_job = (name, version)
        self._child_started = time.time()
        log.info("automatic switch of %s %s started (pid %s): verify, self-test, then restart its sides",
                 name, version, self._child.pid)

    def _check_child(self):
        code = self._child.poll()
        if code is None:
            if time.time() - self._child_started > CHILD_TIMEOUT_S:
                log.error("automatic switch of %s %s took longer than %d s: stopped", *self._child_job, CHILD_TIMEOUT_S)
                self._child.kill()
                self._child.wait()
                self.publish_event("model_switch_failed", model=self._child_job[0], action="auto_install",
                                   to=self._child_job[1], reason=f"no result within {CHILD_TIMEOUT_S} s")
                self._finish(None)
            return
        self._finish(code)

    def _finish(self, code):
        name, version = self._child_job
        for line in _read_lines(self._child_output):
            log.info("[model switch %s %s] %s", name, version, line)
        if code == 0:
            log.info("automatic switch of %s %s done", name, version)
        elif code is not None:
            log.warning("automatic switch of %s %s refused or failed (exit %s); the model stays as it was",
                        name, version, code)
        try:
            os.remove(self._child_output)
        except OSError:
            pass
        self._child = None
        self._child_job = None


def _read_lines(path):
    try:
        with open(path, errors="replace") as f:
            return [line.rstrip() for line in f if line.strip()][-40:]
    except OSError:
        return []
