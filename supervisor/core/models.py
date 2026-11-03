"""
Model management per model name (P11; D-056, D-060, D-061, D-086; CONTRACTS 12).

    <root>/data/models/<name>/<version>/   a package (manifest.json, weights, Detectron yaml, classes.json)
    <root>/data/models/<name>/current      symlink to the running version   (Supervisor only, D-060)
    <root>/data/models/<name>/pinned       file holding the pinned version  (Supervisor only)
    <root>/data/models/<name>/previous     file holding the version before the last switch, for
                                           `model rollback` (Supervisor only; new in P11)

Everything works per model NAME: a site may run one model on both sides or its own model per
side (D-086). The sides that run a model come from common.config.model_package.models_in_use()
and the inspection worker of a side from the renderer's SIDE_WORKER (inspection_left/right).

This module does the slow and risky part of a switch, in the process that runs the command
(`supervisor.py model ...`), never in the Supervisor's main loop:
    1. checks (model in use on this site, version name, not pinned to another version),
    2. verify_package() (manifest, every sha256, classes),
    3. the model self-test (F47) on the newest frames of a side that runs the model,
    4. `previous` := old current, then set_current() (atomic symlink).
A refusal at 1-3 changes nothing on disk and restarts nothing. Restarting the inspection sides
is then asked of the running Supervisor (supervisor.py, reload channel), which applies the
restart_cost: high rules (quiet window or --confirm, D-070).

Texts in ModelCommandError are meant for the person and the MQTT event; they never hold a
secret (site.yaml values are not part of any model path or message).
"""

import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))     # .../code
DEFAULT_SELFTEST = os.path.join(REPO_ROOT, "workers", "inspection", "model_selftest.py")
PREVIOUS_MARKER = "previous"
SELFTEST_TIMEOUT_S = 900          # loads a Mask R-CNN and runs a few frames; generous on a busy GPU
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")     # the rule model_package uses
_RESERVED = ("current", "pinned", "previous")


class ModelCommandError(Exception):
    """A model command was refused. The text says why, for the person and for the MQTT event."""


def _package_api():
    if REPO_ROOT not in sys.path:
        sys.path.append(REPO_ROOT)
    from common.config import model_package
    from common.config.render import SIDE_WORKER
    return model_package, SIDE_WORKER


@dataclass
class ModelInUse:
    """One model name as this site uses it."""
    name: str
    root: str                                   # <root>/data/models/<name>
    sides: List[str]                            # e.g. ["LEFT", "RIGHT"], camera order
    workers: List[str]                          # e.g. ["inspection_left", "inspection_right"]
    frame_dirs: List[str]                       # RAW_IMAGES of those sides, same order
    threshold: float                            # inspection.threshold (the value rendered for each side)


@dataclass
class SelftestResult:
    passed: bool
    reason: str = ""
    summary: Dict = field(default_factory=dict)
    frames_dir: str = ""


class ModelManager:
    def __init__(self, site_config, run_dir: str, selftest_script: str = DEFAULT_SELFTEST,
                 python: str = sys.executable):
        self.cfg = site_config
        self.run_dir = run_dir
        self.selftest_script = selftest_script
        self.python = python
        self.mp, side_worker = _package_api()
        self.models: Dict[str, ModelInUse] = {}
        for name, sides in self.mp.models_in_use(site_config).items():
            cams = {cam.side: cam.id for cam in site_config.cameras}
            root = self._model_root(sides[0])
            self.models[name] = ModelInUse(
                name=name, root=root, sides=list(sides), workers=[side_worker[s] for s in sides],
                frame_dirs=[site_config.paths.frame_dir(cams[s], "RAW_IMAGES") for s in sides],
                threshold=site_config.inspection.threshold)

    def _model_root(self, side: str) -> str:
        insp = self.cfg.inspection
        model = insp.sides[side].model if side in insp.sides and insp.sides[side].model else insp.model
        return model.root

    # ------------------------------------------------------------------ reading
    def get(self, name: str) -> ModelInUse:
        if name not in self.models:
            used = ", ".join(sorted(self.models)) or "none"
            raise ModelCommandError(f"model {name!r} is not used on this site (models in use: {used})")
        return self.models[name]

    def installed(self, model: ModelInUse) -> List[Dict]:
        """Version folders that hold a manifest.json, sorted by name; `ready` = marked ready by the
        packaging tool or the transfer service. Not verified here (that reads every file)."""
        result = []
        if os.path.isdir(model.root):
            for entry in sorted(os.listdir(model.root)):
                path = os.path.join(model.root, entry)
                if (entry in _RESERVED or entry.startswith(".") or os.path.islink(path)
                        or not os.path.isfile(os.path.join(path, self.mp.MANIFEST_FILE))):
                    continue
                result.append({"version": entry, "ready": os.path.isfile(os.path.join(path, self.mp.READY_FILE))})
        return result

    def state(self, model: ModelInUse) -> Dict:
        """current / previous / pinned; a broken marker is reported, not raised."""
        out = {}
        for key, reader in (("current", self.mp.read_current), ("pinned", self.mp.read_pinned),
                            ("previous", self._read_previous)):
            try:
                out[key] = reader(model.root)
            except self.mp.ModelPackageError as e:
                out[key] = None
                out.setdefault("problems", []).extend(f"{key}: {p}" for p in e.problems)
        return out

    def status(self) -> List[Dict]:
        rows = []
        for name in sorted(self.models):
            model = self.models[name]
            rows.append({"model": name, "sides": model.sides, "workers": model.workers, **self.state(model),
                         "installed": self.installed(model)})
        return rows

    def may_switch_automatically(self, name: str, auto_update: str) -> bool:
        """For P12 (transfer, auto_update: install): never when pinned (D-060)."""
        return auto_update == "install" and self.state(self.get(name)).get("pinned") is None

    # ------------------------------------------------------------------ changing
    def switch(self, name: str, version: str) -> Dict:
        """Checks, verify, self-test, then previous := current, current := version.
        Returns {model, from, to, sides, workers, selftest}. Raises ModelCommandError and then
        changes nothing."""
        model = self.get(name)
        self._check_version_name(version)
        state = self.state(model)
        self._refuse_if_pinned_elsewhere(model, state, version)
        if state.get("current") == version:
            raise ModelCommandError(f"{name} {version} is already the current version")
        self._verify(model, version)
        test = self.run_selftest(model, version)
        if not test.passed:
            raise ModelCommandError(f"self-test failed for {name} {version}: {test.reason}")
        self._move_current(model, state.get("current"), version)
        return {"model": name, "from": state.get("current"), "to": version, "sides": model.sides,
                "workers": model.workers, "selftest": test.summary}

    def rollback(self, name: str) -> Dict:
        """Back to `previous` (verified; no self-test: that version ran here before, and a
        rollback must work when the self-test itself is the problem). Both stay on disk."""
        model = self.get(name)
        state = self.state(model)
        target = state.get("previous")
        if not target:
            raise ModelCommandError(f"{name} has no previous version to roll back to")
        if target == state.get("current"):
            raise ModelCommandError(f"{name}: the previous version {target} is already current")
        self._refuse_if_pinned_elsewhere(model, state, target)
        self._verify(model, target)
        self._move_current(model, state.get("current"), target)
        return {"model": name, "from": state.get("current"), "to": target, "sides": model.sides,
                "workers": model.workers}

    def pin(self, name: str, version: str) -> Dict:
        model = self.get(name)
        self._check_version_name(version)
        if not os.path.isdir(os.path.join(model.root, version)):
            raise ModelCommandError(f"{name}: version folder {version} not found")
        old = self.state(model).get("pinned")
        _write_marker(os.path.join(model.root, self.mp.PINNED_MARKER), version)
        current = self.state(model).get("current")
        return {"model": name, "version": version, "previous_pin": old, "current": current}

    def unpin(self, name: str) -> Dict:
        model = self.get(name)
        old = self.state(model).get("pinned")
        marker = os.path.join(model.root, self.mp.PINNED_MARKER)
        if not os.path.exists(marker):
            raise ModelCommandError(f"{name} is not pinned")
        os.remove(marker)
        return {"model": name, "version": old}

    @contextlib.contextmanager
    def lock(self):
        """One model command at a time on this machine (two switches must not interleave)."""
        os.makedirs(self.run_dir, exist_ok=True)
        with open(os.path.join(self.run_dir, "model.lock"), "w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ModelCommandError("another model command is running; try again when it is done") from None
            yield

    # ------------------------------------------------------------------ steps
    def run_selftest(self, model: ModelInUse, version: str) -> SelftestResult:
        """workers/inspection/model_selftest.py (F47, CONTRACTS 12) with the inspection Python,
        on the RAW_IMAGES of the first side of this model that has frames, with the side's
        threshold. Exit 0 = pass. The reason comes from the --json line, else the last line."""
        frames = next((d for d in model.frame_dirs if _has_jpg(d)), model.frame_dirs[0])
        cmd = [self.python, self.selftest_script, "--model", model.name, "--version", version,
               "--frames", frames, "--threshold", str(model.threshold), "--json",
               "--models-dir", os.path.dirname(model.root)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=SELFTEST_TIMEOUT_S,
                                  cwd=os.path.dirname(self.selftest_script) or None)
        except subprocess.TimeoutExpired:
            return SelftestResult(False, f"no result within {SELFTEST_TIMEOUT_S} s", frames_dir=frames)
        except OSError as e:
            return SelftestResult(False, f"cannot run the self-test ({e.strerror})", frames_dir=frames)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        summary = _json_line(lines[-1]) if lines else {}
        if proc.returncode == 0:
            return SelftestResult(True, summary=summary, frames_dir=frames)
        reason = summary.get("reason") or _last_text_line(lines) or _last_text_line(proc.stderr.splitlines()) \
            or f"exit code {proc.returncode}"
        return SelftestResult(False, str(reason)[:300], summary, frames)

    def _verify(self, model: ModelInUse, version: str):
        try:
            self.mp.verify_package(os.path.join(model.root, version), name=model.name, version=version)
        except self.mp.ModelPackageError as e:
            raise ModelCommandError(f"package {model.name} {version} is not usable: " + "; ".join(e.problems)) from None

    def _move_current(self, model: ModelInUse, old: Optional[str], new: str):
        if old and old != new:
            _write_marker(os.path.join(model.root, PREVIOUS_MARKER), old)      # before current moves
        self.mp.set_current(model.root, new)

    def _refuse_if_pinned_elsewhere(self, model: ModelInUse, state: Dict, version: str):
        pinned = state.get("pinned")
        if pinned and pinned != version:
            raise ModelCommandError(f"{model.name} is pinned to {pinned}: run `model unpin {model.name}` first")

    @staticmethod
    def _check_version_name(version: str):
        if not _VERSION_RE.match(version or "") or version in _RESERVED:
            raise ModelCommandError(f"{version!r} is not a version name")

    @staticmethod
    def _read_previous(model_root: str) -> Optional[str]:
        path = os.path.join(model_root, PREVIOUS_MARKER)
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as f:
            version = f.read().strip()
        return version if _VERSION_RE.match(version) and version not in _RESERVED else None


def _write_marker(path: str, version: str):
    """Same one-line format as `pinned` (read_pinned() strips it); atomic replace."""
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(version + "\n")
    os.replace(tmp, path)


def _has_jpg(folder: str) -> bool:
    try:
        return any(n.lower().endswith(".jpg") for n in os.listdir(folder))
    except OSError:
        return False


def _json_line(line: str) -> Dict:
    try:
        data = json.loads(line)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _last_text_line(lines) -> str:
    for line in reversed(list(lines)):
        if line.strip() and not line.lstrip().startswith("{"):
            return line.strip()
    return ""
